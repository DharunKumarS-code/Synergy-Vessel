"""Phase 10.3 -- administrator-managed location synonyms. Synthetic data and temporary folders only."""

from __future__ import annotations

import json

import pytest

from app.imports.service import ImportService
from app.imports.synonyms import SYNONYMS_FILE, SynonymFailure, SynonymStore
from app.location_intel.rules import KnownLocations, match
from conftest import sign_in
from test_imports import commit, make, preview  # noqa: F401
from test_location_intel import client, no_network  # noqa: F401  (trusted seed fixture + network guard)
from test_trusted_locations import SEED_NAMES, commit_deck, known, preview_li, row_of

URL = "/api/locations/synonyms"
FCL = "FORECASTLE DECK AREA"


def create(c, synonym, canonical):
    return c.post(URL, json={"synonym": synonym, "canonical": canonical})


def listing(c):
    r = c.get(URL)
    assert r.status_code == 200, r.text
    return r.json()


def syn_file(c):
    return c.app.state.imports.dir / SYNONYMS_FILE


def approve(c, iid, text, location):
    r = c.post(f"/api/imports/{iid}/locations",
               json={"row_number": row_of(c, iid, text), "action": "manual", "location": location,
                     "confirm_new_location": True})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ creation + matching
def test_admin_creates_a_synonym_and_future_imports_resolve_to_the_canonical(client):
    assert preview_li(client, {4: "Bow mooring station"}, 800)[0]["rule_method"] == "unknown"
    r = create(client, "  Bow   mooring station ", "forecastle deck area")
    assert r.status_code == 201, r.text
    s = r.json()["synonym"]
    assert (s["synonym"], s["canonical"]) == ("Bow mooring station", FCL)            # canonical stored in its trusted spelling
    assert s["id"].startswith("SYN-") and s["created_by"]["role"] == "admin"
    li = preview_li(client, {4: "BOW MOORING STATION", 8: "bow mooring  station."}, 810)
    for x in li:
        assert (x["rule_method"], x["normalized_location"], x["rule_confidence"], x["review_status"]) \
            == ("managed_synonym", FCL, 0.97, "auto_applied")
        assert x["applied_location"] == FCL and x["ai_status"] == "not_needed"
    iid = commit_deck(client, {4: "Bow Mooring Station"}, 820)
    rec = next(r for r in client.app.state.store.records if r.import_id == iid)
    assert (rec.original_location, rec.official_location, rec.location_method, rec.location_review) \
        == ("Bow Mooring Station", FCL, "managed_synonym", "auto_applied")
    assert "Bow Mooring Station" not in known(client)                                 # an auto-applied value adds no trust
    entry = listing(client)["synonyms"][0]
    assert entry["status"] == "active" and set(listing(client)["canonical_locations"]) >= SEED_NAMES


@pytest.mark.parametrize("synonym, canonical, status, code", [
    ("", FCL, 422, "empty_synonym"),
    ("   ", FCL, 422, "empty_synonym"),
    ("Bow station", "  ", 422, "empty_canonical"),
    ("N/A", FCL, 422, "invalid_synonym"),
    ("No.", FCL, 422, "invalid_synonym"),
    ("Not Labelled", FCL, 422, "invalid_synonym"),
    ("Bow station", "Galley store", 422, "unknown_canonical"),                       # not a trusted location
    ("forecastle  deck area", FCL, 409, "redundant"),                                # already normalized match
    ("Foc'sle", FCL, 409, "redundant"),                                              # already a built-in synonym
    ("Cargo hold no 2", "CARGO HOLD NO. 3", 409, "is_location"),                     # another existing location
    ("Hold 9 hatch", "CARGO HOLD NO. 3", 422, "number_mismatch"),
    ("Aft hold access", "CARGO HOLD NO. 3", 422, "number_mismatch"),                 # numbers must agree exactly
    ("Bow station 2", FCL, 422, "number_mismatch"),
])
def test_invalid_synonyms_are_rejected_and_nothing_is_written(client, synonym, canonical, status, code):
    r = create(client, synonym, canonical)
    assert (r.status_code, r.json()["code"]) == (status, code), r.text
    assert r.json()["detail"]
    assert not syn_file(client).exists()


def test_numbers_must_agree_and_never_cross(client):
    assert create(client, "No 3 hold access", "CARGO HOLD NO. 3").status_code == 201
    li = preview_li(client, {4: "No 3 hold access", 8: "No 2 hold access", 12: "No 9 hold access"}, 830)
    assert [(x["rule_method"], x["normalized_location"]) for x in li] == \
        [("managed_synonym", "CARGO HOLD NO. 3"), ("unknown", None), ("unknown", None)]


