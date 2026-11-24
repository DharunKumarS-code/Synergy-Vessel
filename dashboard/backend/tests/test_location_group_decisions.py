"""Phase 10.5 -- whole-group location decisions (admin only, all-or-nothing). Synthetic data and temporary folders only."""

from __future__ import annotations

import pytest

import app.imports.service as service_module
from app.location_intel.providers import MockLocationProvider
from app.location_intel.service import LocationIntelligence
from conftest import jpeg_bytes, sign_in
from test_deck_photos_import import BANNER, DECK_NAME, deck
from test_imports import commit, make, preview, row, tree, workbook  # noqa: F401
from test_location_intel import approve_all, client, no_network  # noqa: F401  (trusted seed fixture + network guard)
from test_location_synonyms import FCL, create, listing
from test_location_workbench import decide, record, withdraw
from test_trusted_locations import commit_deck, known, row_of

GD = "/api/locations/review/group-decision"
BOW = "bow mooring station"


def plan(c, gk, action, location=None):
    r = c.post(GD, json={"group_key": gk, "action": action, "location": location, "dry_run": True})
    assert r.status_code == 200, r.text
    return r.json()


def apply(c, gk, action, location=None, expected=None, status=200):
    if expected is None:
        expected = plan(c, gk, action, location)["eligible_count"]
    r = c.post(GD, json={"group_key": gk, "action": action, "location": location, "expected_count": expected})
    assert r.status_code == status, r.text
    return r.json()


def bow_group(c, seed=2000):
    a = commit_deck(c, {4: "Bow mooring station", 8: "BOW mooring  station"}, seed)
    b = commit_deck(c, {4: "bow mooring station"}, seed + 10)
    return a, b


def files(c):
    return {p.relative_to(c.app.state.imports.dir).as_posix(): p.read_bytes()
            for p in c.app.state.imports.dir.rglob("locations.json")}


def audit(c):
    return c.app.state.auth.audit.entries(limit=1000)


def recs(c, iid):
    return [r for r in c.app.state.store.records if r.import_id == iid]


def in_queue(c, gk):
    return any(g["group_key"] == gk for g in c.get("/api/locations/review").json()["groups"])


# ------------------------------------------------------------------ manual
def test_manual_group_decision_resolves_once_and_applies_to_every_eligible_row(client):
    a, b = bow_group(client)
    before_files, before_audit = files(client), len(audit(client))
    p = plan(client, BOW, "manual", "forecastle  deck area")
    assert (p["location"], p["resolved_via"], p["new_location"], p["eligible_count"], p["dry_run"]) == \
        (FCL, "trusted_location", False, 3, True)
    assert files(client) == before_files and len(audit(client)) == before_audit      # a preview writes nothing
    r = apply(client, BOW, "manual", "forecastle  deck area", expected=3)
    assert r["applied"] is True and r["rolled_back"] is False and len(r["decided_rows"]) == 3
    gid = r["group_decision_id"]
    for iid in (a, b):
        for rec in recs(client, iid):
            assert (rec.official_location, rec.location_review) == (FCL, "manual")
            assert rec.original_location.casefold().split() == BOW.split()           # original Excel text kept
        d = client.get(f"/api/imports/{iid}").json()["location_decisions"]
        assert all(x["group"]["id"] == gid and x["entered_location"] == "forecastle deck area" for x in d.values())
    assert not in_queue(client, BOW)
    entry = audit(client)[0]
    assert entry["event"] == "location_group_decided" and entry["target"] == gid
    assert entry["details"]["count"] == 3 and entry["details"]["location"] == FCL and entry["details"]["action"] == "manual"
    assert sorted(x["import_id"] for x in entry["details"]["records"]) == sorted([a, a, b])
    assert {(x["import_id"], x["row"]) for x in entry["details"]["records"]} == \
        {(x["import_id"], x["row_number"]) for x in r["decided_rows"]}
    assert entry["actor"]["role"] == "admin"


def test_manual_new_location_follows_existing_behaviour(client):
    bow_group(client)
    r = apply(client, BOW, "manual", "Bow Mooring Station")
    assert (r["location"], r["new_location"], r["resolved_via"]) == ("Bow Mooring Station", True, "new_location")
    assert "Bow Mooring Station" in known(client)                                    # manual approval is trusted (10.2)


