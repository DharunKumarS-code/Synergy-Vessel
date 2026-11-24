import hashlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl
import pytest
from openpyxl.utils.units import EMU_to_pixels
from PIL import Image as PILImage

from src import report_builder

HEADERS = [
    "Serial Number", "Image Filename", "Report Date", "Original Excel Filename",
    "Worksheet Name", "Official Location", "AI Suggested Location",
    "Extracted Photograph", "Activity Identified", "Location Confidence",
]


def _tiny_jpeg_bytes(color) -> bytes:
    im = PILImage.new("RGB", (40, 30), color)
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


def test_report_has_expected_columns_and_preserves_image_bytes(tmp_path):
    img_bytes = _tiny_jpeg_bytes((10, 20, 30))
    rows = [
        report_builder.ReportRow(
            serial_number=1, image_filename="2026-01-01_001.jpg", report_date="2026-01-01",
            original_excel_filename="source.xlsx", worksheet_name="JAN 01 2026",
            official_location="Not Labelled", ai_suggested_location="Main Deck",
            image_bytes=img_bytes, activity_identified="No activity observed",
            location_confidence="Medium",
        ),
    ]
    out_path = tmp_path / "Final Official vs AI Location Report.xlsx"
    report_builder.build_report("Week TEST", rows, out_path)

    wb = openpyxl.load_workbook(out_path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    assert [ws.cell(1, c).value for c in range(1, 11)] == HEADERS
    assert ws.cell(2, 6).value == "Not Labelled"
    assert ws.cell(2, 7).value == "Main Deck"
    assert ws.cell(2, 8).value is None  # image floats above an otherwise empty cell

    assert len(ws._images) == 1
    anchor = ws._images[0].anchor
    assert anchor._from.col == 7  # column H, 0-indexed
    assert anchor._from.row == 1  # row 2, 0-indexed

    embedded_bytes = ws._images[0]._data()
    assert hashlib.md5(embedded_bytes).hexdigest() == hashlib.md5(img_bytes).hexdigest()


def test_image_aspect_ratio_preserved_when_scaled_down(tmp_path):
    img_bytes = _tiny_jpeg_bytes((5, 5, 5))  # 40x30, aspect ratio 4:3
    rows = [
        report_builder.ReportRow(
            serial_number=1, image_filename="x.jpg", report_date="2026-01-01",
            original_excel_filename="s.xlsx", worksheet_name="W",
            official_location="Main Deck", ai_suggested_location=None,
            image_bytes=img_bytes, activity_identified="No activity observed",
            location_confidence=None,
        ),
    ]
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", rows, out_path, image_max_width_px=20, image_max_height_px=20)

    wb = openpyxl.load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    anchor = ws._images[0].anchor
    # The anchor's ext (EMU) is what controls the rendered size in Excel -- Image.width/height
    # read back from a reloaded file reflect the embedded picture's native pixel size instead,
    # not the display size, so they are not a reliable thing to assert on post-reload.
    rendered_w = EMU_to_pixels(anchor.ext.cx)
    rendered_h = EMU_to_pixels(anchor.ext.cy)
    # scaled to fit 20x20 preserving 40:30 (4:3) aspect ratio -> 20x15
    assert rendered_w == 20
    assert rendered_h == 15


# ---------------------------------------------------------------------------
# Phase S: malformed-image handling and atomic report saves.
# ---------------------------------------------------------------------------


def _row(filename: str, image_bytes: bytes, serial: int = 1,
         classification_status: str = "CLASSIFIED") -> report_builder.ReportRow:
    return report_builder.ReportRow(
        serial_number=serial, image_filename=filename, report_date="2026-01-01",
        original_excel_filename="s.xlsx", worksheet_name="W",
        official_location="Main Deck", ai_suggested_location=None,
        image_bytes=image_bytes, activity_identified="No activity observed",
        location_confidence=None, classification_status=classification_status,
    )


def test_malformed_image_raises_clear_report_generation_error(tmp_path):
    rows = [_row("bad.jpg", b"this is not a real image at all")]
    out_path = tmp_path / "report.xlsx"

    with pytest.raises(report_builder.ReportGenerationError) as exc_info:
        report_builder.build_report("Week TEST", rows, out_path)

    err = exc_info.value
    assert err.image_filename == "bad.jpg"
    assert "bad.jpg" in str(err)
    assert type(err.original_exception).__name__ in str(err)


def test_report_generation_failure_does_not_replace_an_existing_valid_report(tmp_path):
    out_path = tmp_path / "report.xlsx"
    good_bytes = _tiny_jpeg_bytes((1, 2, 3))
    report_builder.build_report("Week TEST", [_row("good.jpg", good_bytes)], out_path)
    original_content = out_path.read_bytes()

    with pytest.raises(report_builder.ReportGenerationError):
        report_builder.build_report("Week TEST", [_row("bad.jpg", b"not an image")], out_path)

    # the existing valid report must be byte-for-byte untouched by the failed attempt.
    assert out_path.read_bytes() == original_content


def test_no_leftover_temp_files_after_a_failed_report_build(tmp_path):
    out_path = tmp_path / "report.xlsx"
    with pytest.raises(report_builder.ReportGenerationError):
        report_builder.build_report("Week TEST", [_row("bad.jpg", b"not an image")], out_path)

    leftover = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftover == []
    assert not out_path.exists()  # never created in the first place


def test_no_leftover_temp_files_after_a_successful_report_build(tmp_path):
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("good.jpg", _tiny_jpeg_bytes((9, 9, 9)))], out_path)

    names = [p.name for p in tmp_path.iterdir()]
    assert names == ["report.xlsx"]  # no stray ".tmp" file left behind


