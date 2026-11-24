"""Extraction-only workflow (src/image_extraction.py, extract_images.py). All workbooks are
temporary and built here; no real project data, no .env, no network, no AI."""

import hashlib
import io
import sys
from datetime import date
from pathlib import Path

import openpyxl
import pytest
from openpyxl.drawing.image import Image as XLImage
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract_images  # noqa: E402
from src import image_extraction as ie  # noqa: E402


def png_bytes(color, size=(8, 6)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


RED, GREEN, BLUE, GREY = (png_bytes(c) for c in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (90, 90, 90)])


def add_sheet(wb, name, banner, images, headings=()):
    """images: [(cell, bytes)]; headings: [(row, text)] merged A..L (width 12 >= 10)."""
    ws = wb.create_sheet(name)
    ws.cell(1, 1, banner)
    ws.merge_cells("A1:L1")
    for row, text in headings:
        ws.cell(row, 1, text)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=12)
    for cell, data in images:
        img = XLImage(io.BytesIO(data))
        img.anchor = cell
        ws.add_image(img)
    return ws


def make_workbook(path: Path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for s in sheets:
        add_sheet(wb, *s)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def banner(d):  # d like "02 AUG 2026"
    return f" DECK PHOTOS\nM.V. NORD KUDU\nDate: {d}"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    # Week 30: two workbooks; part (1) has 3 images in one sheet (one same-week duplicate)
    make_workbook(root / "Week 30" / "Deck Photos -02 AUG 2026 Part 1 (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"),
         [("B12", GREEN), ("B5", RED), ("H12", BLUE)], [(10, "MAIN DECK")]),
    ])
    make_workbook(root / "Week 30" / "Deck Photos -02 AUG 2026 Part 1 (2).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"), [("B5", RED)]),
    ])
    # generated report + lock file must be ignored by discovery
    make_workbook(root / "Week 30" / "Final Official vs AI Location Report.xlsx",
                  [("X", "x", [("B2", GREY)])])
    (root / "Week 30" / "~$lock.xlsx").write_bytes(b"lock")
    # week 31: images across two worksheets; dates agree; BLUE is a cross-week duplicate
    make_workbook(root / "week 31" / "Deck Photos -09 AUG 2026 Part 1 (1).xlsx", [
        ("AUG 09 2026", banner("09 AUG 2026"), [("B5", GREY)]),
        ("AUG 09 2026 (2)", banner("09 AUG 2026"), [("B5", BLUE), ("B20", png_bytes((1, 2, 3)))]),
    ])
    # week 34: banner and sheet name disagree -> date must be blank
    make_workbook(root / "week 34" / "Deck Photos - AUG 2026 Part 1 (1).xlsx", [
        ("AUG 30 2026", banner("23 AUG 2026"), [("B5", png_bytes((7, 7, 7)))]),
    ])
    return root


def hashes(root):
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def register_rows(path, sheet="Image Register"):
    wb = openpyxl.load_workbook(path)
    rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]
    wb.close()
    return rows


# --------------------------------------------------------------------------- collection


def test_multiple_images_in_one_worksheet_in_reading_order(project):
    run = ie.collect(project, weeks={30})
    w30 = [r for r in run.records if r.week_number == 30]
    assert [(r.workbook[-8:], r.anchor_row, r.anchor_col) for r in w30] == [
        ("(1).xlsx", 5, 2), ("(1).xlsx", 12, 2), ("(1).xlsx", 12, 8), ("(2).xlsx", 5, 2)]
    assert [r.serial_number for r in w30] == [1, 2, 3, 4]
    assert [r.image_id for r in w30] == ["W30-0001", "W30-0002", "W30-0003", "W30-0004"]
    assert [r.source_section_heading for r in w30] == ["Not Labelled", "MAIN DECK", "MAIN DECK", "Not Labelled"]
    assert all(r.worksheet == "AUG 02 2026" for r in w30)
    # generated "Final ..." report and lock file are not sources
    assert {w.path.name for w in run.workbooks if w.week_number == 30} == {
        "Deck Photos -02 AUG 2026 Part 1 (1).xlsx", "Deck Photos -02 AUG 2026 Part 1 (2).xlsx"}


