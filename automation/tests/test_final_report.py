"""--generate-excel-report / --generate-json-report (src/final_report.py + run_pipeline). Offline,
synthetic week folders under tmp_path only."""

import hashlib
import io
import json
import sys

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from PIL import Image as PILImage

import run_pipeline
from src import final_report

HEADERS = ["Serial Number", "Image Filename", "Report Date", "Original Excel Filename",
           "Worksheet Name", "Official Location", "AI Suggested Location", "Extracted Photograph",
           "Activity Identified", "Location Confidence"]
ROWS = [
    [1, "2026-08-02_001.jpg", "8/2/2026", "Part 1 (1).xlsx", "AUG 02 2026 ", "Not Labelled",
     "Main Deck", None, "No activity observed", "Medium"],
    [2, "2026-08-02_002.jpg", "8/2/2026", "Part 1 (1).xlsx", "AUG 02 2026 ", "CARGO HOLD NO. 1",
     None, None, "Crew \"coiling\" line — Ünïcödé & <tags>\nnew line", None],
    [3, "2026-08-02_003.jpg", "8/2/2026", "Part 1 (2).xlsx", "AUG 02 2026 ",
     "Cross deck Fwd of # 1", "", None, "No activity observed", None],
]


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (40, 30), color).save(buf, "JPEG")
    return buf.getvalue()


PHOTOS = [_jpeg((200, 0, 0)), _jpeg((0, 200, 0)), _jpeg((0, 0, 200))]


def _make_report(path, rows=ROWS, photos=PHOTOS, headers=HEADERS):
    wb = Workbook()
    ws = wb.active
    ws.title = "Image Register"
    ws.append(headers)
    for i, row in enumerate(rows):
        ws.append(row)
        if i < len(photos) and photos[i] is not None:
            ws.add_image(XLImage(io.BytesIO(photos[i])), f"H{i + 2}")
    wb.save(path)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    for name in ("Week 30", "week 31"):
        (root / name).mkdir(parents=True)
        _make_report(root / name / final_report.EXCEL_NAME)
    (root / "week 31" / "Nord Kudu Part 1 (1).xlsx").write_bytes(b"source workbook - untouched")
    return root


def _run(monkeypatch, tmp_path, project, *argv):
    settings = {"paths": {"project_root": str(project), "automation_root": str(tmp_path / "auto")}}
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(run_pipeline.config, "load_validated_settings", lambda: settings)
    def _forbidden(*a, **k):
        raise AssertionError("report commands must not build a client or load .env")
    monkeypatch.setattr(run_pipeline.config, "load_provider_environment", _forbidden)
    monkeypatch.setattr(run_pipeline.vision_client, "build_vision_client", _forbidden)
    monkeypatch.setattr(run_pipeline.logging_utils, "configure_logging", _forbidden)
    with pytest.raises(SystemExit) as exc:
        run_pipeline.main()
    return exc.value.code


