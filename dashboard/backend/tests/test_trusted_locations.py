"""Phase 10.2 -- only trusted locations drive automatic matching.

Bug reproduced here: an unreviewed (pending) imported Excel text used to enter the known-location list, so the same text
in a later import was "normalized" and applied automatically to a name no human had approved. Synthetic data only."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.imports.service import TRUSTED_IMPORT_REVIEWS, trusted_location_names
from app.location_intel.providers import LocationQuery, validate_response
from app.location_intel.rules import match
from conftest import jpeg_bytes
from test_deck_photos_import import BANNER, DECK_NAME, deck, rows_of
from test_imports import commit, make, preview  # noqa: F401
from test_location_intel import client, no_network  # noqa: F401  (trusted seed fixture + network guard)

SEED_NAMES = {"FORECASTLE DECK AREA", "CARGO HOLD NO. 3", "CARGO HOLD NO. 2"}


def sheet(texts: dict, seed: int, date="NOV 20 2026 "):
    return deck([(date, BANNER, texts, [(f"A{r + 1}", jpeg_bytes(seed + r)) for r in texts])])


def commit_deck(c, texts, seed):
    r = commit(c, sheet(texts, seed), name=DECK_NAME)
    assert r.status_code == 200, r.text
    return r.json()["import_id"]


def known(c):
    return c.app.state.imports._known_locations()


def preview_li(c, texts, seed):
    p = preview(c, sheet(texts, seed, date="NOV 21 2026 "), name=DECK_NAME).json()
    return [r["location_intel"] for r in rows_of(p)]


def row_of(c, iid, text):
    return next(r["row_number"] for r in c.get(f"/api/imports/{iid}").json()["rows"] if r["official_location"] == text)


# ------------------------------------------------------------------ A: the bug
def test_a_pending_location_never_becomes_trusted(client):
    iid = commit_deck(client, {4: "Aft winch house"}, 600)
    rec = next(r for r in client.app.state.store.records if r.import_id == iid)
    assert (rec.official_location, rec.location_review) == ("Aft winch house", "pending")
    assert "Aft winch house" not in known(client) and "aft winch house" not in known(client)
    li = preview_li(client, {4: "AFT   winch HOUSE"}, 610)[0]
    assert li["rule_method"] == "unknown" and li["review_status"] == "pending"           # was: normalized + auto_applied
    assert li["applied_location"] == "AFT winch HOUSE" and li["normalized_location"] is None


@pytest.mark.parametrize("action", ["reject", "keep_original"])
def test_a_rejected_or_kept_original_text_is_not_trusted(client, action):
    iid = commit_deck(client, {4: "Funnel deck aera"}, 620)                              # spelling candidate, pending
    row = row_of(client, iid, "Funnel deck aera")
    if action == "reject":
        assert client.post(f"/api/imports/{iid}/locations", json={"row_number": row, "action": "reject"}).json()["code"] \
            == "no_suggestion"                                                           # FUNNEL DECK AREA is not trusted
        action = "keep_original"
    assert client.post(f"/api/imports/{iid}/locations", json={"row_number": row, "action": action}).status_code == 200
    assert "Funnel deck aera" not in known(client)


# ------------------------------------------------------------------ B: approved locations stay trusted
def test_b_manually_approved_location_is_trusted_and_matched(client):
    iid = commit_deck(client, {4: "Aft winch house"}, 630)
    r = client.post(f"/api/imports/{iid}/locations",
                    json={"row_number": row_of(client, iid, "Aft winch house"), "action": "manual", "location": "Aft Winch House",
                          "confirm_new_location": True})
    assert r.status_code == 200 and r.json()["decision"]["new_location"] is True
    assert "Aft Winch House" in known(client)
    li = preview_li(client, {4: "AFT   winch HOUSE"}, 640)[0]
    assert (li["rule_method"], li["applied_location"], li["review_status"]) == ("normalized", "Aft Winch House", "auto_applied")


def test_b_accepted_suggestion_is_trusted(client):
    iid = commit_deck(client, {4: "Forecastle deck aera hatch"}, 650)
    row = row_of(client, iid, "Forecastle deck aera hatch")
    assert client.post(f"/api/imports/{iid}/locations",
                       json={"row_number": row, "action": "manual", "location": "Forecastle Hatch",
                             "confirm_new_location": True}).status_code == 200
    iid2 = commit_deck(client, {4: "forecastle   HATCH"}, 660)                          # auto-applied to the approved name
    rec = next(r for r in client.app.state.store.records if r.import_id == iid2)
    assert (rec.official_location, rec.location_review) == ("Forecastle Hatch", "auto_applied")


# ------------------------------------------------------------------ C: source separation (unit)
def test_c_trusted_sources_are_history_and_human_approved_imports_only():
    recs = [NS(source="image_register", location_review=None, official_location="BRIDGE DECK"),
            NS(source="json_report", location_review=None, official_location="MONKEY ISLAND"),
            *[NS(source="excel_import", location_review=s, official_location=f"IMPORT {s}") for s in
              ("accepted", "manual", "pending", "rejected", "kept_original", "auto_applied", "not_required", None)]]
    assert list(trusted_location_names(recs)) == ["BRIDGE DECK", "MONKEY ISLAND", "IMPORT accepted", "IMPORT manual"]
    assert TRUSTED_IMPORT_REVIEWS == {"accepted", "manual"}


def test_c_known_list_keeps_approved_canonical_names_and_drops_pending(client):
    commit_deck(client, {4: "Aft winch house", 8: "Galley store"}, 670)
    names = set(known(client).names)
    assert SEED_NAMES <= names and not {"Aft winch house", "Galley store"} & names


# ------------------------------------------------------------------ D: lifecycle
def test_d_withdrawn_imports_contribute_nothing_and_reinstated_ones_keep_their_approvals(client):
    iid = commit_deck(client, {4: "Aft winch house"}, 680)
    client.post(f"/api/imports/{iid}/locations",
                json={"row_number": row_of(client, iid, "Aft winch house"), "action": "manual", "location": "Aft Winch House",
                          "confirm_new_location": True})
    assert "Aft Winch House" in known(client)
    body = {"reason": "trusted-source test", "confirm_import_id": iid}
    assert client.post(f"/api/imports/{iid}/withdraw", json=body).status_code == 200
    assert "Aft Winch House" not in known(client)
    assert client.post(f"/api/imports/{iid}/reinstate", json=body).status_code == 200
    assert "Aft Winch House" in known(client)                                            # decisions survive reinstatement


# ------------------------------------------------------------------ E: number safety
def test_e_numbers_still_never_cross(client):
    commit_deck(client, {4: "Cargo Hold 9"}, 690)                                       # pending, untrusted
    k = known(client)
    assert "Cargo Hold 9" not in k
    for text in ("Cargo Hold 9", "Hold 9 hatch"):
        assert match(text, k).method == "unknown"                                      # never hold 2 / hold 3
    found, problems = validate_response({"suggestions": [{"original": "Cargo Hold 9", "location": "CARGO HOLD NO. 3",
                                                          "confidence": 0.9}]}, [LocationQuery("Cargo Hold 9")], k)
    assert found == {} and "number" in problems[0]


# ------------------------------------------------------------------ F: rule behaviour for trusted names unchanged
def test_f_rules_unchanged_for_trusted_locations(client):
    li = preview_li(client, {4: "Foc'sle", 8: "CARGO HOLD NO. 2", 12: "cargo hold no 3", 16: "Forecastle deck aera",
                             20: "Cargo Hold"}, 700)
    got = [(x["rule_method"], x["normalized_location"], x["rule_confidence"], x["review_status"]) for x in li]
    assert got == [("synonym", "FORECASTLE DECK AREA", 0.95, "auto_applied"),
                   ("exact", "CARGO HOLD NO. 2", 1.0, "not_required"),
                   ("normalized", "CARGO HOLD NO. 3", 0.98, "auto_applied"),
                   ("spelling", "FORECASTLE DECK AREA", 0.93, "auto_applied"),
                   ("ambiguous", None, 0.4, "pending")]
