"""Phase 10.4 -- workbench improvements: synonym-aware decisions (R1) and the live current match in the queue (R2).
Synthetic data and temporary folders only."""

from __future__ import annotations

from app.location_intel.providers import MockLocationProvider
from app.location_intel.rules import KnownLocations
from app.location_intel.service import LocationIntelligence
from test_imports import commit, make, preview, tree  # noqa: F401
from test_location_intel import client, no_network  # noqa: F401  (trusted seed fixture + network guard)
from test_location_synonyms import FCL, URL, approve, create, listing
from test_trusted_locations import commit_deck, known, row_of

QUEUE = "/api/locations/review"


def decide(c, iid, text, action, location=None, new=False):
    body = {"row_number": row_of(c, iid, text), "action": action, **({"location": location} if location else {}),
            **({"confirm_new_location": True} if new else {})}
    r = c.post(f"/api/imports/{iid}/locations", json=body)
    assert r.status_code == 200, r.text
    return r.json()["decision"]


def record(c, iid):
    return next(r for r in c.app.state.store.records if r.import_id == iid)


def group(c, text):
    q = c.get(QUEUE)
    assert q.status_code == 200, q.text
    return next((g for g in q.json()["groups"] if g["display_text"] == text), None)


def withdraw(c, iid, action="withdraw"):
    body = {"reason": "workbench test", "confirm_import_id": iid}
    assert c.post(f"/api/imports/{iid}/{action}", json=body).status_code == 200


# ------------------------------------------------------------------ R1: resolver (unit)
def test_resolve_trusted_precedence_and_safety():
    names = [FCL, "CARGO HOLD NO. 3", "Bow station", "BRIDGE DECK"]
    managed = {"Bow mooring station": FCL, "Bow station": "CARGO HOLD NO. 3",       # shadowed by a real location
               "Hold 9 access": "CARGO HOLD NO. 3",                                 # numbers disagree (hand-edited)
               "Galley": "GALLEY STORE",                                            # canonical not trusted
               "Stern station": FCL, "STERN  station": "BRIDGE DECK"}          # conflicting (hand-edited)
    k = KnownLocations(names, managed=managed)
    assert k.resolve_trusted("bow  MOORING station") == (FCL, "managed_synonym")
    assert k.resolve_trusted("BOW STATION") == ("Bow station", "trusted_location")  # real location wins
    assert k.resolve_trusted("forecastle deck area") == (FCL, "trusted_location")
    for text in ("Hold 9 access", "Galley", "Stern station", "", None, "Unknown place"):
        assert k.resolve_trusted(text) == (None, None)
    assert "stern station" not in k.managed_keys                                    # conflicts match nothing either
    assert k.resolve("Bow mooring station") is None                                 # AI-validation resolver unchanged


# ------------------------------------------------------------------ R1: decisions (API)
def test_manual_entry_of_a_synonym_resolves_to_its_canonical(client):
    sid = create(client, "Bow mooring station", FCL).json()["synonym"]["id"]
    iid = commit_deck(client, {4: "Mooring point fwd"}, 1000)
    d = decide(client, iid, "Mooring point fwd", "manual", "BOW  mooring station")
    assert (d["location"], d["new_location"], d["resolved_via"], d["entered_location"]) == \
        (FCL, False, "managed_synonym", "BOW mooring station")
    rec = record(client, iid)
    assert (rec.original_location, rec.official_location, rec.location_review) == ("Mooring point fwd", FCL, "manual")
    assert "BOW mooring station" not in known(client)                               # no duplicate trusted location
    assert next(s for s in listing(client)["synonyms"] if s["id"] == sid)["status"] == "active"


def test_manual_entry_still_prefers_real_locations_and_creates_new_ones_explicitly(client):
    iid = commit_deck(client, {4: "Mooring point fwd", 8: "Galley store"}, 1010)
    d = decide(client, iid, "Mooring point fwd", "manual", "forecastle deck area")
    assert (d["location"], d["new_location"], d["resolved_via"]) == (FCL, False, "trusted_location")
    d = decide(client, iid, "Galley store", "manual", "Hold 9 galley", new=True)             # unknown -> new location, as before
    assert (d["location"], d["new_location"], d["resolved_via"]) == ("Hold 9 galley", True, "new_location")
    assert "Hold 9 galley" in known(client)                                         # 10.2: manual approval is trusted


def test_accepting_a_suggestion_that_is_now_a_synonym_resolves_to_its_canonical(client):
    a = commit_deck(client, {4: "Aft winch house"}, 1020)
    approve(client, a, "Aft winch house", "Aft Winch House")                       # trusted
    b = commit_deck(client, {4: "Aft winch"}, 1030)                                 # partial -> pending, candidate
    assert client.get(f"/api/imports/{b}").json()["rows"][0]["location_intel"]["rule_candidate"] == "Aft Winch House"
    withdraw(client, a)                                                             # candidate no longer trusted
    assert create(client, "Aft Winch House", FCL).status_code == 201                # admin maps the wording
    d = decide(client, b, "Aft winch", "accept")
    assert (d["suggestion"], d["location"], d["resolved_via"]) == ("Aft Winch House", FCL, "managed_synonym")
    assert record(client, b).official_location == FCL
    assert listing(client)["synonyms"][0]["status"] == "active"
    assert "Aft Winch House" not in known(client)