def _snapshot(root):
    return {p.relative_to(root): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


# 1/4. Excel command -----------------------------------------------------------------------

def test_excel_command_verifies_and_keeps_existing_reports(monkeypatch, tmp_path, project, capsys):
    before = _snapshot(project)
    assert _run(monkeypatch, tmp_path, project, "--generate-excel-report") == 0
    assert _snapshot(project) == before          # nothing written, nothing modified
    out = capsys.readouterr().out
    assert out.count("VERIFIED") == 2 and "2 week(s), 6 record(s)" in out


def test_excel_overwrite_backs_up_and_rebuilds_with_identical_data(monkeypatch, tmp_path, project):
    path = project / "Week 30" / final_report.EXCEL_NAME
    original = path.read_bytes()
    source = final_report.load_report("Week 30", path)
    assert _run(monkeypatch, tmp_path, project, "--generate-excel-report", "--overwrite") == 0
    backups = list((tmp_path / "auto" / "backups").glob("final_report_excel_*/Week 30/*.xlsx"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    rebuilt = final_report.load_report("Week 30", path)
    assert rebuilt.headers == HEADERS and rebuilt.rows == source.rows
    assert [r[0] for r in rebuilt.rows] == [1, 2, 3]
    assert [p.data for p in rebuilt.photos] == PHOTOS
    assert not list(project.rglob("*.json"))


# 2/5. JSON command ------------------------------------------------------------------------

def test_json_command_writes_valid_json_in_each_week_folder(monkeypatch, tmp_path, project):
    excel_before = {k: v for k, v in _snapshot(project).items() if k.suffix == ".xlsx"}
    assert _run(monkeypatch, tmp_path, project, "--generate-json-report") == 0
    for week in ("Week 30", "week 31"):
        doc = json.loads((project / week / final_report.JSON_NAME).read_text(encoding="utf-8"))
        assert doc["week"] == week and doc["record_count"] == 3
    assert sorted(p.parent.name for p in project.rglob("*.json")) == ["Week 30", "week 31"]
    assert {k: v for k, v in _snapshot(project).items() if k.suffix == ".xlsx"} == excel_before


def test_json_types_nulls_and_special_characters(project):
    data = final_report.load_report("Week 30", project / "Week 30" / final_report.EXCEL_NAME)
    doc = json.loads(final_report.serialize_json(final_report.build_json_document(data)))
    r1, r2, r3 = doc["records"]
    assert [c["key"] for c in doc["columns"]][:2] == ["serial_number", "image_filename"]
    assert r1["serial_number"] == 1 and isinstance(r1["serial_number"], int)
    assert r1["worksheet_name"] == "AUG 02 2026 "                 # text kept verbatim
    assert r2["ai_suggested_location"] is None and r2["location_confidence"] is None
    assert r3["ai_suggested_location"] is None                     # "" -> null, consistently
    assert r2["activity_identified"] == ROWS[1][8]                  # quotes, unicode, <>, newline
    assert r1["extracted_photograph"]["sha256"] == hashlib.sha256(PHOTOS[0]).hexdigest()
    assert r1["extracted_photograph"]["format"] == "jpeg"


def test_json_validation_rejects_bad_documents():
    doc = {"columns": [{"key": "serial_number"}], "record_count": 2,
           "records": [{"serial_number": 1}]}
    with pytest.raises(ValueError):
        final_report.validate_json_document(doc)
    doc["record_count"] = 1
    doc["records"] = [{"serial_number": float("nan")}]
    with pytest.raises(ValueError):
        final_report.serialize_json(doc)


# 3. independence --------------------------------------------------------------------------

def test_commands_are_mutually_exclusive_and_reject_ai_options(monkeypatch, tmp_path, project):
    for argv in (["--generate-excel-report", "--generate-json-report"],
                 ["--generate-json-report", "--dry-run"],
                 ["--generate-excel-report", "--from-registers"],
                 ["--overwrite"]):
        assert _run(monkeypatch, tmp_path, project, *argv) == 2
    assert not list(project.rglob("*.json"))


def test_excel_command_does_not_touch_existing_json(monkeypatch, tmp_path, project):
    _run(monkeypatch, tmp_path, project, "--generate-json-report")
    json_before = {k: v for k, v in _snapshot(project).items() if k.suffix == ".json"}
    _run(monkeypatch, tmp_path, project, "--generate-excel-report", "--overwrite")
    assert {k: v for k, v in _snapshot(project).items() if k.suffix == ".json"} == json_before


# 6. consistency ---------------------------------------------------------------------------

def test_json_matches_excel_record_for_record(monkeypatch, tmp_path, project):
    _run(monkeypatch, tmp_path, project, "--generate-json-report")
    for week in ("Week 30", "week 31"):
        wb = load_workbook(project / week / final_report.EXCEL_NAME)
        ws = wb.active
        excel_rows = [list(r) for r in ws.iter_rows(min_row=2, values_only=True)]
        doc = json.loads((project / week / final_report.JSON_NAME).read_text(encoding="utf-8"))
        assert doc["record_count"] == len(excel_rows) == len(ws._images)
        for xl, rec in zip(excel_rows, doc["records"]):
            for header, value in zip(HEADERS, xl):
                key = final_report.json_keys([header])[0]
                if header == "Extracted Photograph":
                    continue
                assert rec[key] == (None if value in (None, "") else value)
        assert [r["serial_number"] for r in doc["records"]] == [r[0] for r in excel_rows]


# 8. missing folders / source --------------------------------------------------------------

def test_missing_source_report_is_reported_and_other_weeks_still_run(monkeypatch, tmp_path, project, capsys):
    (project / "week 31" / final_report.EXCEL_NAME).unlink()
    assert _run(monkeypatch, tmp_path, project, "--generate-json-report") == 1
    out = capsys.readouterr().out
    assert "week 31: MISSING" in out
    assert (project / "Week 30" / final_report.JSON_NAME).is_file()
    assert not (project / "week 31" / final_report.JSON_NAME).exists()


def test_corrupt_or_invalid_source_is_invalid(monkeypatch, tmp_path, project, capsys):
    (project / "Week 30" / final_report.EXCEL_NAME).write_bytes(b"not a workbook")
    _make_report(project / "week 31" / final_report.EXCEL_NAME, headers=HEADERS[1:])
    assert _run(monkeypatch, tmp_path, project, "--generate-excel-report") == 1
    out = capsys.readouterr().out
    assert out.count("INVALID") == 2 and "Serial Number" in out


def test_no_week_folders_and_unknown_week(monkeypatch, tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(monkeypatch, tmp_path, empty, "--generate-json-report") == 1
    assert _run(monkeypatch, tmp_path, empty, "--generate-json-report", "--weeks", "Week 99") == 2


def test_weeks_filter_limits_output(monkeypatch, tmp_path, project):
    assert _run(monkeypatch, tmp_path, project, "--generate-json-report", "--weeks", "WEEK 31") == 0
    assert [p.parent.name for p in project.rglob("*.json")] == ["week 31"]


# 9. existing output handling --------------------------------------------------------------

def test_existing_json_unchanged_conflict_and_overwrite(monkeypatch, tmp_path, project, capsys):
    path = project / "Week 30" / final_report.JSON_NAME
    _run(monkeypatch, tmp_path, project, "--generate-json-report")
    first = path.read_bytes()
    capsys.readouterr()
    assert _run(monkeypatch, tmp_path, project, "--generate-json-report") == 0
    assert "UNCHANGED" in capsys.readouterr().out and path.read_bytes() == first

    path.write_text('{"edited": true}', encoding="utf-8")
    assert _run(monkeypatch, tmp_path, project, "--generate-json-report") == 1
    assert "Week 30: CONFLICT" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == '{"edited": true}'  # kept

    assert _run(monkeypatch, tmp_path, project, "--generate-json-report", "--overwrite") == 0
    assert path.read_bytes() == first
    backups = list((tmp_path / "auto" / "backups").glob("final_report_json_*/Week 30/*.json"))
    assert [b.read_text(encoding="utf-8") for b in backups] == ['{"edited": true}']


def test_row_without_photo_is_null_and_extra_photo_is_rejected(tmp_path):
    path = tmp_path / final_report.EXCEL_NAME
    _make_report(path, photos=[PHOTOS[0], None, PHOTOS[2]])
    data = final_report.load_report("W", path)
    doc = final_report.build_json_document(data)
    assert doc["records"][1]["extracted_photograph"] is None
    wb = load_workbook(path)
    wb.active.add_image(XLImage(io.BytesIO(PHOTOS[1])), "H2")
    wb.save(path)
    with pytest.raises(final_report.ReportSourceError, match="2 photos"):
        final_report.load_report("W", path)
