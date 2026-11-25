"""SV-08 -- physical viewpoint mapping: generator, validation, API, administrator review, AI gate, backup.

A synthetic vessel with two workbook layout eras (no real data is touched, no live AI):

* Era A (weeks 30, 31, 34): serials 1-10. #1 Not Labelled (AI "Main Deck"), #2 Main Deck, #3/#4 CARGO HOLD NO. 1,
  #5-#10 filler locations. Week 31 #2 reuses Week 30 #2's file (not retaken). Week 34 #10 is relabelled
  "Bosun Store" (label inconsistency, count change). Week 30's register adds a row that reuses #1's file under
  "PORT AND STARBOARD VIEW" (identical file, different label).
* Era B (weeks 38, 39): renumbered. #1 Main Deck, #2 Not Labelled, #3/#4 CARGO HOLD NO. 1, #5-#9 fillers
  (Location 9 is gone), #10 "Main Deck Area" (similar name: never merged with Main Deck).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import backup as bk
from app import config
from app.ai.providers import MockConditionProvider
from app.cli import main as cli_main
from app.config import Settings
from app.main import create_app
from app.services import viewpoints as vp
from app.services.inspection_store import InspectionStore
from conftest import IMG_DIR, TEST_PASSWORD, XLSX_A, _img, _rec, _register, _report, jpeg_bytes

DATES = {30: "8/2/2026", 31: "8/9/2026", 34: "8/30/2026", 38: "9/27/2026", 39: "10/4/2026"}
ERA_A = {1: ("Not Labelled", "Main Deck"), 2: ("Main Deck", None), 3: ("CARGO HOLD NO. 1", None),
         4: ("CARGO HOLD NO. 1", None), **{s: (f"Location {s}", None) for s in range(5, 11)}}
ERA_B = {1: ("Main Deck", None), 2: ("Not Labelled", "Main Deck"), 3: ("CARGO HOLD NO. 1", None),
         4: ("CARGO HOLD NO. 1", None), **{s: (f"Location {s}", None) for s in range(5, 9)},
         10: ("Main Deck Area", None)}


def _build(root: Path) -> Path:
    seed = 100
    shas: dict[tuple[int, int], str] = {}
    for week in (30, 31, 34, 38, 39):
        folder = root / f"Week {week}"
        layout = dict(ERA_A if week < 38 else ERA_B)
        if week == 34:
            layout[10] = ("Bosun Store", None)
        recs = []
        for serial, (loc, ai) in sorted(layout.items()):
            if week == 31 and serial == 2:
                payload = jpeg_bytes(100 + 2)       # Week 30 #2 reused: identical file (not retaken)
            else:
                seed += 1
                payload = jpeg_bytes(seed if not (week == 30 and serial == 2) else 102)
            sha = _img(folder / IMG_DIR, f"W{week}-{serial:04d}.jpg", payload)
            shas[(week, serial)] = sha
            recs.append(_rec(serial, f"p{week}_{serial:03d}.jpg", DATES[week], XLSX_A, "WS", loc, ai, sha))
        _report(folder, f"Week {week}", recs)
        rows = [(s, f"W{week}-{s:04d}.jpg", DATES[week], XLSX_A, "WS", None if loc == "Not Labelled" else loc)
                for s, (loc, _) in sorted(layout.items())]
        if week == 30:
            (folder / IMG_DIR / "W30-0011.jpg").write_bytes((folder / IMG_DIR / "W30-0001.jpg").read_bytes())
            rows.append((11, "W30-0011.jpg", DATES[week], XLSX_A, "WS", "PORT AND STARBOARD VIEW"))
        _register(folder, week, rows)
    return root


@pytest.fixture(scope="module")
def vroot(tmp_path_factory) -> Path:
    return _build(tmp_path_factory.mktemp("sv08"))


@pytest.fixture(scope="module")
def vsettings(vroot, tmp_path_factory) -> Settings:
    base = tmp_path_factory.mktemp("sv08app")
    return Settings(data_root=vroot, analysis_dir=base / "analyses", import_dir=base / "imports",
                    auth_dir=base / "auth", viewpoint_dir=base / "locations", viewpoint_ai_gate=True)


@pytest.fixture(scope="module")
def vstore(vsettings) -> InspectionStore:
    return InspectionStore.build(vsettings)


@pytest.fixture(scope="module")
def mapping(vstore, vsettings) -> dict:
    return vp.generate(vstore.records, vsettings.unlabelled_placeholder, created_at="2026-11-24T00:00:00+00:00")


def _members(data: dict) -> dict[str, tuple[dict, dict]]:
    return {m["record_id"]: (v, m) for v in data["viewpoints"] for m in v["members"]}


def _vp_of(data: dict, record_id: str) -> dict:
    return _members(data)[record_id][0]


def _tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


# =========================================================================== generator
def test_layout_eras_are_detected(mapping):
    assert [e["weeks"] for e in mapping["generated"]["eras"]] == [[30, 31, 34], [38, 39]]


def test_every_record_is_in_exactly_one_viewpoint(mapping, vstore):
    ids = [m["record_id"] for v in mapping["viewpoints"] for m in v["members"]]
    assert sorted(ids) == sorted(r.record_id for r in vstore.records)
    assert len(ids) == len(set(ids))
    assert vp.validate_mapping(mapping, vstore.by_id).ok


def test_nothing_is_approved_automatically(mapping):
    statuses = {m["status"] for v in mapping["viewpoints"] for m in v["members"]}
    assert statuses <= {"proposed", "needs_review"}
    assert not vp.has_decisions(mapping)


def test_serial_numbers_match_only_inside_one_layout_era(mapping):
    # Week 30 #3 and Week 38 #3 are both "CARGO HOLD NO. 1" but in different layouts: the link is a proposal.
    hold = _vp_of(mapping, "W30-0003")
    w38 = next(m for m in hold["members"] if m["week"] == 38)
    assert w38["method"] == "proposed_cross_layout" and w38["status"] == "needs_review" and "cross_layout" in w38["flags"]
    inside = [m for m in hold["members"] if m["week"] in (31, 34)]
    assert {m["method"] for m in inside} == {"same_serial_within_layout"}
    assert all(m["serial"] == 3 for m in hold["members"] if m["week"] < 38)


def test_cross_layout_chain_follows_position_within_the_location(mapping):
    a = _vp_of(mapping, "W34-0003")
    b = _vp_of(mapping, "W34-0004")
    assert a is not b
    assert {m["record_id"] for m in a["members"] if m["week"] == 38} == {"W38-0003"}
    assert {m["record_id"] for m in b["members"] if m["week"] == 38} == {"W38-0004"}
    # Main Deck: era A #2 continues as era B #1 (renumbered), only as a needs_review proposal.
    md = _vp_of(mapping, "W30-0002")
    assert {m["record_id"] for m in md["members"] if m["week"] == 38} == {"W38-0001"}


def test_identical_file_is_a_repeat_not_a_new_photograph(mapping):
    md = _vp_of(mapping, "W31-0002")
    w31 = next(m for m in md["members"] if m["record_id"] == "W31-0002")
    assert w31["method"] == "identical_file"
    assert any("byte-identical" in e for e in w31["evidence"])


def test_identical_file_under_a_different_label_is_flagged_not_merged(mapping):
    register = _vp_of(mapping, "W30-0011")
    unlabelled = _vp_of(mapping, "W30-0001")
    assert register is not unlabelled
    assert register["official_location"] == "PORT AND STARBOARD VIEW"
    assert "identical_file_different_label" in _members(mapping)["W30-0011"][1]["flags"]
    assert "identical_file_different_label" in _members(mapping)["W30-0001"][1]["flags"]
    assert any(f["type"] == "identical_file_different_label" and set(f["record_ids"]) >= {"W30-0001", "W30-0011"}
               for f in mapping["findings"])


def test_label_inconsistency_and_count_change_are_flagged(mapping):
    types = {f["type"] for f in mapping["findings"]}
    assert {"label_inconsistency", "location_count_changed"} <= types
    m = _members(mapping)["W34-0010"][1]
    assert m["status"] == "needs_review" and "label_inconsistency" in m["flags"]
    # The relabelled photo is its own viewpoint: never merged into "Location 10".
    assert _vp_of(mapping, "W34-0010") is not _vp_of(mapping, "W31-0010")


def test_similar_names_and_not_labelled_are_never_merged(mapping):
    assert _vp_of(mapping, "W38-0010")["official_location"] == "Main Deck Area"
    assert _vp_of(mapping, "W38-0010") is not _vp_of(mapping, "W38-0001")
    nl = _vp_of(mapping, "W30-0001")
    assert nl["official_location"] == "Not Labelled"
    assert all(m["official_location"] == "Not Labelled" for m in nl["members"])   # original label kept
    assert {m["week"] for m in nl["members"]} == {30, 31, 34, 38, 39}             # still available every week


def test_unmatched_cross_layout_location_is_reported(mapping):
    # "Location 9" exists only in era A; nothing in era B continues it.
    assert _vp_of(mapping, "W34-0009")["members"][-1]["week"] == 34
    assert {m["week"] for m in _vp_of(mapping, "W38-0010")["members"]} == {38, 39}


def test_generation_is_deterministic(vstore, vsettings, mapping):
    again = vp.generate(vstore.records, vsettings.unlabelled_placeholder, created_at="2026-11-24T00:00:00+00:00")
    assert again == mapping


# =========================================================================== validation
def test_validation_rejects_double_membership_paths_and_unknown_records(mapping, vstore):
    bad = json.loads(json.dumps(mapping))
    bad["viewpoints"][1]["members"].append(dict(bad["viewpoints"][0]["members"][0]))
    assert any("active member of both" in e for e in vp.validate_mapping(bad, vstore.by_id).errors)
    bad = json.loads(json.dumps(mapping))
    bad["viewpoints"][0]["members"][0]["notes"] = "C:\\Users\\someone\\photo.jpg"
    assert any("absolute" in e for e in vp.validate_mapping(bad).errors)
    bad = json.loads(json.dumps(mapping))
    bad["viewpoints"][0]["members"][0]["record_id"] = "W99-0001"
    assert any("not in the inspection inventory" in e for e in vp.validate_mapping(bad, vstore.by_id).errors)
    bad = json.loads(json.dumps(mapping))
    bad["viewpoints"][0]["members"][0]["status"] = "auto_approved"
    assert not vp.validate_mapping(bad).ok


# =========================================================================== CLI
def test_cli_generates_writes_report_and_never_touches_historical_files(vroot, vsettings, monkeypatch, tmp_path, capsys):
    before = _tree_hash(vroot)
    out = tmp_path / "vp"
    for k, v in {"SYNERGY_DATA_ROOT": vroot, "SYNERGY_AUTH_DIR": tmp_path / "auth",
                 "SYNERGY_VIEWPOINT_DIR": tmp_path / "unused"}.items():
        monkeypatch.setenv(k, str(v))
    assert cli_main(["generate-viewpoints", "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out and not (tmp_path / "unused").exists()
    assert cli_main(["generate-viewpoints", "--out", str(out)]) == 0
    data = json.loads((out / vp.MAPPING_FILE).read_text(encoding="utf-8"))
    assert vp.validate_mapping(data).ok
    report = (out / vp.REPORT_FILE).read_text(encoding="utf-8")
    assert "layout era" in report.lower() and "Nothing in this mapping is approved automatically" in report
    assert str(vroot) not in json.dumps(data)                                  # no file-system paths
    assert _tree_hash(vroot) == before                                         # historical data unchanged
    # An existing proposal-only mapping is replaced only on request; a mapping with decisions never is.
    assert cli_main(["generate-viewpoints", "--out", str(out)]) == 3
    assert cli_main(["generate-viewpoints", "--out", str(out), "--replace-proposals"]) == 0
    data = json.loads((out / vp.MAPPING_FILE).read_text(encoding="utf-8"))
    assert data["revision"] == 2
    data["viewpoints"][0]["members"][0]["status"] = "approved"
    (out / vp.MAPPING_FILE).write_text(json.dumps(data), encoding="utf-8")
    snapshot = (out / vp.MAPPING_FILE).read_bytes()
    assert cli_main(["generate-viewpoints", "--out", str(out), "--replace-proposals"]) == 3
    assert "NOT overwritten" in capsys.readouterr().out
    assert (out / vp.MAPPING_FILE).read_bytes() == snapshot


# =========================================================================== API
@pytest.fixture(scope="module")
def provider():
    return MockConditionProvider("stable")


@pytest.fixture(scope="module")
def vclient(vsettings, mapping, provider):
    vp.ViewpointStore(vsettings.resolved_viewpoint_dir).write(mapping)
    with TestClient(create_app(vsettings, condition_provider=provider)) as c:
        yield c


def _login(c: TestClient, role: str) -> dict:
    name = f"sv08-{role}"
    users = c.app.state.auth.users
    if users.by_username(name) is None:
        users.create(name, TEST_PASSWORD, role, f"SV08 {role}")
    r = c.post("/api/auth/login", json={"username": name, "password": TEST_PASSWORD})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def admin(vclient):
    return _login(vclient, "admin")


@pytest.fixture(scope="module")
def viewer(vclient):
    return _login(vclient, "viewer")


@pytest.fixture(scope="module")
def reviewer(vclient):
    return _login(vclient, "reviewer")


def _decide(c, headers, **body):
    rev = c.get("/api/admin/viewpoints/review", headers=headers).json()["revision"]
    return c.post("/api/admin/viewpoints/decisions", headers=headers, json={"revision": rev, **body})


def test_viewpoint_endpoints_require_sign_in(vclient):
    assert vclient.get("/api/inspections/viewpoints", params={"location": "Main Deck"}).status_code == 401
    assert vclient.get("/api/admin/viewpoints/review").status_code == 401


def test_read_only_viewpoints_for_a_location(vclient, viewer):
    r = vclient.get("/api/inspections/viewpoints", params={"location": "cargo hold no. 1"}, headers=viewer)
    assert r.status_code == 200
    body = r.json()
    assert body["mapping"]["available"] is True and len(body["viewpoints"]) == 2
    assert [w["week"] for w in body["weeks"]] == [30, 31, 34, 38, 39]
    first = body["viewpoints"][0]["weeks"][0]["photos"][0]
    assert first["image_url"].startswith("/api/images/") and first["viewpoint_member"]["status"] == "proposed"
    text = json.dumps(body)
    assert "\\\\" not in text and ":/" not in text.replace("https:/", "")   # no file-system paths


def test_week_without_a_photograph_is_reported_unavailable(vclient, viewer):
    body = vclient.get("/api/inspections/viewpoints", params={"location": "Location 9"}, headers=viewer).json()
    weeks = {w["week"]: w["available"] for w in body["viewpoints"][0]["weeks"]}
    assert weeks == {30: True, 31: True, 34: True, 38: False, 39: False}


def test_repeated_file_is_marked_not_retaken(vclient, viewer):
    body = vclient.get("/api/inspections/viewpoints", params={"location": "Main Deck"}, headers=viewer).json()
    view = next(v for v in body["viewpoints"] if any(p["record_id"] == "W30-0002" for w in v["weeks"] for p in w["photos"]))
    w31 = next(w for w in view["weeks"] if w["week"] == 31)["photos"][0]
    assert w31["viewpoint_member"]["repeat_of_previous_week"] is True
    pair = vclient.get("/api/inspections/viewpoints/pair", params={"earlier": "W30-0002", "later": "W31-0002"}, headers=viewer).json()
    assert pair["status"] == "not_retaken"


def test_main_deck_areas_group_is_display_only(vclient, viewer):
    body = vclient.get("/api/inspections/viewpoints/groups", headers=viewer).json()
    g = body["groups"][0]
    assert g["display_name"] == "Main Deck Areas"
    # The fixture names ("Main Deck", "Main Deck Area") are not on the reviewed allowlist: a name containing
    # "deck" is no longer enough to be grouped.
    assert g["locations"] == [] and "Reviewed list" in g["basis"]
    assert vclient.get("/api/locations", headers=viewer).json()  # no fake "Deck" location exists
    assert all(l["location"].lower() != "deck" for l in vclient.get("/api/locations", headers=viewer).json())


# The 25 location entries of the real inventory (24 Official Locations + the Not Labelled placeholder).
REAL_LOCATIONS = [
    "BOAT DECK AREA", "BRIDGE DECK", *[f"CARGO HOLD NO. {n}" for n in range(1, 8)],
    "Cross deck Between # 1&2", "Cross deck Between # 2&3", "Cross deck Between # 3&4", "Cross deck Between # 4&5",
    "Cross deck between # 5&6", "Cross deck between # 6&7", "Cross deck Fwd of # 1", "FORECASTLE DECK AREA",
    "FUNNEL DECK AREA", "GANGWAY ENTRANCE", "MONKEY ISLAND", "POOP DECK AND MOORING WINCH",
    "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW", "RESCUE BOAT AND GARBAGE STORAGE DECK", "TOP VIEW MAIN DECK",
    "Not Labelled",
]
APPROVED_MAIN_DECK = {
    "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW", "TOP VIEW MAIN DECK", "Cross deck Fwd of # 1",
    "Cross deck Between # 1&2", "Cross deck Between # 2&3", "Cross deck Between # 3&4", "Cross deck Between # 4&5",
    "Cross deck between # 5&6", "Cross deck between # 6&7",
}


def test_main_deck_group_uses_only_the_reviewed_allowlist():
    from types import SimpleNamespace
    recs = [SimpleNamespace(official_location=name) for name in REAL_LOCATIONS]
    g = vp._deck_group(recs, "not labelled")
    assert set(g["locations"]) == APPROVED_MAIN_DECK and len(g["locations"]) == 9
    for excluded in ("BRIDGE DECK", "FUNNEL DECK AREA", "BOAT DECK AREA", "RESCUE BOAT AND GARBAGE STORAGE DECK",
                     "FORECASTLE DECK AREA", "POOP DECK AND MOORING WINCH", "GANGWAY ENTRANCE", "MONKEY ISLAND",
                     "CARGO HOLD NO. 7", "Not Labelled"):
        assert excluded not in g["locations"]
    # Case and spacing differences in the workbooks still match; nothing else does.
    assert vp._main_deck_allowed("top view  main deck") and vp._main_deck_allowed("CROSS DECK BETWEEN # 5&6")
    assert not vp._main_deck_allowed("Main Deck") and not vp._main_deck_allowed(None)


def test_groups_corrects_a_mapping_generated_with_the_old_deck_word_rule(mapping, vstore, tmp_path):
    legacy = json.loads(json.dumps(mapping))
    legacy["location_groups"] = [{"group_id": "main-deck-areas", "display_name": "Main Deck Areas",
                                  "locations": ["BRIDGE DECK", "TOP VIEW MAIN DECK", "FUNNEL DECK AREA", "Main Deck"],
                                  "basis": "Official Locations whose name contains the word 'deck'"}]
    store = vp.ViewpointStore(tmp_path)
    store.write(legacy)
    before = store.path.read_bytes()
    body = vp.ViewpointService(tmp_path, lambda: vstore).groups()
    assert [x["location"] for x in body["groups"][0]["locations"]] == ["TOP VIEW MAIN DECK"]
    assert "word 'deck'" not in body["groups"][0]["basis"]
    assert store.path.read_bytes() == before                                   # display-only: the file is not rewritten


def test_existing_inspection_routes_still_work(vclient, viewer):
    assert vclient.get("/api/inspections/W30-0001", headers=viewer).json()["record_id"] == "W30-0001"
    assert vclient.get("/api/inspections/search", params={"location": "Main Deck", "match": "exact"}, headers=viewer).status_code == 200


def test_admin_review_is_admin_only(vclient, viewer, reviewer):
    for h in (viewer, reviewer):
        assert vclient.get("/api/admin/viewpoints/review", headers=h).status_code == 403
        r = vclient.post("/api/admin/viewpoints/decisions", headers=h,
                         json={"action": "approve", "revision": 1, "viewpoint_id": "VP-X-01", "record_ids": []})
        assert r.status_code == 403


def test_ai_is_blocked_until_the_viewpoint_is_approved_then_allowed(vclient, admin, reviewer, provider):
    pair = {"earlier": "W30-0003", "later": "W31-0003"}
    v = vclient.get("/api/ai-analysis/validate", params={"earlier_record_id": pair["earlier"], "later_record_id": pair["later"]},
                    headers=reviewer).json()
    assert v["valid"] is False and v["errors"][0]["code"] == "viewpoint_not_approved"
    assert v["viewpoint"]["status"] == "requires_review"
    body = {"earlier_record_id": pair["earlier"], "later_record_id": pair["later"]}
    calls = len(provider.calls)
    r = vclient.post("/api/ai-analysis/compare", json=body, headers=reviewer)
    assert r.status_code == 409 and r.json()["code"] == "viewpoint_not_approved"
    assert len(provider.calls) == calls                                           # nothing reached the AI

    vid = _vp_of(vclient.app.state.viewpoints.mapping().data, "W30-0003")["viewpoint_id"]
    r = _decide(vclient, admin, action="approve", viewpoint_id=vid, record_ids=["W30-0003", "W31-0003"])
    assert r.status_code == 200, r.text
    assert vclient.get("/api/inspections/viewpoints/pair", params=pair, headers=reviewer).json()["status"] == "approved"
    r = vclient.post("/api/ai-analysis/compare", json=body, headers=reviewer)
    assert r.status_code == 200, r.text
    assert len(provider.calls) == calls + 1


def test_approved_unlabelled_pair_drops_only_the_ai_only_location_warning(vroot, mapping, tmp_path):
    # Own app and mapping copy: the shared module client is not touched.
    s = Settings(data_root=vroot, analysis_dir=tmp_path / "a", import_dir=tmp_path / "i", auth_dir=tmp_path / "auth",
                 viewpoint_dir=tmp_path / "loc", viewpoint_ai_gate=True)
    vp.ViewpointStore(s.resolved_viewpoint_dir).write(json.loads(json.dumps(mapping)))
    prov = MockConditionProvider("stable")
    with TestClient(create_app(s, condition_provider=prov)) as c:
        admin_h, reviewer_h = _login(c, "admin"), _login(c, "reviewer")
        pair = {"earlier_record_id": "W31-0001", "later_record_id": "W34-0001"}   # Not Labelled, AI hint "Main Deck"
        codes = lambda v, k: [i["code"] for i in v[k]]                            # noqa: E731

        before = c.get("/api/ai-analysis/validate", params=pair, headers=reviewer_h).json()
        assert before["location"]["match"] == "AI_ONLY"
        assert "ai_only_location" in codes(before, "warnings")                     # unapproved: warning kept ...
        assert "viewpoint_not_approved" in codes(before, "errors")                 # ... and still blocked

        vid = _vp_of(c.app.state.viewpoints.mapping().data, "W31-0001")["viewpoint_id"]
        assert _decide(c, admin_h, action="approve", viewpoint_id=vid, record_ids=["W31-0001", "W34-0001"]).status_code == 200
        after = c.get("/api/ai-analysis/validate", params=pair, headers=reviewer_h).json()
        assert after["viewpoint"]["status"] == "approved"
        assert "ai_only_location" not in codes(after, "warnings")
        assert "viewpoint_verified_location" in codes(after, "info")
        assert after["location"]["match"] == "AI_ONLY"                             # the location evidence itself is unchanged
        assert after["valid"] is True and after["requires_acknowledgement"] is (len(after["warnings"]) > 0)
        assert prov.calls == []                                                    # validation never calls the AI


def test_identical_and_cross_viewpoint_pairs_never_reach_the_ai(vclient, admin, reviewer, provider):
    data = vclient.app.state.viewpoints.mapping().data
    md = _vp_of(data, "W30-0002")["viewpoint_id"]
    assert _decide(vclient, admin, action="approve", viewpoint_id=md, record_ids=["W30-0002", "W31-0002"]).status_code == 200
    calls = len(provider.calls)
    r = vclient.post("/api/ai-analysis/compare", json={"earlier_record_id": "W30-0002", "later_record_id": "W31-0002"}, headers=reviewer)
    assert r.status_code == 400 and r.json()["code"] == "same_image"
    r = vclient.post("/api/ai-analysis/compare", json={"earlier_record_id": "W30-0003", "later_record_id": "W31-0004"}, headers=reviewer)
    assert r.status_code == 409 and r.json()["code"] == "viewpoint_not_approved"
    assert len(provider.calls) == calls


def test_decisions_are_audited_versioned_and_kept_in_history(vclient, admin):
    data = vclient.app.state.viewpoints.mapping().data
    vid = _vp_of(data, "W30-0005")["viewpoint_id"]
    review = vclient.get("/api/admin/viewpoints/review", params={"location": "Location 5"}, headers=admin).json()
    assert review["matches"] and all(m["viewpoint_id"] == vid for m in review["matches"])
    stale = review["revision"]
    # Bulk approval refuses while flagged (cross-layout) members are undecided; they are decided individually.
    r = _decide(vclient, admin, action="approve_all_proposed", viewpoint_id=vid)
    assert r.status_code == 409 and r.json()["code"] == "flagged_members"
    assert _decide(vclient, admin, action="approve", viewpoint_id=vid, record_ids=["W38-0005"]).status_code == 200
    assert _decide(vclient, admin, action="approve_all_proposed", viewpoint_id=vid).status_code == 200
    stale_after = vclient.get("/api/admin/viewpoints/review", headers=admin).json()["revision"]
    assert stale_after == stale + 2
    # A decision made on an old revision is refused (someone else changed the mapping in between).
    r = vclient.post("/api/admin/viewpoints/decisions", headers=admin,
                     json={"action": "rename", "revision": stale, "viewpoint_id": vid, "display_name": "x"})
    assert r.status_code == 409 and r.json()["code"] == "mapping_changed"
    # Changing an approved member needs a reason.
    r = _decide(vclient, admin, action="reject", viewpoint_id=vid, record_ids=["W31-0005"])
    assert r.status_code == 422 and r.json()["code"] == "reason_required"
    r = _decide(vclient, admin, action="reject", viewpoint_id=vid, record_ids=["W31-0005"], notes="wrong angle")
    assert r.status_code == 200
    hist = vclient.get("/api/admin/viewpoints/history", params={"viewpoint_id": vid}, headers=admin).json()["items"]
    assert [h["action"] for h in hist[:2]] == ["reject", "approve_all_proposed"]
    assert hist[0]["changes"][0]["previous"]["status"] == "approved" and hist[0]["by"]["username"] == "sv08-admin"
    entries = [json.loads(l) for l in (vclient.app.state.settings.resolved_auth_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    decided = [e for e in entries if e["event"] == "viewpoint_decided" and e["target"] == vid]
    assert [e["details"]["action"] for e in decided][-2:] == ["approve_all_proposed", "reject"]
    assert any(e["event"] == "viewpoint_decision_failed" for e in entries)
    assert vclient.app.state.auth.audit.verify().valid
    # The rejected photograph is no longer offered for comparison but stays in the file.
    assert "W31-0005" not in vclient.app.state.viewpoints.mapping().member_of
    assert any(m["record_id"] == "W31-0005" for m in _vp_of(vclient.app.state.viewpoints.mapping().data, "W30-0005")["members"])


def test_move_split_join_rename(vclient, admin):
    data = vclient.app.state.viewpoints.mapping().data
    a = _vp_of(data, "W30-0006")["viewpoint_id"]
    b = _vp_of(data, "W30-0007")["viewpoint_id"]
    assert _decide(vclient, admin, action="move", viewpoint_id=a, record_ids=["W39-0006"], target_viewpoint_id=b).status_code == 422
    r = _decide(vclient, admin, action="move", viewpoint_id=a, record_ids=["W39-0006"], target_viewpoint_id=b, notes="same view as 7")
    assert r.status_code == 200
    m = vclient.app.state.viewpoints.mapping()
    assert m.member_of["W39-0006"][0]["viewpoint_id"] == b and m.member_of["W39-0006"][1]["status"] == "approved"
    r = _decide(vclient, admin, action="split", viewpoint_id=a, at_week=38)
    assert r.status_code == 200
    new_id = r.json()["history_entry"]["viewpoint_ids"][1]
    assert vclient.app.state.viewpoints.mapping().member_of["W38-0006"][0]["viewpoint_id"] == new_id
    r = _decide(vclient, admin, action="join", viewpoint_id=a, target_viewpoint_id=new_id, notes="split was wrong")
    assert r.status_code == 200
    m = vclient.app.state.viewpoints.mapping()
    assert m.member_of["W38-0006"][0]["viewpoint_id"] == a and m.member_of["W38-0006"][1]["status"] == "needs_review"
    assert _decide(vclient, admin, action="rename", viewpoint_id=a, display_name="Location 6 - port side").status_code == 200
    assert vp.validate_mapping(m.data, vclient.app.state.store.by_id).ok


def test_unavailable_or_invalid_mapping_blocks_ai_and_is_reported(vsettings, provider, tmp_path):
    s = Settings(data_root=vsettings.data_root, analysis_dir=tmp_path / "a", import_dir=tmp_path / "i",
                 auth_dir=tmp_path / "auth", viewpoint_dir=tmp_path / "none", viewpoint_ai_gate=True)
    with TestClient(create_app(s, condition_provider=provider)) as c:
        h = _login(c, "reviewer")
        body = c.get("/api/inspections/viewpoints", params={"location": "Main Deck"}, headers=h).json()
        assert body["mapping"]["available"] is False and "generate-viewpoints" in body["mapping"]["problem"]
        assert body["viewpoints"] == []
        calls = len(provider.calls)
        r = c.post("/api/ai-analysis/compare", json={"earlier_record_id": "W30-0003", "later_record_id": "W31-0003"}, headers=h)
        assert r.status_code == 409 and r.json()["code"] == "viewpoint_not_approved"
        (tmp_path / "none").mkdir()
        (tmp_path / "none" / vp.MAPPING_FILE).write_text("{broken", encoding="utf-8")
        body = c.get("/api/inspections/viewpoints", params={"location": "Main Deck"}, headers=h).json()
        assert body["mapping"]["available"] is False and "invalid" in body["mapping"]["problem"]
        assert len(provider.calls) == calls


def test_production_gate_cannot_be_switched_off_by_environment(monkeypatch):
    assert config.VIEWPOINT_AI_GATE_PRODUCTION is True
    monkeypatch.setattr(config, "DEFAULT_VIEWPOINT_AI_GATE", config.VIEWPOINT_AI_GATE_PRODUCTION)
    for name in ("SYNERGY_VIEWPOINT_AI_GATE", "SYNERGY_VIEWPOINT_GATE", "SYNERGY_DISABLE_VIEWPOINT_GATE"):
        monkeypatch.setenv(name, "0")
    s = config.get_settings()
    assert s.viewpoint_ai_gate is None and s.resolved_viewpoint_ai_gate is True


def test_backup_includes_the_viewpoint_mapping(vsettings, vclient, tmp_path):
    s = Settings(data_root=vsettings.data_root, analysis_dir=vsettings.analysis_dir, import_dir=vsettings.import_dir,
                 auth_dir=vsettings.auth_dir, viewpoint_dir=vsettings.viewpoint_dir, backup_dir=tmp_path / "bk")
    made = bk.create_backup(s)
    m = bk.read_manifest(bk._backup_path(s, made["backup_id"]))
    assert "app/locations/viewpoints.json" in {f["path"] for f in m["files"]}
    assert m["restore_targets"]["app/locations"] == "SYNERGY_VIEWPOINT_DIR"
    dest = tmp_path / "restored"
    bk.restore_backup(s, made["backup_id"], dest)
    assert (dest / "app" / "locations" / "viewpoints.json").read_bytes() == (vsettings.viewpoint_dir / "viewpoints.json").read_bytes()
    assert "SYNERGY_VIEWPOINT_DIR" in (dest / "RESTORE_README.txt").read_text(encoding="utf-8")
    with pytest.raises(bk.BackupError):
        bk.restore_backup(s, made["backup_id"], vsettings.viewpoint_dir / "x")
