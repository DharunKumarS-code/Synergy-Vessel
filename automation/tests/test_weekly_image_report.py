"""Weekly extraction-only reports (src/weekly_image_report.py, extract_images.py), written
directly into each week folder. Temporary workbooks only -- no real project data, no .env, no
network, no AI."""

import socket
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import extract_images  # noqa: E402
from src import discovery  # noqa: E402
from src import image_extraction as ie  # noqa: E402
from src import weekly_image_report as wr  # noqa: E402
from test_image_extraction import (BLUE, GREEN, RED, banner, hashes, make_workbook,  # noqa: E402
                                   png_bytes, project)  # noqa: F401 -- fixture

AUTOMATION_OUTPUT = Path(__file__).resolve().parent.parent / "output"

EXPECTED_HEADERS = [
    "Serial Number", "Image Filename", "Report Date", "Original Excel Filename",
    "Worksheet Name", "Official Location", "Extracted Photograph", "Activity Identified",
    "Location Confidence",
]


def open_register(path):
    wb = openpyxl.load_workbook(path)
    return wb, wb["Image Register"]


def test_single_sheet_nine_columns_no_ai_suggested_location(project):
    _, outs, _ = wr.generate_weekly_reports(project)
    for o in outs:
        wb, ws = open_register(o.report_path)
        assert wb.sheetnames == ["Image Register"] and ws.sheet_state == "visible"
        header = [c.value for c in ws[1]]
        assert header == EXPECTED_HEADERS and ws.max_column == 9
        assert "AI Suggested Location" not in header
        wb.close()


def test_register_in_week_folder_photos_in_new_images_folder(project):
    original = project / "Week 30" / "Extracted Images"
    original.mkdir()
    (original / "W30-0001.png").write_bytes(b"original photo")  # pre-existing original folder
    before_dirs = {p for p in project.rglob("*") if p.is_dir()}
    _, outs, _ = wr.generate_weekly_reports(project)
    new_dir = project / "Week 30" / "Extracted Images - New"
    # the only new folders are the per-week "Extracted Images - New" folders
    assert {p for p in project.rglob("*") if p.is_dir()} - before_dirs == {
        project / f / "Extracted Images - New" for f in ("Week 30", "week 31", "week 34")}
    o30 = outs[0]
    assert o30.report_path == project / "Week 30" / "Week_30_Image_Register.xlsx"
    assert sorted(p.name for p in new_dir.iterdir()) == [
        "W30-0001.png", "W30-0002.png", "W30-0003.png", "W30-0004.png"]
    assert (new_dir / "W30-0001.png").read_bytes() == RED
    assert not any(p.suffix == ".png" for p in (project / "Week 30").iterdir())  # none at the root
    # the original "Extracted Images" folder is untouched
    assert [p.name for p in original.iterdir()] == ["W30-0001.png"]
    assert (original / "W30-0001.png").read_bytes() == b"original photo"
    assert (project / "week 31" / "Week_31_Image_Register.xlsx").is_file()


def test_reference_formatting_preserved(project):
    _, outs, _ = wr.generate_weekly_reports(project)
    wb, ws = open_register(outs[0].report_path)
    h = ws["A1"]
    assert (h.font.name, h.font.sz, h.font.b, h.font.color.rgb) == ("Arial", 10, True, "FFFFFFFF")
    assert (h.fill.fgColor.theme, round(h.fill.fgColor.tint, 4)) == (1, 0.25)
    assert (h.alignment.horizontal, h.alignment.vertical, h.alignment.wrap_text) == ("center", "center", True)
    d = ws["B2"]
    assert (d.font.name, d.font.sz, d.alignment.vertical, d.alignment.wrap_text) == ("Arial", 10, "top", True)
    assert d.border.left.style is None
    assert {k: ws.column_dimensions[k].width for k in "ABCDEFGHI"} == wr.COLUMN_WIDTHS
    assert ws.row_dimensions[1].height == 24 and ws.row_dimensions[2].height == 107.25
    wb.close()