def test_manual_through_active_and_inactive_synonyms(client):
    a = commit_deck(client, {4: "Stern store"}, 2020)
    approve_all(client, a)                                                            # "Stern store" trusted
    assert create(client, "Aft store room", "Stern store").status_code == 201
    bow_group(client, 2030)
    p = plan(client, BOW, "manual", "AFT  store room")
    assert (p["location"], p["resolved_via"], p["new_location"]) == ("Stern store", "managed_synonym", False)
    withdraw(client, a)                                                               # synonym now inactive
    p = plan(client, BOW, "manual", "Aft store room")
    assert (p["location"], p["resolved_via"], p["new_location"]) == ("Aft store room", "new_location", True)
    withdraw(client, a, "reinstate")
    r = apply(client, BOW, "manual", "Aft store room")
    assert (r["location"], r["resolved_via"]) == ("Stern store", "managed_synonym")
    assert listing(client)["synonyms"][0]["status"] == "active" and "Aft store room" not in known(client)


def test_manual_group_decision_keeps_location_numbers(client):
    commit_deck(client, {4: "Cargo Hold 9", 8: "cargo hold 9"}, 2040)
    before = files(client)
    for loc in ("CARGO HOLD NO. 3", "Cargo hold"):
        r = client.post(GD, json={"group_key": "cargo hold 9", "action": "manual", "location": loc, "dry_run": True})
        assert (r.status_code, r.json()["code"]) == (422, "number_mismatch")
    r = client.post(GD, json={"group_key": "cargo hold 9", "action": "manual", "location": "CARGO HOLD NO. 3", "expected_count": 2})
    assert (r.status_code, r.json()["code"]) == (422, "number_mismatch")
    assert files(client) == before
    assert apply(client, "cargo hold 9", "manual", "Cargo Hold No 9")["new_location"] is True   # same number is fine


# ------------------------------------------------------------------ keep original / reject
def test_keep_original_group_decision_establishes_no_trust(client):
    a, b = bow_group(client)
    r = apply(client, BOW, "keep_original")
    assert len(r["decided_rows"]) == 3 and r["location"] is None
    for iid in (a, b):
        assert {(x.location_review, x.official_location == x.original_location) for x in recs(client, iid)} == {("kept_original", True)}
    assert not {"Bow mooring station", "bow mooring station"} & set(known(client).names)
    assert not (client.app.state.imports.dir / "location_synonyms.json").exists()     # never creates synonyms


def aft_setup(c, seed=2100):
    a = commit_deck(c, {4: "Aft winch house"}, seed)
    decide(c, a, "Aft winch house", "manual", "Aft Winch House", new=True)             # new -> trusted
    b = commit_deck(c, {4: "Aft winch"}, seed + 10)                                    # partial candidates, pending
    c2 = commit_deck(c, {4: "aft  WINCH"}, seed + 20)
    return a, b, c2


def test_reject_group_decision_keeps_originals(client):
    _, b, c2 = aft_setup(client)
    commit_deck(client, {12: "Aft winch "}, 2150)                                       # same group, same candidate
    r = apply(client, "aft winch", "reject")
    assert len(r["decided_rows"]) == 3
    assert {x.location_review for iid in (b, c2) for x in recs(client, iid)} == {"rejected"}
    assert record(client, b).official_location == "Aft winch"


# ------------------------------------------------------------------ accept
def test_accept_lists_current_suggestions_then_applies_the_confirmed_one(client):
    _, b, c2 = aft_setup(client)
    first = plan(client, "aft winch", "accept")
    assert first["suggestions"] == [{"location": "Aft Winch House", "count": 2}] and first["eligible_count"] == 0
    r = apply(client, "aft winch", "accept", "Aft Winch House")
    assert (r["location"], r["resolved_via"], len(r["decided_rows"])) == ("Aft Winch House", "trusted_location", 2)
    for iid in (b, c2):
        rec = record(client, iid)
        assert (rec.official_location, rec.location_review, rec.location_method) == ("Aft Winch House", "accepted", "approved_suggestion")
    r = client.post(GD, json={"group_key": "aft winch", "action": "accept", "expected_count": 2})
    assert (r.status_code, r.json()["code"]) == (400, "choose_suggestion")


def test_accept_never_applies_a_stale_untrusted_suggestion(client):
    a, b, _ = aft_setup(client)
    withdraw(client, a)                                                                # the candidate loses its trust
    p = plan(client, "aft winch", "accept")
    assert p["suggestions"] == [] and p["excluded_reasons"][0]["reason"] == "no_trusted_suggestion"
    r = client.post(GD, json={"group_key": "aft winch", "action": "accept", "location": "Aft Winch House", "expected_count": 2})
    assert (r.status_code, r.json()["code"]) == (409, "untrusted_suggestion")
    assert record(client, b).location_review == "pending"