# ------------------------------------------------------------------ conflicts
def test_duplicates_and_conflicting_mappings_are_rejected_not_overwritten(client):
    assert create(client, "No 3 hold access", "CARGO HOLD NO. 3").status_code == 201
    before = syn_file(client).read_bytes()
    r = create(client, "NO 3  HOLD ACCESS", "cargo hold no 3")
    assert (r.status_code, r.json()["code"]) == (409, "duplicate")
    assert create(client, "Bow station", FCL).status_code == 201
    before = syn_file(client).read_bytes()
    r = create(client, "bow  STATION", "CARGO HOLD NO. 2")                           # one synonym -> two canonicals
    assert (r.status_code, r.json()["code"]) == (409, "conflict") and "FORECASTLE DECK AREA" in r.json()["detail"]
    assert syn_file(client).read_bytes() == before                                   # never silently overwritten
    assert [(s["synonym"], s["canonical"]) for s in listing(client)["synonyms"]] == \
        [("No 3 hold access", "CARGO HOLD NO. 3"), ("Bow station", FCL)]


def test_builtin_alias_conflict_and_validation_unit(tmp_path):
    k = KnownLocations(["BRIDGE DECK", FCL, "CARGO HOLD NO. 3"])
    with pytest.raises(SynonymFailure) as e:
        SynonymStore.validate("Nav bridge deck", FCL, k, [])                         # built-in alias of BRIDGE DECK
    assert e.value.code == "conflict" and "BRIDGE DECK" in e.value.message
    assert SynonymStore.validate(" Bow  station ", "forecastle deck area", k, []) == ("Bow station", FCL)


def test_circular_mapping_is_rejected_and_shadowed_synonyms_become_inactive(client):
    a = commit_deck(client, {4: "Stern store"}, 835)
    approve(client, a, "Stern store", "Stern Store")                                  # trusted canonical
    assert create(client, "Aft winch house", "Stern Store").status_code == 201        # text not (yet) a location
    iid = commit_deck(client, {4: "Aft winch house"}, 840)
    rec = next(r for r in client.app.state.store.records if r.import_id == iid)
    assert rec.location_method == "managed_synonym" and rec.official_location == "Stern Store"
    # (Phase 10.4: entering an ACTIVE synonym's wording resolves to its canonical -- see test_location_workbench.)
    # While the canonical is withdrawn the synonym is inactive, so the same wording becomes a real location ...
    body = {"reason": "shadowing test", "confirm_import_id": a}
    assert client.post(f"/api/imports/{a}/withdraw", json=body).status_code == 200
    approve(client, iid, "Aft winch house", "Aft winch house")
    assert client.post(f"/api/imports/{a}/reinstate", json=body).status_code == 200
    entry = listing(client)["synonyms"][0]                                             # ... and then shadows it
    assert entry["status"] == "inactive" and "existing location" in entry["status_reason"]
    li = preview_li(client, {4: "aft winch house"}, 850)[0]
    assert (li["rule_method"], li["normalized_location"]) == ("normalized", "Aft winch house")   # location wins
    r = create(client, "Rear winch hut", "Aft winch house")
    assert (r.status_code, r.json()["code"]) == (409, "circular")


# ------------------------------------------------------------------ precedence + determinism (rules unit)
def test_precedence_exact_normalized_managed_builtin_spelling():
    names = ["BRIDGE DECK", FCL, "CARGO HOLD NO. 3"]
    managed = {"Bow station": FCL, "bridge": FCL, "Bridge deck": FCL, "Hold 9": "CARGO HOLD NO. 3", "Galley": "GALLEY"}
    k = KnownLocations(names, managed=managed)
    assert match("BRIDGE DECK", k).method == "exact"
    assert match("bridge  deck", k).method == "normalized"                            # a location beats any synonym
    assert (match("bridge", k).method, match("bridge", k).normalized_location) == ("managed_synonym", FCL)  # beats built-in
    assert match("nav bridge deck", k).method == "synonym"                            # built-in aliases still work
    assert match("Bow statoin", k).method != "managed_synonym"                         # no fuzzy matching of synonyms
    assert "hold 9" not in k.managed_keys and "galley" not in k.managed_keys          # numbers / missing canonical
    shuffled = KnownLocations(list(reversed(names)), managed=dict(reversed(list(managed.items()))))
    assert shuffled.managed_keys == k.managed_keys


def test_existing_rules_unchanged_when_synonyms_exist(client):
    assert create(client, "Bow station", FCL).status_code == 201
    li = preview_li(client, {4: "Foc'sle", 8: "CARGO HOLD NO. 2", 12: "cargo hold no 3", 16: "Forecastle deck aera",
                             20: "Cargo Hold"}, 860)
    got = [(x["rule_method"], x["normalized_location"], x["rule_confidence"], x["review_status"]) for x in li]
    assert got == [("synonym", FCL, 0.95, "auto_applied"), ("exact", "CARGO HOLD NO. 2", 1.0, "not_required"),
                   ("normalized", "CARGO HOLD NO. 3", 0.98, "auto_applied"), ("spelling", FCL, 0.93, "auto_applied"),
                   ("ambiguous", None, 0.4, "pending")]