def test_weekly_counts_and_each_report_holds_only_its_week(project):
    _, outs, _ = wr.generate_weekly_reports(project)
    assert {o.week_number: len(o.records) for o in outs} == {30: 4, 31: 3, 34: 1}
    for o in outs:
        wb, ws = open_register(o.report_path)
        assert ws.max_row - 1 == len(o.records)
        assert all(c.value.startswith(f"W{o.week_number}-") for c in ws["B"][1:])
        wb.close()
        assert wr.verify_week_output(o) == []


def test_photos_embedded_aligned_and_bytes_preserved(project):
    _, outs, _ = wr.generate_weekly_reports(project)
    wb, ws = open_register(outs[0].report_path)
    assert len(ws._images) == 4
    by_row = {im.anchor._from.row + 1: im for im in ws._images}
    for r, data in zip([2, 3, 4, 5], [RED, GREEN, BLUE, RED]):  # reading order, duplicate kept
        im = by_row[r]
        assert im.anchor._from.col == 6 and im.anchor._from.colOff == 0 and im.anchor._from.rowOff == 0
        assert im._data() == data
        assert ws.cell(r, 7).value is None  # a photo, not a path
        assert round(im.anchor.ext.width / 9525) == 180
    wb.close()


def test_tall_photo_stays_inside_its_row(tmp_path):
    root = tmp_path / "p"
    tall = png_bytes((5, 5, 5), size=(30, 60))
    make_workbook(root / "Week 40" / "x (1).xlsx", [("OCT 04 2026", banner("04 OCT 2026"), [("B5", tall), ("B9", RED)])])
    _, outs, _ = wr.generate_weekly_reports(root)
    wb, ws = open_register(outs[0].report_path)
    im = next(i for i in ws._images if i.anchor._from.row == 1)
    assert im.anchor.ext.height / 9525 == 360
    assert ws.row_dimensions[2].height >= 360 * 0.75
    wb.close()


def test_official_location_populated_or_truly_blank(project):
    _, outs, _ = wr.generate_weekly_reports(project)
    wb, ws = open_register(outs[0].report_path)
    # B5 (no heading above) blank; B12 / H12 under "MAIN DECK"; part (2) B5 blank
    assert [ws.cell(r, 6).value for r in range(2, 6)] == [None, "MAIN DECK", "MAIN DECK", None]
    values = {c.value for col in ws.iter_cols(min_row=2) for c in col}
    assert not values & {"Not Labelled", "Unknown", "N/A", ""}
    wb.close()


def test_same_photo_under_two_different_headings_is_ambiguous_and_blank(tmp_path):
    root = tmp_path / "p"
    make_workbook(root / "Week 30" / "x (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"),
         [("B5", RED), ("B12", RED), ("H12", GREEN), ("B30", RED)], [(10, "MAIN DECK"), (25, "PORT VIEW")]),
    ])
    _, outs, _ = wr.generate_weekly_reports(root)
    wb, ws = open_register(outs[0].report_path)
    # RED: no heading, then "MAIN DECK", then "PORT VIEW" -> two real headings -> every copy blank
    assert [ws.cell(r, 6).value for r in range(2, 6)] == [None, None, "MAIN DECK", None]
    assert ws.max_row == 5 and len(ws._images) == 4  # the duplicate rows are still kept
    wb.close()


def test_repeat_under_a_heading_keeps_it_when_other_copy_has_none(tmp_path):
    # Real-data shape (rows 81/82 of Weeks 30-37, 97/98 of Weeks 38, 40-42): a photo first appears
    # above the first heading, then again under "PORT AND STARBOARD ...". "No heading" is not a
    # conflicting location: the repeat keeps its own heading; the first copy stays blank (no copy).
    root = tmp_path / "p"
    heading = "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW"
    make_workbook(root / "Week 30" / "x (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"),
         [("B5", RED), ("H5", BLUE), ("B12", GREEN), ("B30", RED), ("H30", BLUE)],
         [(10, "MAIN DECK"), (25, heading)]),
    ])
    _, outs, _ = wr.generate_weekly_reports(root)
    assert outs[0].locations == {"W30-0001": None, "W30-0002": None, "W30-0003": "MAIN DECK",
                                 "W30-0004": heading, "W30-0005": heading}
    wb, ws = open_register(outs[0].report_path)
    assert [ws.cell(r, 6).value for r in range(2, 7)] == [None, None, "MAIN DECK", heading, heading]
    assert wr.verify_week_output(outs[0]) == []
    wb.close()


