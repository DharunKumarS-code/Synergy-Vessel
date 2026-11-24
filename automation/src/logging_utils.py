"""Run-level logging: a structured log file per run plus a separate flagged-items CSV
listing every "Needs Review" / low-confidence row for human follow-up.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path


def configure_logging(logs_dir: Path) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    )
    return log_path


class FlaggedCsvSchemaError(RuntimeError):
    """Raised when FlaggedItemsWriter would otherwise append to a file that already exists but
    whose header doesn't match the current FIELDS schema (e.g. an older-format CSV from before
    a column was added). Refuses to touch the file rather than risk corrupting it with
    mixed-schema rows -- see FlaggedItemsWriter._check_existing_file()."""


class FlaggedItemsWriter:
    """Appends one row per photo whose AI classification needs human review.

    Crash-safe by construction: add() appends its row to disk immediately (opening the file in
    "a" mode, writing the header only once on the first row) rather than buffering in memory for
    a single bulk write at the end of the run -- so flagged items from weeks already completed
    are never lost if a later week is interrupted. Each pipeline run still gets its own uniquely
    timestamped file (set at construction), so nothing from a previous run is ever appended to or
    duplicated across runs.
    """

    FIELDS = [
        "week", "image_filename", "official_location", "ai_suggested_location",
        "location_confidence", "activity_identified", "reason", "failure_type",
    ]

    def __init__(self, logs_dir: Path):
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = logs_dir / f"flagged_items_{datetime.now():%Y%m%d_%H%M%S}.csv"
        self._any_rows = self._check_existing_file()

    def _check_existing_file(self) -> bool:
        """Each run normally gets a fresh, never-before-seen timestamped path, so this is a
        no-op in practice -- but if self.path already exists (e.g. an unlikely same-second
        collision, or a pre-existing file placed there manually), verify its header matches the
        current schema before anything is ever appended to it. A mismatched header means a
        different, likely older, CSV format already lives here: raise and leave the file
        completely untouched rather than silently corrupt it with mixed-schema rows. Returns
        True if the file already has a valid, matching header (so add() knows not to write a
        duplicate one) -- False if there's no file yet (the normal case).
        """
        if not self.path.exists():
            return False
        with self.path.open("r", newline="", encoding="utf-8") as f:
            first_line = f.readline().rstrip("\r\n")
        if not first_line:
            return False  # empty file -- safe to treat as fresh
        expected_header = ",".join(self.FIELDS)
        if first_line != expected_header:
            raise FlaggedCsvSchemaError(
                f"{self.path}: existing file's header does not match the current schema "
                f"(found {first_line!r}, expected {expected_header!r}) -- refusing to append "
                f"to it. The existing file was left untouched."
            )
        return True

    def add(self, week: str, image_filename: str, official_location: str,
             ai_suggested_location: str, location_confidence: str,
             activity_identified: str, reason: str, failure_type: str = "VISUAL_REVIEW") -> None:
        """failure_type: "API_FAILURE" (Gemini/the provider could not be reached or errored --
        reason then carries the actual, already-redacted error text) or "VISUAL_REVIEW" (a
        successful API response that genuinely needs human review -- reason stays the existing
        generic confidence explanation). Added as a new trailing column so every existing
        column keeps its original position.
        """
        row = {
            "week": week,
            "image_filename": image_filename,
            "official_location": official_location,
            "ai_suggested_location": ai_suggested_location,
            "location_confidence": location_confidence,
            "activity_identified": activity_identified,
            "reason": reason,
            "failure_type": failure_type,
        }
        write_header = not self._any_rows
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        self._any_rows = True

    def write(self) -> Path | None:
        """Rows are already persisted incrementally by add() -- this is kept only so existing
        call sites (run_pipeline.main()) keep working unchanged, now just reporting whether
        anything was ever written instead of performing the write itself."""
        return self.path if self._any_rows else None
