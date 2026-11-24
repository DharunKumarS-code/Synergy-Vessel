import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import extraction

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_heading_assignment():
    # extract_workbook() alone does not dedupe -- dedup is a week-level concern applied once
    # in extract_week() after combining all of a week's source files (see next test).
    result = extraction.extract_workbook(FIXTURES / "sample_week_part1.xlsx", min_merge_width=10)
    assert len(result.images) == 4
    locations = [img.official_location for img in result.images]
    assert locations == ["Not Labelled", "MAIN DECK", "HATCH COAMING", "HATCH COAMING"]


def test_extract_week_dedupes_exact_duplicate_images():
    result = extraction.extract_week([FIXTURES / "sample_week_part1.xlsx"])

    # img4 (row 30) is an exact duplicate of img2 (row 12) and must be dropped, keeping the
    # first occurrence; only 3 of the 4 embedded images should remain.
    assert len(result.images) == 3
    locations = [img.official_location for img in result.images]
    assert locations == ["Not Labelled", "MAIN DECK", "HATCH COAMING"]
    assert any("Dropped duplicate image" in w for w in result.warnings)


def test_consistent_date_is_trusted():
    result = extraction.extract_workbook(FIXTURES / "sample_week_part1.xlsx", min_merge_width=10)
    assert result.report_date is not None
    assert result.report_date.isoformat() == "2026-01-01"


def test_mismatched_date_is_not_trusted():
    result = extraction.extract_workbook(
        FIXTURES / "sample_week_part2_mismatched_date.xlsx", min_merge_width=10
    )
    assert result.report_date is None
    assert "disagree" in result.date_note


def test_extract_week_combines_files_and_resolves_shared_date():
    # part1 has a confirmed 2026-01-01 date; part2 (used here only for its image + heading,
    # not its own mismatched date) should not change serial ordering of part1's images.
    result = extraction.extract_week(
        [FIXTURES / "sample_week_part1.xlsx", FIXTURES / "sample_week_part2_mismatched_date.xlsx"]
    )
    # part1 contributes 3 images (after dedup), part2 contributes 1
    assert len(result.images) == 4
    assert result.images[0].official_location == "Not Labelled"
    assert result.images[-1].official_location == "CROSS DECK FWD OF # 1"
    # the week-level date disagrees across files (2026-01-01 vs None) -> not trusted
    assert result.report_date is None


# ---------------------------------------------------------------------------
# Phase R: extraction failures must be clearly reported, never silently treated as an empty
# (and therefore misleadingly "successful") extraction result.
# ---------------------------------------------------------------------------


def test_corrupt_workbook_raises_clear_extraction_error(tmp_path):
    bad_file = tmp_path / "corrupt.xlsx"
    bad_file.write_bytes(b"this is not a real xlsx file at all")

    with pytest.raises(extraction.WorkbookExtractionError) as exc_info:
        extraction.extract_workbook(bad_file, min_merge_width=10)

    err = exc_info.value
    assert err.workbook_path == bad_file
    assert "corrupt.xlsx" in str(err)
    # the original exception type must be visible, not hidden behind a generic message
    assert type(err.original_exception).__name__ in str(err)


def test_missing_workbook_path_raises_clear_extraction_error(tmp_path):
    missing = tmp_path / "does_not_exist.xlsx"

    with pytest.raises(extraction.WorkbookExtractionError) as exc_info:
        extraction.extract_workbook(missing, min_merge_width=10)

    assert exc_info.value.workbook_path == missing
    assert "does_not_exist.xlsx" in str(exc_info.value)


def test_extract_week_propagates_workbook_extraction_error(tmp_path):
    bad_file = tmp_path / "corrupt.xlsx"
    bad_file.write_bytes(b"not a real workbook")

    with pytest.raises(extraction.WorkbookExtractionError):
        extraction.extract_week([bad_file])


def test_successfully_opened_workbook_is_closed(monkeypatch):
    close_calls = []
    real_load = extraction.openpyxl.load_workbook

    def spying_load(*args, **kwargs):
        wb = real_load(*args, **kwargs)
        original_close = wb.close

        def spy_close():
            close_calls.append(True)
            return original_close()

        wb.close = spy_close
        return wb

    monkeypatch.setattr(extraction.openpyxl, "load_workbook", spying_load)
    extraction.extract_workbook(FIXTURES / "sample_week_part1.xlsx", min_merge_width=10)
    assert close_calls == [True]


def test_workbook_is_still_closed_when_parsing_fails_after_a_successful_open(monkeypatch):
    close_calls = []
    real_load = extraction.openpyxl.load_workbook

    def spying_load(*args, **kwargs):
        wb = real_load(*args, **kwargs)
        original_close = wb.close
        wb.close = lambda: (close_calls.append(True), original_close())[1]
        return wb

    monkeypatch.setattr(extraction.openpyxl, "load_workbook", spying_load)
    monkeypatch.setattr(extraction, "_extract_from_workbook",
                         lambda *a, **kw: (_ for _ in ()).throw(ValueError("simulated parse failure")))

    with pytest.raises(extraction.WorkbookExtractionError):
        extraction.extract_workbook(FIXTURES / "sample_week_part1.xlsx", min_merge_width=10)

    assert close_calls == [True]  # closed even though parsing failed, not just on the happy path
