"""src/register_input.py -- loading, validating and re-hashing completed weekly registers.

Registers are produced by the real extraction-only code (weekly_image_report) from synthetic
temporary workbooks. No real project data, no .env, no network, no AI."""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import discovery, register_input  # noqa: E402
from src import weekly_image_report as wr  # noqa: E402
from test_image_extraction import banner, hashes, make_workbook, png_bytes  # noqa: E402

RED, YELLOW, GREEN, BLUE = (png_bytes(c) for c in [(200, 0, 0), (220, 220, 0), (0, 160, 0), (0, 0, 200)])


def build_week30(root: Path) -> Path:
    """Week 30 rows: W30-0001 RED (no heading), W30-0002 YELLOW (no heading), W30-0003 GREEN and
    W30-0004 BLUE ("MAIN DECK"), W30-0005 RED again, above part (2)'s first heading -> no
    heading of its own -> blank (a same-week repeat of a blank row)."""
    make_workbook(root / "Week 30" / "Deck Photos -02 AUG 2026 Part 1 (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"),
         [("B5", RED), ("B6", YELLOW), ("B12", GREEN), ("H12", BLUE)], [(10, "MAIN DECK")]),
    ])
    make_workbook(root / "Week 30" / "Deck Photos -02 AUG 2026 Part 1 (2).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"), [("B5", RED)],
         [(10, "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW")]),
    ])
    return root


@pytest.fixture
def week30(tmp_path):
    root = build_week30(tmp_path / "project")
    wr.generate_weekly_reports(root)
    [week] = discovery.find_week_folders(root)
    return week


def test_rows_map_image_ids_locations_and_bytes(week30):
    rows = register_input.load_register_week(week30)
    assert [r.image_id for r in rows] == [f"W30-000{i}" for i in range(1, 6)]
    assert [r.serial_number for r in rows] == [1, 2, 3, 4, 5]
    assert [r.image_filename for r in rows] == [f"W30-000{i}.png" for i in range(1, 6)]
    assert [r.official_location for r in rows] == [None, None, "MAIN DECK", "MAIN DECK", None]
    assert [r.needs_ai for r in rows] == [True, True, False, False, True]
    assert [r.image_bytes for r in rows] == [RED, YELLOW, GREEN, BLUE, RED]
    for r in rows:
        assert r.sha256 == hashlib.sha256(r.image_path.read_bytes()).hexdigest()
        assert r.image_path == week30.path / "Extracted Images - New" / r.image_filename
        assert r.report_date == "8/2/2026" and r.worksheet_name == "AUG 02 2026"
    assert rows[0].source_workbook == "Deck Photos -02 AUG 2026 Part 1 (1).xlsx"
    assert rows[0].sha256 == rows[4].sha256  # the same-week duplicate keeps its own row


def test_official_location_kept_verbatim(tmp_path):
    heading = "  Cross deck  Fwd of # 1 (port side)"
    make_workbook(tmp_path / "Week 30" / "Deck Photos -02 AUG 2026 Part 1 (1).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"), [("B12", GREEN)], [(10, heading)]),
    ])
    wr.generate_weekly_reports(tmp_path)
    [week] = discovery.find_week_folders(tmp_path)
    [row] = register_input.load_register_week(week)
    # exactly what the register holds (the extraction stage strips the heading cell's ends)
    assert row.official_location == heading.strip()


def test_ambiguous_date_text_kept(tmp_path):
    make_workbook(tmp_path / "week 34" / "Deck Photos - AUG 2026 Part 1 (1).xlsx", [
        ("AUG 30 2026", banner("23 AUG 2026"), [("B5", RED)]),
    ])
    wr.generate_weekly_reports(tmp_path)
    [week] = discovery.find_week_folders(tmp_path)
    [row] = register_input.load_register_week(week)
    assert row.report_date == "AMBIGUOUS/PENDING" and row.image_id == "W34-0001"


