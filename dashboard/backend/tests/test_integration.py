"""JSON loading, multi-week integration, image linking and validation reporting."""

import datetime as dt

import pytest

from app.services.image_service import ImageIndex, ImageNotFound, InvalidImageId
from app.services.inspection_store import parse_worksheet_date, vessel_from


def test_discovers_only_folders_with_reports(store):
    folders = [w["folder"] for w in store.report.weeks]
    assert folders == ["Week 30", "week 34", "week 35"]


def test_unreadable_report_is_reported_not_raised(store):
    w35 = next(w for w in store.report.weeks if w["folder"] == "week 35")
    assert w35["readable"] is False
    assert any("report unreadable" in i for i in w35["issues"])
    assert store.report.totals["weeks_readable"] == 2


def test_record_counts_preserved_and_invalid_records_kept(store):
    t = store.report.totals
    assert t["json_records"] == 5  # 3 + 2 valid
    assert t["invalid_records"] == 1
    assert store.report.invalid_records[0]["week"] == "week 34"
    w34 = next(w for w in store.report.weeks if w["folder"] == "week 34")
    assert any("record_count 5 != 3" in i for i in w34["issues"])


def test_original_fields_preserved(store):
    r = store.by_id["W30-0002"]
    assert r.original_image_filename == "2026-08-02_002.jpg"
    assert r.serial_number == 2 and r.report_date == "8/2/2026"
    assert r.official_location == "Main Deck"
    assert r.extracted_photograph.width_px == 850


def test_official_location_never_overwritten_by_ai(store):
    r = store.by_id["W30-0001"]
    assert r.official_location == "Not Labelled"
    assert r.ai_suggested_location == "Main Deck"
    assert r.official_location_labelled is False


def test_duplicate_in_week_disambiguated_by_register_and_extra_retained(store):
    json_rec = store.by_id["W30-0001"]
    assert json_rec.image_match_method == "sha256+register_location"
    extra = store.by_id["W30-0004"]
    assert extra.source == "image_register"
    assert extra.official_location == "PORT AND STARBOARD VIEW"
    assert extra.extracted_photograph.sha256 == json_rec.extracted_photograph.sha256
    assert json_rec.same_photo_appearances == 2


def test_cross_week_duplicate_counted(store):
    assert store.by_id["W30-0002"].same_photo_appearances == 2
    assert store.by_id["W34-0001"].same_photo_appearances == 2


def test_missing_image_reported_safely(store):
    missing = store.report.missing_images
    assert len(missing) == 1 and missing[0]["week"] == "week 34"
    rec = store.by_id[missing[0]["record_id"]]
    assert rec.image_id is None and rec.image_url is None and rec.image_status == "missing"


def test_dates_interpreted(store):
    assert store.by_id["W30-0002"].inspection_date == dt.date(2026, 8, 2)
    pending = store.by_id["W34-0001"]
    assert pending.report_date == "AMBIGUOUS/PENDING"
    assert pending.inspection_date == dt.date(2026, 9, 6)
    assert pending.date_source == "worksheet_name"


def test_totals(store):
    t = store.report.totals
    assert t["total_appearances"] == 6
    assert t["resolved_images"] == 5
    assert t["image_files_on_disk"] == 5
    assert store.report.unlinked_image_files == []


@pytest.mark.parametrize("text,expected", [
    ("AUG 30 2026", dt.date(2026, 8, 30)), ("SEPT 06 2026", dt.date(2026, 9, 6)),
    ("AUG 02 2026 ", dt.date(2026, 8, 2)), ("FOO 1 2026", None), ("AUG 32 2026", None), (None, None),
])
def test_parse_worksheet_date(text, expected):
    assert parse_worksheet_date(text) == expected


def test_vessel_from_filename():
    assert vessel_from("Nord Kudu  Deck Photos - OCT 25 2026 Part 1 (2).xlsx") == ("nord-kudu", "Nord Kudu")
    assert vessel_from(None) == ("unknown-vessel", "Unknown vessel")


def test_image_index_rejects_bad_ids_and_outside_paths(tmp_path):
    approved = tmp_path / "ok"
    approved.mkdir()
    idx = ImageIndex([approved])
    for bad in ["..", "../secret", "a/b", "a\\b", "", "W30-0001.jpg", "x" * 65, "%2e%2e"]:
        with pytest.raises(InvalidImageId):
            idx.resolve(bad)
    from app.services.image_service import ImageFile
    outside = tmp_path / "evil.jpg"
    outside.write_bytes(b"x")
    idx.add(ImageFile("evil", outside, "x", "0", 1))
    with pytest.raises(ImageNotFound, match="outside"):
        idx.resolve("evil")
