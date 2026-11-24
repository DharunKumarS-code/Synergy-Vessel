"""Functional-audit fix (Phase 1): an individual 'manual' location decision must resolve to a TRUSTED location (or an
active managed synonym). Untrusted text is refused unless the administrator explicitly confirms a new location.
Synthetic data and temporary folders only."""

from __future__ import annotations

import json

import pytest

from test_imports import commit, make  # noqa: F401
from test_location_intel import client, no_network  # noqa: F401  (trusted seed fixture + network guard)
from test_location_synonyms import FCL, create
from test_trusted_locations import commit_deck, known, row_of

TEXT = "Aft winch house"                                    # pending (unknown) in a fresh import


def post(c, iid, **body):
    return c.post(f"/api/imports/{iid}/locations", json={"row_number": row_of(c, iid, TEXT), "action": "manual", **body})


def record(c, iid):
    return next(r for r in c.app.state.store.records if r.import_id == iid)


def state(c, iid):
    folder = c.app.state.imports.dir / iid
    loc = folder / "locations.json"
    return (loc.read_bytes() if loc.exists() else None, set(known(c).names),
            (record(c, iid).official_location, record(c, iid).location_review))


@pytest.fixture
def pending(client):  # noqa: F811
    iid = commit_deck(client, {4: TEXT}, 910)
    assert record(client, iid).location_review == "pending"
    return client, iid


@pytest.mark.parametrize("text", ["NOT REAL", "UNKNOWN LOCATION", "Somewhere random aft", "Forecastle deck aera"])
def test_untrusted_manual_location_is_refused_and_changes_nothing(pending, text):
    c, iid = pending
    before = state(c, iid)
    r = post(c, iid, location=text)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "untrusted_location" and "not a trusted location" in body["detail"] and body["guidance"]
    assert state(c, iid) == before                     # no decision file change, no new trusted name, record untouched
    assert text not in known(c).names


def test_trusted_location_is_accepted_and_persists(pending):
    c, iid = pending
    r = post(c, iid, location=FCL)
    assert r.status_code == 200, r.text
    d = r.json()["decision"]
    assert (d["location"], d["new_location"], d["resolved_via"]) == (FCL, False, "trusted_location")
    assert (record(c, iid).official_location, record(c, iid).location_review) == (FCL, "manual")
    stored = json.loads((c.app.state.imports.dir / iid / "locations.json").read_text())
    assert [v["location"] for v in stored["decisions"].values()] == [FCL]


@pytest.mark.parametrize("typed", ["forecastle deck area", "  FORECASTLE   deck  AREA ", "Forecastle Deck Area"])
def test_normalised_spellings_of_a_trusted_location_resolve_to_it(pending, typed):
    c, iid = pending
    d = post(c, iid, location=typed).json()["decision"]
    assert (d["location"], d["new_location"]) == (FCL, False)


def test_active_managed_synonym_is_accepted_and_resolves_to_its_canonical(pending):
    c, iid = pending
    assert create(c, "Bow station", FCL).status_code == 201
    d = post(c, iid, location="bow  STATION").json()["decision"]
    assert (d["location"], d["resolved_via"], d["new_location"]) == (FCL, "managed_synonym", False)


def test_new_location_only_with_explicit_confirmation(pending):
    c, iid = pending
    assert post(c, iid, location="Aft Winch House").status_code == 422
    assert "Aft Winch House" not in known(c).names
    r = post(c, iid, location="Aft Winch House", confirm_new_location=True)
    assert r.status_code == 200, r.text
    assert r.json()["decision"]["new_location"] is True
    assert "Aft Winch House" in known(c).names        # approved -> trusted (unchanged Phase 10.2 behaviour)


def test_confirmation_is_harmless_for_trusted_locations(pending):
    c, iid = pending
    d = post(c, iid, location=FCL, confirm_new_location=True).json()["decision"]
    assert (d["location"], d["new_location"]) == (FCL, False)


def test_other_actions_and_length_rules_are_unchanged(pending):
    c, iid = pending
    assert post(c, iid, location="x").status_code == 400                                   # length check still first
    row = row_of(c, iid, TEXT)
    assert c.post(f"/api/imports/{iid}/locations", json={"row_number": row, "action": "keep_original"}).status_code == 200
    assert c.post(f"/api/imports/{iid}/locations",
                  json={"row_number": row, "action": "manual", "location": "x", "confirm_new_location": "maybe"}).status_code == 422


def test_refusal_is_audited_as_a_failed_decision(pending):
    c, iid = pending
    post(c, iid, location="NOT REAL")
    log = c.app.state.auth.audit
    last = log.entries()[0]
    assert last["event"] == "import_location_decision_failed" and last["details"]["code"] == "untrusted_location"
    assert log.verify().valid


def test_anonymous_users_still_cannot_decide(pending):            # reviewer -> 403: test_review_requires_admin
    c, iid = pending
    row = row_of(c, iid, TEXT)
    body = {"row_number": row, "action": "manual", "location": FCL}
    token = c.headers.pop("Authorization", None)
    try:
        assert c.post(f"/api/imports/{iid}/locations", json=body).status_code == 401
    finally:
        if token:
            c.headers["Authorization"] = token
