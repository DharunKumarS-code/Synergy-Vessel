"""Phase P2-3: offline tests for src/discovery.py -- week-folder and source-workbook discovery.

Everything runs in tmp_path with synthetic folders and files; the real project tree and its
original inspection workbooks are never touched. discovery.py only lists names (it never opens a
workbook), so most "workbooks" here are placeholder bytes; one test uses real openpyxl files.

The two defects P2-3 recorded as xfail(strict=True) (text week ordering, folders named *.xlsx)
were fixed in the P2 remediation phase; those tests now pass as ordinary tests.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl
import pytest

from src import discovery


def _dirs(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir()


def _files(folder: Path, *names: str) -> None:
    for name in names:
        (folder / name).write_bytes(b"placeholder - discovery never opens it")


def _names(weeks) -> list[str]:
    return [w.name for w in weeks]


# --- 1. weekly folder discovery / 13. case-insensitive matching ----------------------------


def test_discovers_week_folders_case_insensitively_and_ignores_everything_else(tmp_path):
    _dirs(tmp_path, "Week 30", "week 31", "WEEK 32", "Week33", "automation", "Archive", "Weekly 34",
          "Week 35 - copy", "Week 3a", "Week", "My Week 36")
    (tmp_path / "Week 37").write_text("a FILE named like a week is not a week folder")
    (tmp_path / "Week 38.xlsx").write_bytes(b"x")
    assert _names(discovery.find_week_folders(tmp_path)) == ["Week 30", "week 31", "WEEK 32", "Week33"]


def test_week_folder_carries_path_and_its_workbooks(tmp_path):
    _dirs(tmp_path, "Week 30")
    _files(tmp_path / "Week 30", "Report (1).xlsx")
    [week] = discovery.find_week_folders(tmp_path)
    assert week.path == tmp_path / "Week 30"
    assert week.source_workbooks == [tmp_path / "Week 30" / "Report (1).xlsx"]


def test_real_project_naming_pattern_is_recognised(tmp_path):
    # the real project mixes "Week 30" with lowercase "week 31" ... "week 42"
    real = ["Week 30"] + [f"week {n}" for n in (31, 34, 35, 36, 37, 38, 39, 40, 41, 42)]
    _dirs(tmp_path, *real)
    assert _names(discovery.find_week_folders(tmp_path)) == real


# --- 2. week ordering ---------------------------------------------------------------------


def test_same_width_week_numbers_are_in_numeric_order(tmp_path):
    _dirs(tmp_path, "week 42", "Week 30", "week 35", "week 31")
    assert _names(discovery.find_week_folders(tmp_path)) == ["Week 30", "week 31", "week 35", "week 42"]


def test_ordering_is_deterministic_regardless_of_creation_order(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    _dirs(a, "Week 2", "Week 1", "Week 3")
    _dirs(b, "Week 3", "Week 1", "Week 2")
    assert _names(discovery.find_week_folders(a)) == _names(discovery.find_week_folders(b))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path ordering ignores case")
def test_mixed_case_names_sort_case_insensitively_on_windows(tmp_path):
    _dirs(tmp_path, "week 32", "Week 31", "WEEK 30")
    assert _names(discovery.find_week_folders(tmp_path)) == ["WEEK 30", "Week 31", "week 32"]


def test_week_numbers_of_different_widths_sort_numerically(tmp_path):
    _dirs(tmp_path, "Week 10", "Week 9", "Week 100", "Week 2")
    assert _names(discovery.find_week_folders(tmp_path)) == ["Week 2", "Week 9", "Week 10", "Week 100"]


def test_numeric_order_example_from_specification(tmp_path):
    _dirs(tmp_path, "Week 30", "Week 10", "Week 2", "Week 9", "Week 1")
    assert _names(discovery.find_week_folders(tmp_path)) == ["Week 1", "Week 2", "Week 9", "Week 10", "Week 30"]


def test_numeric_order_ignores_case_and_spacing(tmp_path):
    _dirs(tmp_path, "week 10", "WEEK9", "Week   2", "week 100")
    assert _names(discovery.find_week_folders(tmp_path)) == ["Week   2", "WEEK9", "week 10", "week 100"]


def test_equal_week_numbers_have_a_stable_total_order(tmp_path):
    # "Week 030", "Week30", "week 30": same number -> case-folded name, then exact name
    # (names differ by more than case: Windows folder names are case-insensitive)
    names = ["Week30", "week 30", "Week 030", "WEEK  30"]
    roots = []
    for i, order in enumerate((names, list(reversed(names)))):
        root = tmp_path / str(i)
        root.mkdir()
        _dirs(root, *order)
        roots.append(_names(discovery.find_week_folders(root)))
    assert roots[0] == roots[1]
    numbers_then_names = sorted(roots[0], key=lambda n: (30, n.casefold(), n))
    assert roots[0] == numbers_then_names


def test_real_project_order_is_unchanged_by_numeric_sort(tmp_path):
    real = ["Week 30"] + [f"week {n}" for n in (31, 34, 35, 36, 37, 38, 39, 40, 41, 42)]
    _dirs(tmp_path, *reversed(real))
    assert _names(discovery.find_week_folders(tmp_path)) == real


def test_very_large_and_zero_padded_week_numbers(tmp_path):
    _dirs(tmp_path, "Week 0", "Week 007", "Week 99999999999999999999", "Week 8")
    assert _names(discovery.find_week_folders(tmp_path)) == ["Week 0", "Week 007", "Week 8",
                                                             "Week 99999999999999999999"]


# --- 12. duplicate / unusual folder names --------------------------------------------------


def test_unusual_but_valid_week_names(tmp_path):
    _dirs(tmp_path, "Week 030", "Week30", "Week   32")
    names = _names(discovery.find_week_folders(tmp_path))
    # each is its own folder, even where the numbers collide ("Week 030" vs "Week30")
    assert sorted(names) == sorted(["Week 030", "Week30", "Week   32"])


def test_leading_space_folder_name_is_matched_but_name_kept_verbatim(tmp_path):
    try:
        (tmp_path / " Week 5").mkdir()
    except OSError:
        pytest.skip("filesystem does not allow a leading space in a folder name")
    assert _names(discovery.find_week_folders(tmp_path)) == [" Week 5"]


def test_week_folders_are_not_searched_recursively(tmp_path):
    _dirs(tmp_path, "Archive")
    _dirs(tmp_path / "Archive", "Week 1")
    assert discovery.find_week_folders(tmp_path) == []


# --- 9. missing directories / 10. empty directories ----------------------------------------


def test_missing_root_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        discovery.find_week_folders(tmp_path / "does-not-exist")


def test_root_that_is_a_file_raises(tmp_path):
    (tmp_path / "root.txt").write_text("x")
    with pytest.raises(NotADirectoryError):
        discovery.find_week_folders(tmp_path / "root.txt")


def test_empty_root_finds_no_weeks(tmp_path):
    assert discovery.find_week_folders(tmp_path) == []


def test_empty_week_folder_is_discovered_with_no_workbooks(tmp_path):
    _dirs(tmp_path, "Week 1")
    [week] = discovery.find_week_folders(tmp_path)
    assert week.source_workbooks == []


def test_find_source_workbooks_on_missing_folder_returns_empty(tmp_path):
    assert discovery.find_source_workbooks(tmp_path / "gone") == []


# --- 3. workbook discovery (real synthetic Excel files) ------------------------------------


def test_discovers_real_synthetic_workbooks(tmp_path):
    week = tmp_path / "Week 1"
    week.mkdir()
    for name in ("Inspection (2).xlsx", "Inspection (1).xlsx"):
        wb = openpyxl.Workbook()
        wb.active["A1"] = name
        wb.save(week / name)
    found = discovery.find_source_workbooks(week)
    assert [p.name for p in found] == ["Inspection (1).xlsx", "Inspection (2).xlsx"]
    assert openpyxl.load_workbook(found[0]).active["A1"].value == "Inspection (1).xlsx"  # untouched


def test_workbooks_are_not_searched_recursively(tmp_path):
    week = tmp_path / "Week 1"
    (week / "Extracted Images").mkdir(parents=True)
    _files(week / "Extracted Images", "nested (1).xlsx")
    assert discovery.find_source_workbooks(week) == []


# --- 4. part ordering / 5. double-space filenames ------------------------------------------


def test_parts_sort_by_number_not_text(tmp_path):
    _files(tmp_path, "Report (10).xlsx", "Report (2).xlsx", "Report (1).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == [
        "Report (1).xlsx", "Report (2).xlsx", "Report (10).xlsx"]


def test_double_space_filenames_still_sort_by_part_number(tmp_path):
    # observed in real weeks: a double space before the year sorts "(2)" ahead of "(1)" as text
    _files(tmp_path, "Daily Report 25 Aug 2026 (2).xlsx", "Daily Report 25 Aug  2026 (1).xlsx")
    names = [p.name for p in discovery.find_source_workbooks(tmp_path)]
    assert sorted(names) == ["Daily Report 25 Aug  2026 (1).xlsx", "Daily Report 25 Aug 2026 (2).xlsx"]
    assert names == ["Daily Report 25 Aug  2026 (1).xlsx", "Daily Report 25 Aug 2026 (2).xlsx"]


def test_inconsistent_part_numbering_is_kept_in_sequence(tmp_path):
    # some weeks use (2)/(3) instead of (1)/(2)
    _files(tmp_path, "Report (3).xlsx", "Report (2).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == ["Report (2).xlsx", "Report (3).xlsx"]


def test_part_sort_key():
    assert discovery._part_sort_key(Path("A (2).xlsx")) == (2, "A (2).xlsx")
    assert discovery._part_sort_key(Path("A (2) .xlsx")) == (2, "A (2) .xlsx")
    assert discovery._part_sort_key(Path("A (2).XLSX")) == (2, "A (2).XLSX")
    assert discovery._part_sort_key(Path("A.xlsx")) == (10**6, "A.xlsx")
    assert discovery._part_sort_key(Path("A (2) copy.xlsx")) == (10**6, "A (2) copy.xlsx")


# --- 6. lock files / 7. generated Final reports / 8. stray Excel / 11. unexpected names ----


def test_lock_files_are_excluded(tmp_path):
    _files(tmp_path, "~$Report (1).xlsx", "Report (1).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == ["Report (1).xlsx"]


def test_generated_final_reports_are_excluded_case_insensitively(tmp_path):
    _files(tmp_path, "Final Activity Report.xlsx", "Final Official vs AI Location Report.xlsx",
           "FINAL report.xlsx", "final summary.xlsx", "Report (1).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == ["Report (1).xlsx"]


def test_non_xlsx_files_are_ignored(tmp_path):
    _files(tmp_path, "Report (1).xlsm", "Report (2).xls", "Report (3).csv", "notes.txt", "photo.jpg",
           "Report (4).xlsx.bak", "Report (5).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == ["Report (5).xlsx"]


def test_stray_and_unexpected_xlsx_names_current_behaviour(tmp_path):
    # Pinned as reported in P2-3; reviewed in P2-5 and kept deliberately: only the exact "final "
    # prefix (with a space -- every documented generated/hand-built report name uses it) is
    # treated as a generated report, so these are all treated as SOURCE workbooks. No project
    # evidence defines "Final_..." or "Copy of ..." as non-source, so no rule is invented for
    # them. Unnumbered files sort after numbered parts, alphabetically.
    _files(tmp_path, "Report (1).xlsx", "Final_Report.xlsx", "Copy of Report (1).xlsx",
           "Finalized checklist.xlsx", "zz notes.xlsx", "aa notes.xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == [
        "Copy of Report (1).xlsx", "Report (1).xlsx",
        "Final_Report.xlsx", "Finalized checklist.xlsx", "aa notes.xlsx", "zz notes.xlsx"]


def test_final_space_prefix_also_excludes_a_source_like_name(tmp_path):
    # P2-5 characterization: the existing "final " rule is a prefix match, so a hypothetical
    # SOURCE part named "Final ..." would be excluded too (a known false-exclusion risk of the
    # documented rule; no such source file is documented in this project).
    _files(tmp_path, "Final Deck Photos (1).xlsx", "Deck Photos (2).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == ["Deck Photos (2).xlsx"]


def test_windows_explorer_copy_name_sorts_after_the_numbered_parts(tmp_path):
    # P2-5 characterization: Explorer names a copy "<name> - Copy.xlsx". The "(N)" is then no
    # longer directly before ".xlsx", so the copy has no part number and sorts after every part;
    # a "Copy of <name>" copy keeps its "(N)" and sorts BEFORE the original (name tie-break).
    _files(tmp_path, "Report (2).xlsx", "Report (1) - Copy.xlsx", "Report (1).xlsx", "Copy of Report (2).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == [
        "Report (1).xlsx", "Copy of Report (2).xlsx", "Report (2).xlsx", "Report (1) - Copy.xlsx"]


def test_copy_of_workbook_photos_are_deduped_but_attributed_to_the_copy(tmp_path):
    # P2-5 characterization of the end-to-end effect: discovery order feeds extraction's
    # week-wide exact-duplicate removal (README: keep first occurrence, log every drop). With a
    # byte-identical "Copy of" part present, the photos stay correct and every drop is logged,
    # but the kept rows name the COPY as their source workbook.
    from src import extraction

    _files(tmp_path, "Report (1).xlsx", "Copy of Report (1).xlsx")
    order = [p.name for p in discovery.find_source_workbooks(tmp_path)]
    assert order == ["Copy of Report (1).xlsx", "Report (1).xlsx"]
    photos = [b"photo-a", b"photo-b"]
    images = [extraction.ExtractedImage(anchor_row=i + 1, anchor_col=1, official_location="MAIN DECK",
                                        image_bytes=data, image_ext="jpg", source_workbook=name,
                                        worksheet_name="AUG 02 2026")
              for name in order for i, data in enumerate(photos)]
    warnings = []
    kept = extraction._dedupe_exact_images(images, warnings)
    assert [k.image_bytes for k in kept] == photos  # no duplicate photo rows
    assert {k.source_workbook for k in kept} == {"Copy of Report (1).xlsx"}
    assert len(warnings) == 2 and all("Dropped duplicate image" in w for w in warnings)


@pytest.mark.skipif(sys.platform != "win32", reason="glob is case-insensitive only on Windows")
def test_uppercase_extension_is_included_on_windows(tmp_path):
    _files(tmp_path, "Report (1).XLSX")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == ["Report (1).XLSX"]


def test_folder_named_like_a_workbook_is_not_a_workbook(tmp_path):
    (tmp_path / "folder.xlsx").mkdir()
    _files(tmp_path, "Report (1).xlsx")
    assert [p.name for p in discovery.find_source_workbooks(tmp_path)] == ["Report (1).xlsx"]


def test_only_real_files_are_workbooks_and_exclusions_still_apply(tmp_path):
    for d in ("Report (2).xlsx", "Final Activity Report.xlsx", "~$Report (3).xlsx"):
        (tmp_path / d).mkdir()  # folders with workbook-like names
    _files(tmp_path, "Report (1).xlsx", "~$Report (1).xlsx", "Final Activity Report (9).xlsx", "Report (4).xlsm")
    found = discovery.find_source_workbooks(tmp_path)
    assert [p.name for p in found] == ["Report (1).xlsx"] and all(p.is_file() for p in found)


def test_week_folder_containing_only_xlsx_named_folders_has_no_workbooks(tmp_path):
    _dirs(tmp_path, "Week 1")
    (tmp_path / "Week 1" / "archive.xlsx").mkdir()
    [week] = discovery.find_week_folders(tmp_path)
    assert week.source_workbooks == []


def test_discovery_never_modifies_anything(tmp_path):
    _dirs(tmp_path, "Week 1")
    _files(tmp_path / "Week 1", "Report (1).xlsx", "~$Report (1).xlsx", "Final Activity Report.xlsx")
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
    discovery.find_week_folders(tmp_path)
    after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