def test_same_photo_under_the_same_heading_twice_keeps_it(tmp_path):
    root = tmp_path / "p"
    make_workbook(root / "Week 30" / "x (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"), [("B12", RED), ("B20", RED)], [(10, "MAIN DECK")]),
    ])
    _, outs, _ = wr.generate_weekly_reports(root)
    assert list(outs[0].locations.values()) == ["MAIN DECK", "MAIN DECK"]


def test_blank_copies_stay_blank_and_nothing_is_propagated(tmp_path):
    # two copies with no heading stay blank even though the week has headings elsewhere
    root = tmp_path / "p"
    make_workbook(root / "Week 30" / "x (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"), [("B5", RED), ("B6", RED), ("B12", GREEN)], [(10, "MAIN DECK")]),
    ])
    _, outs, _ = wr.generate_weekly_reports(root)
    assert list(outs[0].locations.values()) == [None, None, "MAIN DECK"]


def test_original_heading_wording_preserved(tmp_path):
    root = tmp_path / "p"
    make_workbook(root / "Week 30" / "x (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"), [("B12", GREEN)], [(10, "Cross deck Between # 1&2")]),
    ])
    _, outs, _ = wr.generate_weekly_reports(root)
    wb, ws = open_register(outs[0].report_path)
    assert ws.cell(2, 6).value == "Cross deck Between # 1&2"
    wb.close()


def test_week34_date_ambiguity_preserved(project):
    _, outs, _ = wr.generate_weekly_reports(project)
    o34 = next(o for o in outs if o.week_number == 34)
    wb, ws = open_register(o34.report_path)
    row = [c.value for c in ws[2]]
    assert row[1] == "W34-0001.png" and row[2] == "AMBIGUOUS/PENDING"
    assert row[7] is None and row[8] is None
    wb.close()


def test_extraction_failure_is_a_visible_row(project, monkeypatch):
    real_load = ie.openpyxl.load_workbook

    def load(path, *a, **k):
        wb = real_load(path, *a, **k)
        if Path(path).name.startswith("Deck Photos -09 AUG"):
            wb.worksheets[1]._images[0]._data = lambda: (_ for _ in ()).throw(OSError("broken part"))
        return wb

    monkeypatch.setattr(ie.openpyxl, "load_workbook", load)
    _, outs, _ = wr.generate_weekly_reports(project)
    o31 = next(o for o in outs if o.week_number == 31)
    wb, ws = open_register(o31.report_path)
    assert ws.max_row - 1 == 3
    assert ws.cell(3, 7).value == "EXTRACTION FAILED" and ws.cell(3, 2).value is None
    assert 3 not in {im.anchor._from.row + 1 for im in ws._images}
    wb.close()
    monkeypatch.undo()
    assert wr.verify_week_output(o31) == []
    assert not (project / "week 31" / "Extracted Images - New" / "W31-0002.png").exists()


def test_unrelated_existing_files_never_overwritten_week_needs_attention(project):
    (project / "Week 30" / "Extracted Images - New").mkdir()
    taken_img = project / "Week 30" / "Extracted Images - New" / "W30-0001.png"
    taken_img.write_bytes(b"someone else's file")
    taken_reg = project / "Week 30" / "Week_30_Image_Register.xlsx"
    taken_reg.write_bytes(b"existing register")
    run, outs, statuses = wr.generate_weekly_reports(project)
    assert taken_img.read_bytes() == b"someone else's file"
    assert taken_reg.read_bytes() == b"existing register"
    assert sorted(p.name for p in (project / "Week 30").rglob("*_v2*")) == []
    st = {s.week_number: s for s in statuses}
    assert st[30].status == wr.WEEK_ATTENTION and "cannot be opened" in st[30].reasons[0]
    assert [o.week_number for o in outs] == [31, 34]  # the other weeks still processed


