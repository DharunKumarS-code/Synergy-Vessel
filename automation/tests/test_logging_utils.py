"""Tests for the crash-safe FlaggedItemsWriter (Phase M): rows must land on disk the moment
add() is called, not buffered for a single bulk write at the end of the run.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import logging_utils


def _read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_no_file_created_when_nothing_is_flagged(tmp_path):
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    assert writer.write() is None
    assert not writer.path.exists()


def test_row_is_persisted_to_disk_immediately_on_add(tmp_path):
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    writer.add("Week 30", "2026-08-02_001.jpg", "Main Deck", "Main Deck", "High",
               "No activity observed", "test reason")

    # the row must already be on disk -- no separate write() call needed for it to exist.
    rows = _read_rows(writer.path)
    assert len(rows) == 1
    assert rows[0]["week"] == "Week 30"
    assert rows[0]["reason"] == "test reason"


def test_columns_match_existing_schema(tmp_path):
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    writer.add("Week 30", "x.jpg", "Main Deck", "Main Deck", "High", "No activity observed", "r")
    rows = _read_rows(writer.path)
    assert list(rows[0].keys()) == logging_utils.FlaggedItemsWriter.FIELDS


def test_completed_week_rows_survive_simulated_interruption(tmp_path):
    # simulate: week 30 finishes and flags 2 items, then the process is "interrupted" (we just
    # stop calling add() -- no crash handler or final write() is involved) before week 31 runs.
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    writer.add("Week 30", "a.jpg", "Main Deck", "Main Deck", "Low", "No activity observed", "r1")
    writer.add("Week 30", "b.jpg", "Hatch Coaming", "Industrial Vessel Deck", "Needs Review",
               "No activity observed", "r2")

    # "interruption": a brand-new reader opens the same path independently, as a post-mortem
    # inspection would, without the writer object or process surviving.
    rows = _read_rows(writer.path)
    assert len(rows) == 2
    assert {r["image_filename"] for r in rows} == {"a.jpg", "b.jpg"}


def test_multiple_add_calls_do_not_duplicate_or_rewrite_header(tmp_path):
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    for i in range(5):
        writer.add("Week 30", f"{i}.jpg", "Main Deck", "Main Deck", "Low",
                    "No activity observed", "r")
    rows = _read_rows(writer.path)
    assert len(rows) == 5
    # exactly one header line followed by 5 data lines -- confirms the header wasn't re-written
    # on every add() call.
    raw_lines = writer.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 6


def test_write_after_incremental_adds_reports_path_without_duplicating_rows(tmp_path):
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    writer.add("Week 30", "a.jpg", "Main Deck", "Main Deck", "Low", "No activity observed", "r")
    result_path = writer.write()
    assert result_path == writer.path
    rows = _read_rows(writer.path)
    assert len(rows) == 1  # write() must not re-write or duplicate anything add() already wrote


# ---------------------------------------------------------------------------
# Phase AA: failure_type column (API_FAILURE vs VISUAL_REVIEW).
# ---------------------------------------------------------------------------


def test_failure_type_defaults_to_visual_review_when_omitted(tmp_path):
    # backward compatibility: a caller that doesn't know about failure_type yet (the old
    # 7-argument call shape) must still produce a valid row with a sensible default.
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    writer.add("Week 30", "x.jpg", "Main Deck", "Main Deck", "High", "No activity observed", "r")
    rows = _read_rows(writer.path)
    assert rows[0]["failure_type"] == "VISUAL_REVIEW"


def test_failure_type_api_failure_persisted_incrementally_alongside_visual_review(tmp_path):
    writer = logging_utils.FlaggedItemsWriter(tmp_path)
    writer.add("Week 30", "a.jpg", "Main Deck", "Main Deck", "Needs Review",
               "Needs Review", "RuntimeError: 503 UNAVAILABLE", failure_type="API_FAILURE")
    writer.add("Week 30", "b.jpg", "Main Deck", "Main Deck", "Low",
               "No activity observed", "Low/Needs-Review confidence on activity or location",
               failure_type="VISUAL_REVIEW")

    rows = _read_rows(writer.path)
    assert len(rows) == 2
    assert rows[0]["failure_type"] == "API_FAILURE"
    assert rows[0]["reason"] == "RuntimeError: 503 UNAVAILABLE"
    assert rows[1]["failure_type"] == "VISUAL_REVIEW"
    # one header line + 2 data lines -- confirms incremental append still works with the new column.
    raw_lines = writer.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 3


# ---------------------------------------------------------------------------
# Phase AC: CSV header-collision protection.
# ---------------------------------------------------------------------------


class _FixedDatetime:
    """Stands in for logging_utils.datetime so the writer's computed path is deterministic and
    can be made to collide with a pre-existing file in these tests."""

    @classmethod
    def now(cls):
        import datetime as _dt
        return _dt.datetime(2026, 1, 1, 0, 0, 0)


def test_header_collision_raises_and_preserves_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_utils, "datetime", _FixedDatetime)
    target_path = tmp_path / "flagged_items_20260101_000000.csv"
    old_contents = (
        "week,image_filename,official_location,ai_suggested_location,location_confidence,"
        "activity_identified,reason\n"
        "Week 30,x.jpg,Main Deck,Main Deck,High,No activity observed,old reason\n"
    )
    target_path.write_text(old_contents, encoding="utf-8")

    with pytest.raises(logging_utils.FlaggedCsvSchemaError):
        logging_utils.FlaggedItemsWriter(tmp_path)

    # the existing (older-format) file must be completely untouched -- no header rewritten, no
    # row appended, no truncation.
    assert target_path.read_text(encoding="utf-8") == old_contents


def test_matching_header_existing_file_appends_without_duplicating_header(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_utils, "datetime", _FixedDatetime)
    target_path = tmp_path / "flagged_items_20260101_000000.csv"
    existing_header = ",".join(logging_utils.FlaggedItemsWriter.FIELDS)
    existing_row = "Week 30,a.jpg,Main Deck,Main Deck,High,No activity observed,r,VISUAL_REVIEW"
    target_path.write_text(existing_header + "\n" + existing_row + "\n", encoding="utf-8")

    writer = logging_utils.FlaggedItemsWriter(tmp_path)  # same schema -- must not raise
    writer.add("Week 30", "b.jpg", "Main Deck", "Main Deck", "Low",
               "No activity observed", "r2", failure_type="API_FAILURE")

    raw_lines = target_path.read_text(encoding="utf-8").strip().splitlines()
    assert raw_lines[0] == existing_header  # header written exactly once, not duplicated
    assert raw_lines[1] == existing_row     # the pre-existing row survives untouched
    assert "b.jpg" in raw_lines[2]           # the new row was appended after it