def test_accept_without_a_synonym_keeps_existing_behaviour(client):
    a = commit_deck(client, {4: "Aft winch house"}, 1040)
    approve(client, a, "Aft winch house", "Aft Winch House")
    b = commit_deck(client, {4: "Aft winch"}, 1050)
    d = decide(client, b, "Aft winch", "accept")
    assert (d["location"], d["resolved_via"]) == ("Aft Winch House", "trusted_location")


# ------------------------------------------------------------------ R2: live current match (read-only)
def test_current_match_follows_synonym_create_edit_delete(client):
    commit_deck(client, {4: "Bow mooring station"}, 1100)
    g = group(client, "Bow mooring station")
    assert g["current_match"] is None and g["rule_methods"] == [{"value": "unknown", "count": 1}]
    sid = create(client, "Bow mooring station", FCL).json()["synonym"]["id"]
    cm = group(client, "Bow mooring station")["current_match"]
    assert (cm["location"], cm["method"], cm["band"], cm["would_auto_apply"]) == (FCL, "managed_synonym", "high", True)
    assert group(client, "Bow mooring station")["rule_methods"] == [{"value": "unknown", "count": 1}]  # stored kept
    assert client.put(f"{URL}/{sid}", json={"synonym": "Bow mooring point", "canonical": FCL}).status_code == 200
    assert group(client, "Bow mooring station")["current_match"] is None
    assert client.put(f"{URL}/{sid}", json={"synonym": "Bow mooring station", "canonical": "CARGO HOLD NO. 2"}).json()[
        "code"] == "number_mismatch"                                                 # numbers still enforced
    assert client.put(f"{URL}/{sid}", json={"synonym": "Bow mooring station", "canonical": FCL}).status_code == 200
    assert group(client, "Bow mooring station")["current_match"]["location"] == FCL
    assert client.delete(f"{URL}/{sid}").status_code == 200
    assert group(client, "Bow mooring station")["current_match"] is None
    assert group(client, "Bow mooring station")["pending_count"] == 1               # never decided automatically


def test_current_match_follows_the_canonicals_trust_and_import_lifecycle(client):
    a = commit_deck(client, {4: "Aft winch house"}, 1110)
    approve(client, a, "Aft winch house", "Aft Winch House")
    commit_deck(client, {4: "Rear winch hut"}, 1120)
    assert create(client, "Rear winch hut", "Aft Winch House").status_code == 201
    assert group(client, "Rear winch hut")["current_match"]["location"] == "Aft Winch House"
    withdraw(client, a)                                                             # canonical no longer trusted
    assert group(client, "Rear winch hut")["current_match"] is None
    withdraw(client, a, "reinstate")
    assert group(client, "Rear winch hut")["current_match"]["method"] == "managed_synonym"


def test_current_match_shows_a_location_approved_after_import(client):
    b = commit_deck(client, {4: "Galley store"}, 1130)                              # pending, nothing to match
    assert group(client, "Galley store")["current_match"] is None
    c2 = commit_deck(client, {8: "Galley store"}, 1140)
    decide(client, c2, "Galley store", "manual", "Galley store", new=True)                   # approved -> trusted
    g = group(client, "Galley store")
    assert g["occurrences"][0]["import_id"] == b and g["pending_count"] == 1
    assert (g["current_match"]["location"], g["current_match"]["method"]) == ("Galley store", "exact")
    withdraw(client, b)
    assert group(client, "Galley store") is None


def test_current_match_hidden_when_unchanged_and_numbers_never_cross(client):
    commit_deck(client, {4: "Funnel deck aera", 8: "Cargo Hold 9"}, 1150)          # spelling candidate is not trusted
    assert group(client, "Cargo Hold 9")["current_match"] is None
    a = commit_deck(client, {4: "Aft winch house"}, 1160)
    approve(client, a, "Aft winch house", "Aft Winch House")
    commit_deck(client, {4: "Aft winch"}, 1170)                                     # stored partial == current partial
    assert group(client, "Aft winch")["current_match"] is None


def test_queue_with_current_match_is_read_only_and_never_calls_ai(client, tmp_path):
    iid = commit_deck(client, {4: "Bow mooring station"}, 1180)
    assert create(client, "Bow mooring station", FCL).status_code == 201
    calls = MockLocationProvider(RuntimeError("the queue must never call the AI"))
    client.app.state.imports.location_intel = LocationIntelligence(client.app.state.imports.settings, provider=calls)
    records = [(r.record_id, r.official_location, r.location_review) for r in client.app.state.store.records]
    audit_before = len(client.app.state.auth.audit.entries(limit=1000))
    before = tree(tmp_path)
    for _ in range(2):
        assert group(client, "Bow mooring station")["current_match"]["location"] == FCL
    assert tree(tmp_path) == before and calls.calls == []
    assert [(r.record_id, r.official_location, r.location_review) for r in client.app.state.store.records] == records
    assert len(client.app.state.auth.audit.entries(limit=1000)) == audit_before
    assert not (client.app.state.imports.dir / iid / "locations.json").exists()
    assert record(client, iid).location_review == "pending"