def test_loading_is_read_only(week30, tmp_path):
    root = tmp_path / "project"
    before = hashes(root)
    register_input.load_register_week(week30)
    assert hashes(root) == before  # same files, same bytes: nothing written, renamed or added


def test_missing_register_is_refused(tmp_path):
    build_week30(tmp_path)  # no registers generated
    [week] = discovery.find_week_folders(tmp_path)
    with pytest.raises(register_input.RegisterInputError, match="is missing"):
        register_input.load_register_week(week)


def test_photo_hash_mismatch_is_refused(week30):
    (week30.path / "Extracted Images - New" / "W30-0002.png").write_bytes(BLUE)
    with pytest.raises(register_input.RegisterInputError, match="not complete.*differs from the embedded photo"):
        register_input.load_register_week(week30)


def test_missing_photo_is_refused(week30):
    (week30.path / "Extracted Images - New" / "W30-0003.png").unlink()
    with pytest.raises(register_input.RegisterInputError, match="not complete"):
        register_input.load_register_week(week30)


def test_register_with_fewer_rows_than_source_images_is_refused(week30):
    # a new embedded photo in a source workbook makes the existing register incomplete
    make_workbook(week30.path / "Deck Photos -02 AUG 2026 Part 1 (3).xlsx", [
        ("AUG 02 2026", banner("02 AUG 2026"), [("B5", GREEN)]),
    ])
    [week] = discovery.find_week_folders(week30.path.parent)
    with pytest.raises(register_input.RegisterInputError, match="5 data rows, expected 6"):
        register_input.load_register_week(week)


def test_photo_is_rehashed_even_after_validation(week30, monkeypatch):
    """The bytes returned are re-read and re-hashed here -- a file changed after
    check_completed_week() passed is still refused."""
    real_check = wr.check_completed_week

    def check_then_tamper(*args):
        problems = real_check(*args)
        (week30.path / "Extracted Images - New" / "W30-0004.png").write_bytes(RED)
        return problems

    monkeypatch.setattr(register_input.wr, "check_completed_week", check_then_tamper)
    with pytest.raises(register_input.RegisterInputError,
                       match="W30-0004.png SHA-256 does not match the register's embedded photo"):
        register_input.load_register_week(week30)


def test_unreadable_source_workbooks_are_refused(week30):
    week = discovery.WeekFolder(week30.name, week30.path, [week30.path / "missing.xlsx"])
    with pytest.raises(register_input.RegisterInputError, match="source workbooks unreadable"):
        register_input.load_register_week(week)


def test_week_without_source_workbooks_is_refused(tmp_path):
    (tmp_path / "Week 30").mkdir()
    [week] = discovery.find_week_folders(tmp_path)
    with pytest.raises(register_input.RegisterInputError, match="no source workbooks"):
        register_input.load_register_week(week)


def test_image_id_must_match_week_and_serial(week30, monkeypatch):
    # simulate a register whose filename is not this week's WNN-NNNN (validation passes)
    monkeypatch.setattr(register_input.wr, "check_completed_week", lambda *a: [])
    real_load = register_input.openpyxl.load_workbook

    def load_with_wrong_id(path, **kwargs):
        wb = real_load(path, **kwargs)
        if Path(path).name == "Week_30_Image_Register.xlsx":  # in memory only, never saved
            wb["Image Register"].cell(3, 2).value = "W31-0002.png"
        return wb

    monkeypatch.setattr(register_input.openpyxl, "load_workbook", load_with_wrong_id)
    with pytest.raises(register_input.RegisterInputError, match="'W31-0002.png' is not W30-0002"):
        register_input.load_register_week(week30)


def test_module_imports_no_ai_or_env_code():
    import ast
    tree = ast.parse(Path(register_input.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {node.module or ""} | {a.name for a in node.names}
    assert imported <= {"__future__", "annotations", "hashlib", "dataclasses", "dataclass", "field",
                        "pathlib", "Path", "openpyxl", "", "discovery", "weekly_image_report"}