def test_build_report_uses_atomic_replace_not_an_in_place_save(tmp_path, monkeypatch):
    calls = []
    real_replace = report_builder.os.replace

    def _tracking_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(report_builder.os, "replace", _tracking_replace)
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("good.jpg", _tiny_jpeg_bytes((9, 9, 9)))], out_path)

    assert len(calls) == 1
    src, dst = calls[0]
    assert src != dst
    assert dst == str(out_path)
    assert ".tmp" in src


# ---------------------------------------------------------------------------
# Phase AC: "Classification Status" trailing column (API_FAILURE / VISUAL_REVIEW / CLASSIFIED).
# ---------------------------------------------------------------------------


def test_api_failure_status_appears_in_the_trailing_column(tmp_path):
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("x.jpg", _tiny_jpeg_bytes((1, 1, 1)),
                                                     classification_status="API_FAILURE")], out_path)
    wb = openpyxl.load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    assert ws.cell(1, 11).value == "Classification Status"
    assert ws.cell(2, 11).value == "API_FAILURE"


def test_visual_review_status_appears_in_the_trailing_column(tmp_path):
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("x.jpg", _tiny_jpeg_bytes((2, 2, 2)),
                                                     classification_status="VISUAL_REVIEW")], out_path)
    wb = openpyxl.load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    assert ws.cell(2, 11).value == "VISUAL_REVIEW"


def test_classified_status_appears_in_the_trailing_column(tmp_path):
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("x.jpg", _tiny_jpeg_bytes((3, 3, 3)))], out_path)
    wb = openpyxl.load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    assert ws.cell(2, 11).value == "CLASSIFIED"


def test_existing_ten_columns_retain_their_exact_order(tmp_path):
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("x.jpg", _tiny_jpeg_bytes((4, 4, 4)),
                                                     classification_status="API_FAILURE")], out_path)
    wb = openpyxl.load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    assert [ws.cell(1, c).value for c in range(1, 11)] == HEADERS  # unchanged, original 10
    assert ws.cell(1, 11).value == "Classification Status"  # the new column is strictly trailing