def test_images_across_multiple_worksheets(project):
    run = ie.collect(project, weeks={31})
    assert [(r.worksheet, r.serial_number) for r in run.records] == [
        ("AUG 09 2026", 1), ("AUG 09 2026 (2)", 2), ("AUG 09 2026 (2)", 3)]
    assert run.workbooks[0].worksheets == ["AUG 09 2026", "AUG 09 2026 (2)"]
    assert all(r.date == date(2026, 8, 9) for r in run.records)


def test_original_bytes_and_sha256_preserved(project):
    run = ie.collect(project, weeks={30})
    first = run.records[0]
    assert first.image_bytes == RED and first.sha256 == hashlib.sha256(RED).hexdigest()
    assert first.image_format == "png" and first.byte_size == len(RED)


def test_duplicates_identified_but_every_appearance_kept(project):
    run = ie.collect(project)
    by_id = {r.image_id: r for r in run.records}
    # RED: W30-0001 original, W30-0004 same-week duplicate (different workbook)
    assert by_id["W30-0001"].duplicate_of == ""
    assert (by_id["W30-0004"].duplicate_of, by_id["W30-0004"].duplicate_scope) == ("W30-0001", "SAME_WEEK")
    # BLUE: W30-0003 original, W31-0002 cross-week duplicate
    assert (by_id["W31-0002"].duplicate_of, by_id["W31-0002"].duplicate_scope) == ("W30-0003", "CROSS_WEEK")
    assert by_id["W31-0002"].workbook.startswith("Deck Photos -09 AUG")  # own source kept
    assert len(run.records) == 8
    assert len(run.unique_hashes) == 6
    assert len(run.duplicate_appearances) == 2


def test_repeat_within_later_week_is_same_week_duplicate_of_that_weeks_copy(tmp_path):
    root = tmp_path / "p"
    make_workbook(root / "Week 30" / "a (1).xlsx", [("S", "x", [("B5", RED)])])
    make_workbook(root / "Week 31" / "b (1).xlsx", [("S", "x", [("B5", RED), ("B9", RED)])])
    run = ie.collect(root)
    got = [(r.image_id, r.duplicate_of, r.duplicate_scope) for r in run.records]
    assert got == [("W30-0001", "", ""), ("W31-0001", "W30-0001", "CROSS_WEEK"),
                   ("W31-0002", "W31-0001", "SAME_WEEK")]


def test_ambiguous_date_left_blank_with_reason(project):
    run = ie.collect(project, weeks={34})
    r = run.records[0]
    assert r.date is None
    assert "disagree" in r.date_status
    assert ("DATE_NOT_CONFIRMED", "week 34") in [(c, w) for c, w, _ in run.issues]


def test_missing_date_left_blank(tmp_path):
    root = tmp_path / "p"
    make_workbook(root / "Week 40" / "x (1).xlsx", [("Sheet1", "no date here", [("B5", RED)])])
    run = ie.collect(root)
    assert run.records[0].date is None
    assert "Neither banner nor sheet name" in run.records[0].date_status


def test_week_date_blank_when_worksheets_disagree(tmp_path):
    root = tmp_path / "p"
    make_workbook(root / "Week 41" / "x (1).xlsx", [
        ("OCT 11 2026", banner("11 OCT 2026"), [("B5", RED)]),
        ("OCT 12 2026", banner("12 OCT 2026"), [("B5", GREEN)]),
    ])
    run = ie.collect(root)
    assert [r.date for r in run.records] == [None, None]
    assert "disagree" in run.week_dates[41][1]


def test_only_existing_weeks_discovered(project):
    run = ie.collect(project)
    assert [wn for wn, _ in run.weeks] == [30, 31, 34]


# --------------------------------------------------------------------------- failures


def test_corrupt_workbook_reported_and_other_workbooks_still_processed(project):
    bad = project / "Week 30" / "Deck Photos -02 AUG 2026 Part 1 (3).xlsx"
    bad.write_bytes(b"not an xlsx")
    run = ie.collect(project)
    failed = [w for w in run.workbooks if w.error]
    assert [w.path.name for w in failed] == [bad.name]
    assert any(c == "WORKBOOK_FAILED" and bad.name in w for c, w, _ in run.issues)
    assert len(run.records) == 8  # everything else extracted
    # a week with an unreadable workbook can't confirm its date
    assert all(r.date is None for r in run.records if r.week_number == 30)


