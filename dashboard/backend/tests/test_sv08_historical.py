"""SV-08 historical regression: the viewpoint generator on the REAL inspection history (read-only).

Skipped where the historical data is not present (for example in CI). Nothing is written: the store is built in
memory and the generator returns a dict. The asserted cases come from the SV-08 investigation.
"""

from __future__ import annotations

import pytest

from app import config
from app.config import Settings
from app.services import viewpoints as vp
from app.services.inspection_store import InspectionStore

ROOT = config.DEFAULT_DATA_ROOT
pytestmark = pytest.mark.skipif(not (ROOT / "Week 42").is_dir() or not (ROOT / "Week 30").is_dir(),
                                reason="real historical data not present")


@pytest.fixture(scope="module")
def real():
    s = Settings(data_root=ROOT, ai_provider="disabled")
    store = InspectionStore.build(s)
    return store, vp.generate(store.records, s.unlabelled_placeholder, created_at="2026-11-24T00:00:00+00:00")


def _vp(data, week, serial, source="json_report"):
    for v in data["viewpoints"]:
        for m in v["members"]:
            if m["week"] == week and m["serial"] == serial and m["source"] == source:
                return v
    raise AssertionError(f"W{week} #{serial} not found")


def _serial_in(v, week):
    return sorted(m["serial"] for m in v["members"] if m["week"] == week)


def test_inventory_is_unchanged(real):
    store, data = real
    assert len(store.records) == 1078
    assert len({r.extracted_photograph.sha256 for r in store.records if r.extracted_photograph}) == 609
    assert data["generated"]["inventory"]["weeks"] == [30, 31, 34, 35, 36, 37, 38, 39, 40, 41, 42]   # no 32/33


def test_two_layout_eras_and_full_coverage(real):
    store, data = real
    assert [e["weeks"] for e in data["generated"]["eras"]] == [[30, 31, 34, 35, 36, 37], [38, 39, 40, 41, 42]]
    linked = [m["record_id"] for v in data["viewpoints"] for m in v["members"]]
    assert sorted(linked) == sorted(r.record_id for r in store.records)
    assert len(data["viewpoints"]) == 99
    assert vp.validate_mapping(data, store.by_id).ok
    assert not vp.has_decisions(data)


def test_not_labelled_crosswalk_week_37_to_38(real):
    _, data = real
    for w37, w38 in ((1, 17), (2, 18), (3, 19), (4, 20), (81, 1), (82, 2)):
        v = _vp(data, 37, w37)
        assert v["official_location"] == "Not Labelled"
        assert _serial_in(v, 38) == [w38], (w37, _serial_in(v, 38))
        m38 = next(m for m in v["members"] if m["week"] == 38)
        assert m38["status"] == "needs_review" and m38["method"] == "proposed_cross_layout"


def test_every_week_37_to_38_link_needs_review(real):
    _, data = real
    for v in data["viewpoints"]:
        for m in v["members"]:
            if m["week"] == 38 and any(x["week"] == 37 for x in v["members"]):
                assert m["status"] == "needs_review" and "cross_layout" in m["flags"]


def test_cargo_hold_7_and_gangway_changes_are_flagged(real):
    _, data = real
    types = {(f["type"], f["message"].split("'")[1] if "'" in f["message"] else "") for f in data["findings"]}
    assert ("location_count_changed", "cargo hold no. 7") in types
    assert ("location_count_changed", "gangway entrance") in types
    assert any(f["type"] == "label_inconsistency" and "#37" in f["message"] for f in data["findings"])
    for serial in range(37, 43):
        m = next(m for m in _vp(data, 37, serial)["members"] if m["week"] == 37)
        assert m["status"] == "needs_review"
    # Week 36 #37 (labelled Gangway Entrance) is not silently merged into Cargo Hold No. 7.
    assert _vp(data, 36, 37)["official_location"].lower() == "gangway entrance"
    assert _vp(data, 36, 37) is not _vp(data, 37, 37)


def test_register_rows_with_reused_files_are_flagged_not_merged(real):
    _, data = real
    reg = [v for v in data["viewpoints"] if any(m["source"] == "image_register" for m in v["members"])]
    assert reg
    for v in reg:
        assert all(m["official_location"] for m in v["members"])
    flagged = [m for v in reg for m in v["members"] if "identical_file_different_label" in m["flags"]]
    assert flagged and all(m["status"] == "needs_review" for m in flagged)


def test_repeated_photographs_are_identical_file_members(real):
    _, data = real
    repeats = sum(1 for v in data["viewpoints"] for m in v["members"] if m["method"] == "identical_file")
    assert repeats == 455   # identical-file transitions between consecutive weeks found by the investigation