def test_accept_uses_the_current_match_through_a_managed_synonym(client):
    a, b = bow_group(client)
    assert plan(client, BOW, "accept")["suggestions"] == []                            # nothing suggested yet
    assert create(client, "Bow mooring station", FCL).status_code == 201
    assert plan(client, BOW, "accept")["suggestions"] == [{"location": FCL, "count": 3}]
    r = apply(client, BOW, "accept", FCL)
    assert (r["resolved_via"], len(r["decided_rows"])) == ("managed_synonym", 3)
    assert {x.official_location for iid in (a, b) for x in recs(client, iid)} == {FCL}


def test_conflicting_suggestions_are_excluded_not_overridden(client):
    r = commit(client, workbook([row(1, "W43-0011.jpg", loc="BOAT DECK AREA", photo=jpeg_bytes(2201)),
                                 row(2, "W43-0012.jpg", loc="FUNNEL DECK AREA", photo=jpeg_bytes(2202))]),
               name="Week_43_extra_Image_Register.xlsx")
    approve_all(client, r.json()["import_id"])
    svc = client.app.state.imports
    svc.location_intel = LocationIntelligence(svc.settings, provider=MockLocationProvider(
        lambda qs, known: {"suggestions": [{"original": q.original, "location": "BOAT DECK AREA", "confidence": 0.95,
                                            "reason": "x"} for q in qs]}))
    with_ai = commit_deck(client, {4: "Funel dek arae"}, 2210)                         # rules FUNNEL vs AI BOAT
    svc.location_intel = LocationIntelligence(svc.settings, provider=MockLocationProvider(lambda qs, known: {"suggestions": []}))
    plain = commit_deck(client, {4: "funel  dek arae"}, 2220)                          # rules FUNNEL only
    p = plan(client, "funel dek arae", "accept", "FUNNEL DECK AREA")
    assert p["eligible_rows"] == [{"import_id": plain, "row_number": row_of(client, plain, "funel  dek arae")}]
    assert p["excluded"] == [{"import_id": with_ai, "row_number": row_of(client, with_ai, "Funel dek arae"),
                              "reason": "conflicting_suggestions"}]
    assert p["suggestions"] == [{"location": "FUNNEL DECK AREA", "count": 1}]
    p = plan(client, "funel dek arae", "accept", "BOAT DECK AREA")
    assert p["eligible_count"] == 0 and {x["reason"] for x in p["excluded"]} == {"conflicting_suggestions", "different_suggestion"}
    r = client.post(GD, json={"group_key": "funel dek arae", "action": "accept", "location": "BOAT DECK AREA", "expected_count": 0})
    assert (r.status_code, r.json()["code"]) == (409, "nothing_to_decide")
    apply(client, "funel dek arae", "accept", "FUNNEL DECK AREA", expected=1)
    assert record(client, with_ai).location_review == "pending"                        # decided only on its own


# ------------------------------------------------------------------ eligibility + lifecycle
def test_decided_rows_are_excluded(client):
    a, b = bow_group(client)
    decide(client, b, "bow mooring station", "keep_original")
    p = plan(client, BOW, "keep_original")
    assert p["eligible_count"] == 2
    assert p["excluded"] == [{"import_id": b, "row_number": row_of(client, b, "bow mooring station"), "reason": "not_pending"}]
    assert p["excluded_reasons"] == [{"reason": "not_pending", "text": "Already decided or matched automatically.", "count": 1}]
    apply(client, BOW, "manual", FCL)
    assert record(client, b).location_review == "kept_original"                       # earlier decision untouched


def test_withdrawn_imports_are_excluded_and_reinstated_ones_return(client):
    a, b = bow_group(client)
    withdraw(client, b)
    before_b = files(client).get(f"{b}/locations.json")
    p = plan(client, BOW, "keep_original")
    rb = row_of(client, b, "bow mooring station")
    assert p["eligible_count"] == 2 and p["excluded"] == [{"import_id": b, "row_number": rb, "reason": "import_withdrawn"}]
    apply(client, BOW, "keep_original")
    assert files(client).get(f"{b}/locations.json") == before_b                      # nothing written to a withdrawn import
    withdraw(client, b, "reinstate")
    p = plan(client, BOW, "keep_original")
    assert p["eligible_rows"] == [{"import_id": b, "row_number": rb}]


def test_stale_confirmation_is_refused_without_changes(client):
    a, b = bow_group(client)
    assert plan(client, BOW, "manual", FCL)["eligible_count"] == 3
    decide(client, b, "bow mooring station", "keep_original")                          # someone decided meanwhile
    before, before_recs = files(client), [(r.record_id, r.location_review) for r in client.app.state.store.records]
    r = client.post(GD, json={"group_key": BOW, "action": "manual", "location": FCL, "expected_count": 3})
    assert (r.status_code, r.json()["code"]) == (409, "group_changed") and "Nothing was changed" in r.json()["detail"]
    assert files(client) == before and [(x.record_id, x.location_review) for x in client.app.state.store.records] == before_recs
    assert audit(client)[0]["event"] == "location_group_decision_failed" and audit(client)[0]["details"]["code"] == "group_changed"