def test_embedded_image_still_intact_alongside_new_status_column(tmp_path):
    img_bytes = _tiny_jpeg_bytes((5, 5, 5))
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("x.jpg", img_bytes, classification_status="API_FAILURE")], out_path)
    wb = openpyxl.load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    assert len(ws._images) == 1
    assert hashlib.md5(ws._images[0]._data()).hexdigest() == hashlib.md5(img_bytes).hexdigest()
    assert ws.cell(2, 8).value is None  # image cell (H) still stays empty -- photo floats above it


# --- L3: fsync before the swap; temp file removed even on Ctrl+C ------------------------------


def test_l3_report_temp_file_is_fsynced_before_it_replaces_the_report(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = report_builder.os.fsync, report_builder.os.replace
    monkeypatch.setattr(report_builder.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(report_builder.os, "replace",
                        lambda src, dst: (events.append("replace"), real_replace(src, dst))[1])
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("a.jpg", _tiny_jpeg_bytes((4, 5, 6)))], out_path)
    assert events == ["fsync", "replace"]
    assert openpyxl.load_workbook(out_path).active.max_row == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_l3_ctrl_c_during_save_removes_temp_file_and_keeps_old_report(tmp_path, monkeypatch):
    out_path = tmp_path / "report.xlsx"
    report_builder.build_report("Week TEST", [_row("a.jpg", _tiny_jpeg_bytes((7, 8, 9)))], out_path)
    old_bytes = out_path.read_bytes()

    def interrupted_save(self, filename):
        Path(filename).write_bytes(b"half-written")
        raise KeyboardInterrupt()

    monkeypatch.setattr(openpyxl.Workbook, "save", interrupted_save)
    with pytest.raises(KeyboardInterrupt):  # propagates unchanged, never wrapped
        report_builder.build_report("Week TEST", [_row("b.jpg", _tiny_jpeg_bytes((1, 1, 1)))], out_path)
    assert out_path.read_bytes() == old_bytes
    assert not list(tmp_path.glob("*.tmp"))


def test_l3_ordinary_save_failure_is_still_a_report_generation_error(tmp_path, monkeypatch):
    def failing_save(self, filename):
        raise PermissionError("locked (simulated)")

    monkeypatch.setattr(openpyxl.Workbook, "save", failing_save)
    with pytest.raises(report_builder.ReportGenerationError):
        report_builder.build_report("Week TEST", [_row("c.jpg", _tiny_jpeg_bytes((2, 2, 2)))],
                                    tmp_path / "report.xlsx")
    assert not list(tmp_path.glob("*.tmp"))


def test_review_columns_are_optional_and_follow_result_source(tmp_path):
    import io as _io
    from PIL import Image as _PIL
    buf = _io.BytesIO(); _PIL.new("RGB", (20, 10), (1, 2, 3)).save(buf, "JPEG")
    row = report_builder.ReportRow(1, "W30-0001.jpg", "2026-08-02", "src.xlsx", "AUG", "", "Main Deck",
                                   buf.getvalue(), "No activity observed", "Low", "VISUAL_REVIEW",
                                   result_source="AI request (this run)", activity_confidence="High",
                                   remarks="Low/Needs-Review confidence on activity or location")
    out = tmp_path / "r.xlsx"
    report_builder.build_report("Week 30", [row], out, result_source_column=True, review_columns=True)
    ws = openpyxl.load_workbook(out)["Image Register"]
    assert [c.value for c in ws[1]] == report_builder.HEADERS + ["Result Source"] + report_builder.REVIEW_HEADERS
    assert [c.value for c in ws[2]][11:] == ["AI request (this run)", "High",
                                             "Low/Needs-Review confidence on activity or location"]
    assert len(ws._images) == 1
    plain = tmp_path / "p.xlsx"
    report_builder.build_report("Week 30", [row], plain)
    assert [c.value for c in openpyxl.load_workbook(plain)["Image Register"][1]] == report_builder.HEADERS