def test_sources_untouched_and_registers_never_rediscovered(project):
    before = hashes(project)
    wr.generate_weekly_reports(project)
    after = hashes(project)
    assert {p: h for p, h in after.items() if p in before} == before
    run2, outs2, statuses2 = wr.generate_weekly_reports(project)
    assert run2.workbooks == [] and outs2 == []  # nothing re-read for extraction
    assert all(s.status == wr.WEEK_COMPLETE for s in statuses2)


# --------------------------------------------------------------------------- re-runs


def test_rerun_skips_complete_weeks_with_zero_changes(project):
    wr.generate_weekly_reports(project)
    snapshot = hashes(project)
    mtimes = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}
    run, outs, statuses = wr.generate_weekly_reports(project)
    assert outs == [] and run.records == []
    assert {s.week_number: s.status for s in statuses} == {30: "COMPLETE", 31: "COMPLETE", 34: "COMPLETE"}
    assert hashes(project) == snapshot  # no file added, removed or changed
    assert {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()} == mtimes
    assert not list(project.rglob("*_v2*"))


def test_weeks_option_restricts_and_still_skips_complete(project):
    _, outs, statuses = wr.generate_weekly_reports(project, weeks={30})
    assert [o.week_number for o in outs] == [30] and [s.week_number for s in statuses] == [30]
    assert not (project / "week 31" / "Week_31_Image_Register.xlsx").exists()
    _, outs, statuses = wr.generate_weekly_reports(project, weeks={30, 31})
    assert [o.week_number for o in outs] == [31]
    assert {s.week_number: s.status for s in statuses} == {30: "COMPLETE", 31: "NEW"}


def test_new_week_added_later_is_extracted_alongside_complete_weeks(project):
    wr.generate_weekly_reports(project)
    make_workbook(project / "Week 43" / "Deck Photos - NOV 01 2026 Part 1 (1).xlsx", [
        ("NOV 01 2026", banner("01 NOV 2026"), [("B5", GREEN), ("B9", BLUE)]),
    ])
    _, outs, statuses = wr.generate_weekly_reports(project)
    assert [o.week_number for o in outs] == [43]
    assert {s.week_number: s.status for s in statuses}[43] == "NEW"
    assert wr.verify_week_output(outs[0]) == []
    assert sorted(p.name for p in (project / "Week 43" / "Extracted Images - New").iterdir()) == [
        "W43-0001.png", "W43-0002.png"]


def _complete_then(project, damage):
    wr.generate_weekly_reports(project)
    damage(project / "Week 30")
    before = hashes(project)
    _, outs, statuses = wr.generate_weekly_reports(project)
    st = {s.week_number: s for s in statuses}
    assert outs == [] and hashes(project) == before  # nothing repaired, overwritten or added
    assert st[31].status == st[34].status == "COMPLETE"
    assert st[30].status == "NEEDS_ATTENTION"
    return st[30].reasons


def test_missing_register_with_photos_needs_attention(project):
    reasons = _complete_then(project, lambda w: (w / "Week_30_Image_Register.xlsx").unlink())
    assert reasons == ["Week_30_Image_Register.xlsx is missing"]


def test_missing_photo_needs_attention(project):
    reasons = _complete_then(project, lambda w: (w / "Extracted Images - New" / "W30-0002.png").unlink())
    assert "do not match" in reasons[0]


def test_extra_v2_photo_needs_attention(project):
    reasons = _complete_then(project, lambda w: (w / "Extracted Images - New" / "W30-0001_v2.png").write_bytes(RED))
    assert "do not match" in reasons[0]