# ------------------------------------------------------------------ update / delete / persistence
def test_update_delete_and_persistence_across_restart(client):
    sid = create(client, "Bow station", FCL).json()["synonym"]["id"]
    r = client.put(f"{URL}/{sid}", json={"synonym": "Bow mooring station", "canonical": FCL})
    assert r.status_code == 200 and r.json()["synonym"]["synonym"] == "Bow mooring station"
    assert r.json()["synonym"]["created_at"] and r.json()["synonym"]["id"] == sid
    assert client.put(f"{URL}/{sid}", json={"synonym": "Bow mooring station", "canonical": FCL}).status_code == 200  # idempotent
    other = create(client, "No 3 hold access", "CARGO HOLD NO. 3").json()["synonym"]["id"]
    r = client.put(f"{URL}/{other}", json={"synonym": "Bow mooring station", "canonical": "CARGO HOLD NO. 3"})
    assert (r.status_code, r.json()["code"]) == (409, "conflict")
    assert client.put(f"{URL}/SYN-000000000000", json={"synonym": "x y", "canonical": FCL}).json()["code"] == "not_found"
    data = json.loads(syn_file(client).read_text(encoding="utf-8"))                    # persisted, atomically replaced
    assert data["version"] == 1 and len(data["synonyms"]) == 2
    assert not [p for p in syn_file(client).parent.iterdir() if p.name.endswith(".tmp")]
    svc = client.app.state.imports
    restarted = ImportService(svc.settings, svc._get_store, lambda: None)             # a fresh service = app restart
    assert [s["synonym"] for s in restarted.list_synonyms()["synonyms"]] == ["No 3 hold access", "Bow mooring station"]
    assert client.delete(f"{URL}/{sid}").json()["deleted"]["synonym"] == "Bow mooring station"
    assert client.delete(f"{URL}/{sid}").status_code == 404
    assert preview_li(client, {4: "Bow mooring station"}, 870)[0]["rule_method"] == "unknown"
    audit = client.get("/api/admin/audit?limit=50").text
    for ev in ("location_synonym_created", "location_synonym_updated", "location_synonym_deleted",
               "location_synonym_update_failed", "location_synonym_delete_failed"):
        assert ev in audit


def test_unreadable_file_disables_synonyms_and_blocks_changes(client):
    assert create(client, "Bow station", FCL).status_code == 201
    syn_file(client).write_text("{bad", encoding="utf-8")
    assert preview_li(client, {4: "Bow station"}, 880)[0]["rule_method"] == "unknown"   # fail safe
    q = listing(client)
    assert q["synonyms"] == [] and q["problems"]
    r = create(client, "Bow mooring station", FCL)
    assert (r.status_code, r.json()["code"]) == (409, "synonyms_unreadable")
    assert syn_file(client).read_text(encoding="utf-8") == "{bad"


# ------------------------------------------------------------------ authorization
def test_only_administrators_can_manage_synonyms(client):
    sid = create(client, "Bow station", FCL).json()["synonym"]["id"]
    before = syn_file(client).read_bytes()
    sign_in(client, "reviewer")
    for r in (client.get(URL), create(client, "Bow mooring station", FCL),
              client.put(f"{URL}/{sid}", json={"synonym": "X station", "canonical": FCL}), client.delete(f"{URL}/{sid}")):
        assert (r.status_code, r.json()["code"]) == (403, "forbidden")
    client.headers.pop("Authorization")
    assert (create(client, "Bow mooring station", FCL).status_code, client.get(URL).status_code) == (401, 401)
    assert syn_file(client).read_bytes() == before


# ------------------------------------------------------------------ Phase 10.2 trust rules intact
def test_untrusted_text_never_becomes_a_canonical_or_a_synonym(client):
    iid = commit_deck(client, {4: "Galley store", 8: "Funnel deck aera"}, 890)          # pending, untrusted
    client.post(f"/api/imports/{iid}/locations", json={"row_number": row_of(client, iid, "Funnel deck aera"),
                                                       "action": "keep_original"})
    for canon in ("Galley store", "Funnel deck aera"):
        assert create(client, "Some other text", canon).json()["code"] == "unknown_canonical"
    assert not syn_file(client).exists()                                              # reviews never create synonyms
    assert not {"Galley store", "Funnel deck aera"} & set(known(client).names)


def test_synonyms_follow_the_canonicals_import_lifecycle(client):
    iid = commit_deck(client, {4: "Aft winch house"}, 900)
    approve(client, iid, "Aft winch house", "Aft Winch House")                        # approved -> trusted
    assert create(client, "Rear winch hut", "aft winch house").json()["synonym"]["canonical"] == "Aft Winch House"
    assert preview_li(client, {4: "Rear winch hut"}, 910)[0]["normalized_location"] == "Aft Winch House"
    body = {"reason": "synonym lifecycle test", "confirm_import_id": iid}
    assert client.post(f"/api/imports/{iid}/withdraw", json=body).status_code == 200
    assert listing(client)["synonyms"][0]["status"] == "inactive"
    assert preview_li(client, {4: "Rear winch hut"}, 920)[0]["rule_method"] == "unknown"
    assert client.post(f"/api/imports/{iid}/reinstate", json=body).status_code == 200
    assert listing(client)["synonyms"][0]["status"] == "active"
    assert preview_li(client, {4: "Rear winch hut"}, 930)[0]["rule_method"] == "managed_synonym"