@pytest.mark.parametrize("body, status, code", [
    ({"group_key": "no such text", "action": "keep_original", "dry_run": True}, 404, "unknown_group"),
    ({"group_key": "   ", "action": "keep_original", "dry_run": True}, 400, "invalid_group"),
    ({"group_key": BOW, "action": "approve", "dry_run": True}, 400, "invalid_action"),
    ({"group_key": BOW, "action": "manual", "location": "x", "dry_run": True}, 400, "invalid_location"),
    ({"group_key": BOW, "action": "keep_original"}, 400, "expected_count_required"),
    ({"group_key": BOW, "action": "keep_original", "expected_count": 0}, 409, "group_changed"),
])
def test_invalid_requests_change_nothing(client, body, status, code):
    bow_group(client)
    before = files(client)
    r = client.post(GD, json=body)
    assert (r.status_code, r.json()["code"]) == (status, code), r.text
    assert files(client) == before


def test_reject_without_stored_suggestions_is_excluded(client):
    bow_group(client)
    p = plan(client, BOW, "reject")
    assert p["eligible_count"] == 0 and {x["reason"] for x in p["excluded"]} == {"no_suggestion"}


# ------------------------------------------------------------------ authorization
def test_only_administrators_can_preview_or_apply(client):
    bow_group(client)
    before = files(client)
    sign_in(client, "reviewer")
    for body in ({"group_key": BOW, "action": "keep_original", "dry_run": True},
                 {"group_key": BOW, "action": "keep_original", "expected_count": 3}):
        r = client.post(GD, json=body)
        assert (r.status_code, r.json()["code"]) == (403, "forbidden")
    client.headers.pop("Authorization")
    assert client.post(GD, json={"group_key": BOW, "action": "keep_original", "expected_count": 3}).status_code == 401
    assert files(client) == before


# ------------------------------------------------------------------ atomicity
def test_a_failed_write_rolls_back_every_import(client, monkeypatch):
    a, b = bow_group(client)
    decide(client, a, "Bow mooring station", "keep_original")                           # a has an existing file
    before, before_recs = files(client), [(r.record_id, r.official_location, r.location_review) for r in client.app.state.store.records]
    real, calls = service_module.write_atomic, []

    def flaky(folder, name, data):
        calls.append(folder.name)
        if len(calls) == 2:                                                           # the second import's write fails
            raise OSError("disk full")
        return real(folder, name, data)

    monkeypatch.setattr(service_module, "write_atomic", flaky)
    r = client.post(GD, json={"group_key": BOW, "action": "manual", "location": FCL, "expected_count": 2})
    assert (r.status_code, r.json()["code"]) == (500, "decision_write_failed")
    assert "every change was undone" in r.json()["detail"]
    assert len(calls) == 3                                                            # write a, fail b, restore a
    assert files(client) == before
    assert [(x.record_id, x.official_location, x.location_review) for x in client.app.state.store.records] == before_recs
    assert audit(client)[0]["event"] == "location_group_decision_failed"


def test_a_failed_reload_rolls_back_every_import(client, monkeypatch):
    a, b = bow_group(client)
    before = files(client)
    svc = client.app.state.imports
    real, state = svc._reload, {"n": 0}

    def flaky_reload():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("reload failed")
        return real()

    monkeypatch.setattr(svc, "_reload", flaky_reload)
    r = client.post(GD, json={"group_key": BOW, "action": "keep_original", "expected_count": 3})
    assert (r.status_code, r.json()["code"]) == (500, "decision_failed")
    assert files(client) == before                                                    # restored; new files removed
    assert not any(k.startswith((a, b)) for k in files(client))
    assert {x.location_review for iid in (a, b) for x in recs(client, iid)} == {"pending"}


# ------------------------------------------------------------------ AI + trust
def test_group_decisions_never_call_the_ai(client):
    bow_group(client)
    calls = MockLocationProvider(RuntimeError("group decisions must never call the AI"))
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings, provider=calls)
    plan(client, BOW, "accept")
    apply(client, BOW, "manual", FCL)
    assert calls.calls == []


def test_trust_rules_unchanged_after_group_decisions(client):
    _, b, _ = aft_setup(client)
    commit_deck(client, {4: "Galley store", 8: "galley STORE"}, 2300)
    apply(client, "galley store", "keep_original")
    apply(client, "aft winch", "reject")
    names = set(known(client).names)
    assert "Galley store" not in names and "galley STORE" not in names and "Aft winch" not in names
    assert "Aft Winch House" in names                                                 # approved locations stay trusted