def test_photo_hash_mismatch_needs_attention(project):
    reasons = _complete_then(project, lambda w: (w / "Extracted Images - New" / "W30-0003.png").write_bytes(GREEN))
    assert reasons == ["row 4: W30-0003.png differs from the embedded photo (SHA-256)"]


def test_register_row_count_mismatch_needs_attention(project):
    def add_source_image(w):  # source now has 5 images; the register still has 4
        make_workbook(w / "Deck Photos -02 AUG 2026 Part 1 (3).xlsx",
                      [("AUG 02 2026", banner("02 AUG 2026"), [("B5", png_bytes((9, 9, 9)))])])
    reasons = _complete_then(project, add_source_image)
    assert any("4 data rows, expected 5" in r for r in reasons)


def test_register_with_extra_sheet_needs_attention(project):
    def add_sheet(w):
        reg = w / "Week_30_Image_Register.xlsx"
        wb = openpyxl.load_workbook(reg)
        wb.create_sheet("Extra")
        wb.save(reg)
    reasons = _complete_then(project, add_sheet)
    assert "expected only 'Image Register'" in reasons[0]


def test_empty_new_images_folder_and_no_register_is_new(project):
    (project / "Week 30" / "Extracted Images - New").mkdir()
    _, outs, statuses = wr.generate_weekly_reports(project, weeks={30})
    assert statuses[0].status == "NEW" and [o.week_number for o in outs] == [30]
    assert wr.verify_week_output(outs[0]) == []


def test_cli_rerun_reports_skipped_and_attention(project, monkeypatch, capsys):
    monkeypatch.setattr(extract_images, "block_network", lambda: None)
    assert extract_images.main(["--project-root", str(project)]) == 0
    capsys.readouterr()
    assert extract_images.main(["--project-root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "Weeks processed: none" in out
    assert "Weeks skipped as already complete: 30, 31, 34" in out
    assert "Weeks requiring attention: none" in out and "Photos extracted: 0" in out
    (project / "week 31" / "Extracted Images - New" / "W31-0001.png").unlink()
    assert extract_images.main(["--project-root", str(project)]) == 1
    out = capsys.readouterr().out
    assert "Weeks requiring attention: 31" in out and "Week 31 (week 31): register filenames" in out


def test_discovery_excludes_generated_registers(tmp_path):
    week = tmp_path / "Week 30"
    week.mkdir()
    for name in ["Deck Photos (1).xlsx", "Week_30_Image_Register.xlsx", "week_30_image_register_v2.xlsx",
                 "Week_30_Image_Register_notes.xlsx"]:
        (week / name).write_bytes(b"x")
    assert [p.name for p in discovery.find_source_workbooks(week)] == [
        "Deck Photos (1).xlsx", "Week_30_Image_Register_notes.xlsx"]


def test_cli_writes_into_week_folders_not_automation_output(project, monkeypatch, capsys,
                                                            env_load_attempts):
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, getattr(socket.socket, name))
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    listing = lambda: sorted(AUTOMATION_OUTPUT.rglob("*")) if AUTOMATION_OUTPUT.exists() else []  # noqa: E731
    before = listing()
    assert extract_images.main(["--project-root", str(project)]) == 0
    assert listing() == before
    for folder, week in [("Week 30", 30), ("week 31", 31), ("week 34", 34)]:
        assert (project / folder / f"Week_{week}_Image_Register.xlsx").is_file()
    out = capsys.readouterr().out
    assert "Images extracted: 8" in out and "PROBLEMS" not in out
    with pytest.raises(extract_images.NetworkBlockedError):
        socket.create_connection(("example.com", 80))
    assert env_load_attempts == []


def test_weekly_module_imports_no_ai_or_env_code():
    import subprocess
    code = ("import sys; sys.path.insert(0, '.'); import extract_images; "
            "bad=[m for m in sys.modules if m.split('.')[0] in "
            "('google','groq','dotenv','requests','httpx') or m in "
            "('src.vision_client','src.classifier','src.config','src.report_builder','run_pipeline')]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(wr.__file__).resolve().parent.parent,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