def test_unreadable_image_recorded_as_failed_not_skipped(project, monkeypatch):
    real_load = ie.openpyxl.load_workbook

    def load(path, *a, **k):
        wb = real_load(path, *a, **k)
        if Path(path).name.endswith("(1).xlsx") and "week 31" in str(path):
            img = wb.worksheets[1]._images[0]
            img._data = lambda: (_ for _ in ()).throw(OSError("simulated broken image part"))
        return wb

    monkeypatch.setattr(ie.openpyxl, "load_workbook", load)
    out = project.parent / "out"
    run = ie.run_extraction(project, out)
    failed = run.failed
    assert [(r.image_id, r.status) for r in failed] == [("W31-0002", "EXTRACTION_FAILED")]
    assert "simulated broken image part" in failed[0].error
    assert ("IMAGE_FAILED" in [c for c, _, _ in run.issues])
    rows = register_rows(out / ie.REGISTER_FILENAME)
    row = next(r for r in rows if r[0] == "W31-0002")
    assert row[6] is None  # no filename for a failed image
    details = register_rows(out / ie.REGISTER_FILENAME, "Extraction Details")
    assert next(r for r in details if r[0] == "W31-0002")[1] == "EXTRACTION_FAILED"
    assert not (out / "Extracted_Images" / "Week_31" / "W31_0002.png").exists()


# --------------------------------------------------------------------------- output


def test_full_run_writes_images_and_register(project):
    out = project.parent / "out"
    before = hashes(project)
    run = ie.run_extraction(project, out)
    assert hashes(project) == before  # source workbooks (and everything else) untouched
    assert all(w.unchanged for w in run.workbooks)

    assert sorted(p.name for p in (out / "Extracted_Images").iterdir()) == ["Week_30", "Week_31", "Week_34"]
    f = out / "Extracted_Images" / "Week_30" / "W30_0001.png"
    assert f.read_bytes() == RED
    assert len(list((out / "Extracted_Images").rglob("*.png"))) == 8  # duplicates kept too

    rows = register_rows(out / ie.REGISTER_FILENAME)
    assert rows[0] == ie.REGISTER_HEADERS
    assert len(rows[0]) == 11
    assert len(rows) - 1 == len(run.records) == 8
    first = rows[1]
    assert first[:4] == ["W30-0001", 30, "Deck Photos -02 AUG 2026 Part 1 (1).xlsx", "AUG 02 2026"]
    assert first[4].date() == date(2026, 8, 2)
    assert first[5:7] == [1, "W30_0001.png"]
    assert all(r[7:11] == [None, None, None, None] for r in rows[1:])  # AI columns blank
    w34 = next(r for r in rows if r[0] == "W34-0001")
    assert w34[4] is None

    wb = openpyxl.load_workbook(out / ie.REGISTER_FILENAME)
    assert wb.sheetnames == ["Image Register", "Extraction Details", "Summary", "Workbooks", "Issues"]
    summary = dict(wb["Summary"].iter_rows(min_row=2, values_only=True))
    assert summary["Images extracted"] == 8
    assert summary["Unique images (SHA-256)"] == 6
    assert summary["Duplicate image appearances (total)"] == 2
    assert summary["Images in Week 31"] == 3
    wb.close()


def test_refuses_non_empty_output_dir(project):
    out = project.parent / "out"
    out.mkdir()
    (out / "keep.txt").write_text("existing")
    with pytest.raises(ie.OutputExistsError):
        ie.run_extraction(project, out)
    assert (out / "keep.txt").read_text() == "existing"
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]


def test_refuses_output_inside_source_week_folder(project):
    with pytest.raises(ie.OutputExistsError):
        ie.run_extraction(project, project / "Week 30" / "out")
    assert not (project / "Week 30" / "out").exists()


def test_extraction_module_imports_no_ai_or_env_code():
    import subprocess
    code = ("import sys; sys.path.insert(0, '.'); import extract_images; "
            "bad=[m for m in sys.modules if m.split('.')[0] in "
            "('google','groq','dotenv','requests','httpx') or m in ('src.vision_client','src.classifier','src.config','run_pipeline')]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(ie.__file__).resolve().parent.parent,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
