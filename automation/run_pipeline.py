"""CLI entry point for the Synergy Vessel automated image analysis workflow.

Output location: automation/output/<week>/ -- deliberately kept separate from the existing,
hand-built week folders (E:\\Synergy Vessel\\Week NN\\...) so this pipeline can never overwrite
or collide with the manually produced reports and extracted images already in this project.

Usage:
    python run_pipeline.py --dry-run                 # extract + report, no AI calls; writes only under dry_run/
    python run_pipeline.py --weeks "Week 30" --sample 5   # real API, first 5 images of one week only
    python run_pipeline.py --force                   # re-process every image even if unchanged
    python run_pipeline.py                            # full run, all weeks, all images
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src import (classifier, config, discovery, extraction, final_report, logging_utils,
                 register_input, report_builder, state, vision_client)

logger = logging.getLogger("pipeline")

DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 2

# Phase P0-1: dry-run isolation. A dry run writes ONLY under <automation_root>/dry_run/ (its own
# .state/, output/ and logs/), its report filename is prefixed, and every AI-assessed row is
# marked DRY_RUN -- never CLASSIFIED/VISUAL_REVIEW -- so a placeholder can never be mistaken
# for (or cached as) a real classification.
DRY_RUN_DIRNAME = "dry_run"
DRY_RUN_REPORT_PREFIX = "DRY RUN - "
DRY_RUN_STATUS = "DRY_RUN"
DRY_RUN_REASON = "Dry run: no AI request was made -- placeholder only, NOT a classification"

# Phase P1-2: stale/partial report handling. The canonical report name only ever holds a report
# from a run that FINISHED the week. An early stop writes "PARTIAL - <report>" instead; any
# report file an unfinished run has made out of date is renamed "STALE <timestamp> - <name>"
# (os.replace -- renamed, never deleted or overwritten).
MANIFEST_NAME = "manifest.csv"
PARTIAL_PREFIX = "PARTIAL - "
# A --sample run's report/manifest: "SAMPLE - <report>" / "SAMPLE - manifest.csv" (and
# "PARTIAL - SAMPLE - ..." if it stops early). Sample runs only ever write or retire this family.
SAMPLE_PREFIX = "SAMPLE - "
STALE_PREFIX = "STALE "
UNPROCESSED_STATUS = "UNPROCESSED"
NOT_PROCESSED_ACTIVITY = "Not Processed (Run Stopped Early)"


def _dry_run_settings(settings: dict) -> dict:
    """Return a copy of settings with every location the pipeline writes to (state, output,
    logs/flagged CSV) redirected under <automation_root>/dry_run/, and the report filename
    prefixed. The real settings dict is not mutated. Raises ValueError if a redirected location
    would still coincide with a real one (defensive; cannot happen with the default layout)."""
    real = settings["paths"]
    root = Path(real["automation_root"]) / DRY_RUN_DIRNAME
    paths = {**real, "automation_root": str(root), "state_dir": str(root / ".state"),
             "logs_dir": str(root / "logs")}
    pairs = [(paths["state_dir"], real["state_dir"]), (paths["logs_dir"], real["logs_dir"]),
             (str(root / "output"), str(Path(real["automation_root"]) / "output"))]
    for dry, live in pairs:
        if Path(dry).resolve() == Path(live).resolve():
            raise ValueError(f"dry-run location {dry} would overlap the real location {live}")
    report = {**settings["report"],
              "output_filename": DRY_RUN_REPORT_PREFIX + settings["report"]["output_filename"]}
    return {**settings, "paths": paths, "report": report}


class ServiceUnavailableStop(RuntimeError):
    """Phase AE3: raised by process_week() once the run-wide circuit breaker trips, i.e. this
    many consecutive AI-assessed images each still failed with a Gemini 5xx after every retry.
    Caught by run_weeks() in its own except clause (after the quota clause, before the generic
    isolation handler) to stop the whole run, exactly like a quota stop."""

    def __init__(self, message: str, consecutive_failures: int, server_errors: int = 0,
                 timeouts: int = 0):
        self.consecutive_failures = consecutive_failures
        self.server_errors = server_errors
        self.timeouts = timeouts
        super().__init__(message)


class ServiceUnavailableBreaker:
    """Phase AE3: run-wide (cross-week) count of consecutive images whose classification ended
    in vision_client.GeminiServiceUnavailableError -- and, since Phase AE5, also
    vision_client.GeminiRequestTimeoutError, counted toward the same threshold but tracked
    separately (server_errors vs timeouts) so stop messages can tell them apart. Any successful
    API call resets everything to 0. Cached/skipped images, already-labelled images, and other
    failure types (4xx, parse errors, ordinary connection errors) neither count nor reset --
    they are neutral, since they say nothing new about Gemini's availability. Phase P2-8: counts
    the provider-neutral classifier flags, so Groq 5xx-after-retries and Groq timeouts
    (vision_client.GroqServiceUnavailableError / GroqRequestTimeoutError) count the same way."""

    def __init__(self, threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD):
        self.threshold = threshold
        self.consecutive = 0
        self.server_errors = 0
        self.timeouts = 0

    def record(self, result: classifier.ClassificationResult) -> bool:
        """Update from one freshly-requested result; return True if the breaker has tripped."""
        if result.service_unavailable:
            self.consecutive += 1
            self.server_errors += 1
        elif result.request_timeout:
            self.consecutive += 1
            self.timeouts += 1
        elif result.error is None:
            self.consecutive = self.server_errors = self.timeouts = 0
        return self.consecutive >= self.threshold

    def describe(self) -> str:
        return (f"{self.server_errors} server error(s) (5xx after all retries), "
                f"{self.timeouts} request timeout(s)")


def _circuit_breaker_threshold(settings: dict) -> int:
    rate_limit = settings.get("rate_limit")
    if rate_limit is not None and not isinstance(rate_limit, dict):
        # Phase P2 remediation (defect 2): checked before any field access, so a list or string
        # is a clean configuration error rather than an AttributeError. Same wording as
        # vision_client.RetryPolicy.from_settings().
        raise ValueError(f"rate_limit must be a mapping, got {type(rate_limit).__name__}")
    value = (rate_limit or {}).get(
        "circuit_breaker_threshold", DEFAULT_CIRCUIT_BREAKER_THRESHOLD
    )
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"rate_limit.circuit_breaker_threshold must be an integer >= 1, got {value!r}")
    return value


def _generate_image_filename(report_date, serial: int) -> tuple[str, bool]:
    """Returns (filename, is_pending). Mirrors the existing project convention: a confirmed
    date yields "YYYY-MM-DD_NNN.jpg"; an unconfirmed/ambiguous date yields
    "Timestamp_Pending_NNN.jpg" rather than guessing (see extraction.resolve_report_date).
    """
    if report_date is not None:
        return f"{report_date.isoformat()}_{serial:03d}.jpg", False
    return f"Timestamp_Pending_{serial:03d}.jpg", True


def _classification_status(result: classifier.ClassificationResult, needs_review: bool) -> str:
    """Phase AC: the single source of truth for whether an image's outcome is an API/provider
    failure (including confirmed quota exhaustion -- result.error is set either way), a
    successful-but-needs-review visual result, or a successful, no-review-needed one. Used by
    both the Excel report's "Classification Status" column and (via _flag_reason) the flagged
    CSV's failure_type, so the two can never disagree about the same image. An API failure is
    never reported as CLASSIFIED, regardless of needs_review.
    """
    if result.error:
        return "API_FAILURE"
    return "VISUAL_REVIEW" if needs_review else "CLASSIFIED"


def _flag_reason(result: classifier.ClassificationResult) -> tuple[str, str]:
    """Phase AA: the (failure_type, reason) pair for the flagged CSV -- only ever called when
    needs_review is already True, so _classification_status here only ever returns API_FAILURE
    or VISUAL_REVIEW. result.error is already redacted by vision_client.py before it ever
    reaches here, so it's safe to surface as-is.
    """
    status = _classification_status(result, needs_review=True)
    reason = result.error if status == "API_FAILURE" else "Low/Needs-Review confidence on activity or location"
    return status, reason


def _remarks(result: classifier.ClassificationResult | None, classification_status: str) -> str:
    """Register-mode report "Remarks": the existing reason for the row's status, never new text --
    _flag_reason's API error / review reason, or DRY_RUN_REASON; blank otherwise."""
    if result is None:
        return ""
    if classification_status == DRY_RUN_STATUS:
        return DRY_RUN_REASON
    if classification_status in ("API_FAILURE", "VISUAL_REVIEW"):
        return _flag_reason(result)[1] or ""
    return ""


# Phase AE9: image-level outcome keys, reported separately from week-level success.
#   classified    -- AI-assessed image with a successful, confident result (fresh or cached)
#   labelled      -- official location already known from the heading; no AI call needed
#   visual_review -- successful API answer that still needs human review (VISUAL_REVIEW)
#   api_failure   -- the request failed (API_FAILURE): NOT classified, retried next run
#   unprocessed   -- never reached in a week that stopped early (quota / circuit breaker)
#   dry_run       -- P0-1: AI-assessed image in a dry run (no request made; placeholder only)
OUTCOME_KEYS = ("classified", "labelled", "visual_review", "api_failure", "unprocessed", "dry_run")


def _empty_outcomes() -> dict:
    return dict.fromkeys(OUTCOME_KEYS, 0)


def _count_outcome(outcomes: dict, classification_status: str, is_ai_assessed: bool) -> None:
    if classification_status == DRY_RUN_STATUS:
        outcomes["dry_run"] += 1
    elif classification_status == "API_FAILURE":
        outcomes["api_failure"] += 1
    elif classification_status == "VISUAL_REVIEW":
        outcomes["visual_review"] += 1
    elif is_ai_assessed:
        outcomes["classified"] += 1
    else:
        outcomes["labelled"] += 1


def _rate_limit_description(kind: str | None) -> str:
    if kind == "per_minute":
        return "per-minute rate limit, still exceeded after one bounded wait"
    return "rate limit not identifiable as per-minute or daily"


# M2: plain-English cause for each vision_client.ProviderPermanentError kind.
PROVIDER_ERROR_DESCRIPTIONS = {
    "authentication": "authentication failed (the API key is missing, invalid or revoked)",
    "permission": "permission denied (this key or project may not use this model or API)",
    "not_found": "the requested model or resource was not found (check 'model' in settings.yaml)",
    "configuration": "a persistent account or configuration problem reported by the provider",
}


def _provider_error_description(kind: str | None) -> str:
    return PROVIDER_ERROR_DESCRIPTIONS.get(kind, "a provider-wide error")


def _result_provenance(settings: dict) -> dict | None:
    """M3: what produced a classification -- provider, model and the prompt/schema fingerprint.
    Saved with every new state entry; cross-week reuse requires an exact match. None (no reuse,
    nothing recorded) when settings do not name both a provider and a model."""
    provider, model = settings.get("provider"), settings.get("model")
    if not provider or not model:
        return None
    return {"provider": provider, "model": model,
            "prompt_schema": vision_client.classification_fingerprint(provider)}


def _saved_result(entry: dict) -> classifier.ClassificationResult:
    """A ClassificationResult from a saved state entry, ignoring bookkeeping keys ("ok", and
    M3's "provenance" / "reused_from_week")."""
    return classifier.ClassificationResult(**{k: v for k, v in entry.items() if k in state.RESULT_FIELDS})


def _provider_label(settings: dict) -> str:
    """Phase P2-8: the provider name used in rate-limit / circuit-breaker stop messages. "Gemini"
    when settings name no provider (or gemini), so every Gemini message is unchanged."""
    provider = str(settings.get("provider") or "gemini").strip().lower()
    return {"gemini": "Gemini", "groq": "Groq"}.get(provider, provider or "Gemini")


def _needs_review(result: classifier.ClassificationResult) -> bool:
    return (result.location_confidence in ("Low", classifier.NEEDS_REVIEW)
            or result.activity_confidence in ("Low", classifier.NEEDS_REVIEW)
            or result.ai_suggested_location == classifier.NEEDS_REVIEW)


def _report_file_names(report_name: str, is_sample: bool = False,
                       manifest_name: str = MANIFEST_NAME) -> tuple[list[str], list[str]]:
    """([report, manifest] of a finished run, [report, manifest] of an early-stopped run) for one
    week's output folder. P1-2 correction: a --sample run uses its own "SAMPLE - " family, so a
    sample report can never carry (or replace) the normal complete report's name.
    manifest_name: "manifest.csv" by default; --from-registers uses its own (see _output_names)."""
    report, manifest = (SAMPLE_PREFIX + report_name, SAMPLE_PREFIX + manifest_name) if is_sample \
        else (report_name, manifest_name)
    return [report, manifest], [PARTIAL_PREFIX + report, PARTIAL_PREFIX + manifest]


def _retire_report_files(output_dir: Path, names: list[str], week_name: str, why: str) -> list[Path]:
    """Phase P1-2: rename each existing file in names to "STALE <timestamp> - <name>" so it can
    never be mistaken for a current result. os.replace() is an atomic rename on the same
    folder; an existing STALE file is never overwritten (a counter is added instead), and
    nothing is ever deleted. Returns the new paths.

    Phase P2-10: one file that cannot be renamed (Windows PermissionError -- typically the file
    is open in Excel) no longer aborts the rest, and never raises: every other file is still
    retired. The stuck file is left byte-for-byte untouched under its normal name, so it is
    flagged instead -- an OUT OF DATE warning file is written next to it, an ERROR is logged,
    and run_weeks() reports it (outdated_files; a --strict-exit failure). A file that IS renamed
    has any earlier warning file for its name removed, since that name no longer holds it."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    retired = []
    for name in names:
        src = output_dir / name
        if not src.is_file():
            continue
        target = output_dir / f"{STALE_PREFIX}{stamp} - {name}"
        n = 2
        while target.exists():
            target = output_dir / f"{STALE_PREFIX}{stamp} ({n}) - {name}"
            n += 1
        try:
            os.replace(src, target)
        except OSError as exc:
            _flag_outdated_report_file(output_dir, name, week_name, why, exc)
            continue
        retired.append(target)
        _clear_outdated_markers(output_dir, [name], week_name)
    if retired:
        logger.warning("%s: %s -- renamed out-of-date report file(s) so they cannot be mistaken "
                       "for current results: %s", week_name, why, ", ".join(p.name for p in retired))
    return retired


# Phase P2-10: "<file name> - OUT OF DATE - DO NOT USE.txt", written next to a report-family file
# that is out of date but could not be renamed to STALE. Sorts directly after the file it warns
# about; never matches a report-family name, a STALE name or a temp name, so nothing else in the
# pipeline ever picks it up. Removed only by the pipeline itself, once the condition is resolved.
OUTDATED_MARKER_SUFFIX = " - OUT OF DATE - DO NOT USE.txt"
# Files flagged by _flag_outdated_report_file() during the current run_weeks() call (cleared at
# its start), so a flagged file is reported even when its warning file could not be written.
_OUTDATED_THIS_RUN: list[Path] = []


def _outdated_marker_path(output_dir: Path, name: str) -> Path:
    return output_dir / f"{name}{OUTDATED_MARKER_SUFFIX}"


def _flag_outdated_report_file(output_dir: Path, name: str, week_name: str, why: str,
                               exc: BaseException) -> None:
    """Phase P2-10: name could not be renamed to STALE. Leave it untouched, write the warning file
    (atomically: temp file in the same folder + os.replace) and log one unambiguous ERROR. Never
    raises -- a failure here must not replace the original stop or error."""
    marker = _outdated_marker_path(output_dir, name)
    _OUTDATED_THIS_RUN.append(output_dir / name)
    text = (
        f'WARNING: "{name}" in this folder is OUT OF DATE -- do NOT use it.\n\n'
        f"Week: {week_name}\n"
        f"Written: {datetime.now().isoformat(timespec='seconds')}\n"
        f"Why it is out of date: {why}.\n"
        f'It should have been renamed to "{STALE_PREFIX}<timestamp> - {name}", but the rename '
        f"failed ({type(exc).__name__}: {exc}).\n"
        "This almost always means the file was open in Excel (or another program) during the run.\n\n"
        "The file itself was left untouched: its contents are from an EARLIER run. Newer results\n"
        "already exist in the pipeline's saved state.\n\n"
        "What to do: close the file everywhere (Excel, preview panes), then rerun the same command\n"
        "(no --force needed -- photos already done are not sent again). This warning file is\n"
        f'removed automatically once a run replaces or renames "{name}".\n'
    )
    written = False
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(output_dir), prefix=f"{marker.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, marker)
            written = True
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except Exception as marker_exc:  # noqa: BLE001 -- never mask the original problem
        logger.error("%s: the warning file %s could not be written either (%s: %s).", week_name,
                     marker.name, type(marker_exc).__name__, marker_exc)
    logger.error(
        "%s: %s is OUT OF DATE but still has its normal name -- do NOT use it. It could not be "
        "renamed to STALE (%s: %s), most likely because it is open in Excel. Its contents are "
        "unchanged, from an earlier run. %s Close the file and rerun the same command (no --force "
        "needed).", week_name, output_dir / name, type(exc).__name__, exc,
        f'Warning file written next to it: "{marker.name}".' if written
        else "The warning file could not be written either (see above).")


def _clear_outdated_markers(output_dir: Path, names: list[str], week_name: str) -> None:
    """Phase P2-10: remove the pipeline's own warning file for each name that has just been
    replaced by a current file or renamed to STALE. Only ever touches exact marker names; a
    failure is logged, never raised."""
    for name in names:
        if output_dir / name in _OUTDATED_THIS_RUN:
            _OUTDATED_THIS_RUN.remove(output_dir / name)  # resolved within this run
        marker = _outdated_marker_path(output_dir, name)
        if not marker.is_file():
            continue
        try:
            marker.unlink()
            logger.info("%s: %s is no longer out of date -- removed its warning file %s.",
                        week_name, name, marker.name)
        except OSError as exc:
            logger.error("%s: could not remove the warning file %s (%s: %s); %s itself is now "
                         "current -- the warning file can be deleted by hand.", week_name,
                         marker.name, type(exc).__name__, exc, name)


def _outdated_report_files(output_dir: Path) -> list[Path]:
    """Phase P2-10: report-family files in output_dir that a warning file still flags as out of
    date (the marker exists AND the file it names still exists under that name)."""
    if not output_dir.is_dir():
        return []
    found = []
    for marker in sorted(output_dir.glob(f"*{OUTDATED_MARKER_SUFFIX}")):
        target = output_dir / marker.name[: -len(OUTDATED_MARKER_SUFFIX)]
        if marker.is_file() and target.is_file():
            found.append(target)
    return found


def _write_manifest(path: Path, manifest_rows: list[dict]) -> None:
    """Write the manifest CSV atomically (Phase P2-6). Content, column order, dialect and
    encoding are exactly as before (UTF-8, no BOM, newline=""; an empty file when there are no
    rows). The complete file is written to a collision-safe temp file in the SAME directory
    (os.replace() is only atomic within one filesystem), flushed, fsynced and closed, and only
    then os.replace()d over path -- which on Windows also replaces an existing destination. Any
    failure, including Ctrl+C mid-write, removes the temp file and re-raises, leaving an existing
    manifest byte-for-byte untouched. Same pattern as report_builder.build_report() and
    state.save_state(). The temp name ("<name>.<random>.tmp") never matches a report-family name,
    so P1-2's STALE renaming can never pick it up."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()) if manifest_rows else [])
            if manifest_rows:
                writer.writeheader()
                writer.writerows(manifest_rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)  # only ever our own temp file
        except OSError:
            pass  # never let clean-up mask the original error
        raise


def _processing_status_rows(week_name: str, report_rows: list, n_extracted: int, sample: int | None,
                            stop_reason: str | None = None,
                            input_note: str | None = None) -> list[tuple[str, str]]:
    """Phase P1-2: contents of the report's "Processing Status" sheet."""
    counts = Counter(r.classification_status for r in report_rows)
    # M1: a finished week whose report still contains failed requests is never called plainly
    # "COMPLETE" -- those photos have no classification. No wording claims that this run
    # processed every photo (rows taken from saved state are shown by their saved status).
    n_failed = counts.get("API_FAILURE", 0)
    failures = f"{n_failed} API FAILURE(S) \u2013 not all photos classified" if n_failed else ""
    if stop_reason is not None:
        status = "PARTIAL - processing stopped early; this is NOT a complete report"
    elif sample is not None and len(report_rows) < n_extracted:
        status = (f"COMPLETE FOR A SAMPLE ONLY - first {len(report_rows)} of {n_extracted} photos "
                  "(--sample); not a full-week report" + (f"; WITH {failures}" if failures else ""))
    elif sample is not None:
        status = (f"COMPLETE FOR A SAMPLE RUN - --sample {sample} covered all {n_extracted} photos; "
                  "rerun without --sample for the normal weekly report"
                  + (f"; WITH {failures}" if failures else ""))
    elif failures:
        status = f"COMPLETE WITH {failures}"
    else:
        status = "COMPLETE"
    rows = [("Report status", status), ("Week", week_name),
            ("Generated", datetime.now().isoformat(timespec="seconds")),
            ("Photos in this report", f"{len(report_rows)} of {n_extracted} extracted")]
    if input_note is not None:  # --from-registers only; legacy rows unchanged
        rows.append(("Input", input_note))
    if stop_reason is not None:
        rows.append(("Stop reason", stop_reason))
    rows.append(("Rows by Classification Status",
                 "; ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "none"))
    if stop_reason is not None:
        rows.append(("What to do", f"Rerun the same command later (no --force needed). "
                                   f"{UNPROCESSED_STATUS} rows were not reached this run and "
                                   "API_FAILURE rows will be retried automatically."))
    return rows


def _attach_week_outcomes(exc: Exception, outcomes: dict) -> Exception:
    """Record the stopping image (always an API_FAILURE -- it raises before the shared tally)
    and attach the tally to the stop exception. Phase P1-3: the images after the stopping image
    are tallied individually by stop_with_partial_report (into this same dict), no longer all
    counted as "unprocessed" -- labelled photos and photos already done in state never were."""
    outcomes["api_failure"] += 1
    exc.week_outcomes = outcomes
    return exc


@dataclass
class _WorkItem:
    """One report row to produce. Legacy mode: one photo from extraction.extract_week() (same-week
    duplicates already dropped). --from-registers: one register appearance (every row kept, the
    register's Image ID as identifier). Both modes run through the same loop in _process_week."""
    serial: int
    filename: str
    image_bytes: bytes
    source_workbook: str
    worksheet_name: str
    official_location: str      # as shown in the report / manifest
    ai_assessed: bool
    report_date: str
    content_hash: str           # SHA-256 of image_bytes (the state key)
    image_id: str | None = None  # register mode only (WNN-NNNN)


# --from-registers: separate per-week output names, so no existing report family (the normal
# "Final Official vs AI Location Report.xlsx" / manifest.csv, or their PARTIAL/SAMPLE/STALE
# variants) is ever written, renamed or retired by a register-based run -- and vice versa.
REGISTER_REPORT_TEMPLATE = "AI Classification - Week_{:02d}.xlsx"
REGISTER_MANIFEST_TEMPLATE = "AI Classification - Week_{:02d} - manifest.csv"
# Result Source values (register mode report column / manifest field).
SOURCE_OFFICIAL = "Official Location (no AI request)"
SOURCE_REQUEST = "AI request (this run)"
SOURCE_DRY_RUN = "Dry run placeholder (no AI request)"
SOURCE_SAVED = "Saved result (earlier run)"
SOURCE_REUSED = "Reused from {} (identical photo, no AI request)"
SOURCE_DUPLICATE = "Same photo as {} (no AI request)"
SOURCE_NOT_REACHED = "Not reached (run stopped early)"


def _output_names(week: discovery.WeekFolder, settings: dict, client: vision_client.VisionClient,
                  from_registers: bool) -> tuple[str, str]:
    """(report name, manifest name) for the week's output folder."""
    if not from_registers:
        return settings["report"]["output_filename"], MANIFEST_NAME
    number = register_input.week_number(week.name)
    prefix = DRY_RUN_REPORT_PREFIX if isinstance(client, vision_client.NullVisionClient) else ""
    return (prefix + REGISTER_REPORT_TEMPLATE.format(number),
            prefix + REGISTER_MANIFEST_TEMPLATE.format(number))


def process_week(week: discovery.WeekFolder, settings: dict, client: vision_client.VisionClient,
                  force: bool, sample: int | None,
                  flagged: logging_utils.FlaggedItemsWriter,
                  breaker: ServiceUnavailableBreaker | None = None,
                  reuse_results: state.CrossWeekResults | None = None,
                  from_registers: bool = False) -> dict:
    """Process one week. Phase P1-2 wrapper: if processing does not finish -- Ctrl+C, an
    extraction/report error, anything -- the week's existing report files (complete or partial)
    are renamed STALE, because state may already be newer than they are. An early stop
    (quota / rate limit / circuit breaker) is handled inside _process_week, which writes a
    PARTIAL report and marks the week handled before raising. A --sample run only ever touches
    its own SAMPLE-prefixed files (see _report_file_names), never the normal complete report.
    from_registers: input is the week's completed register (see src/register_input.py) and the
    output is the separate "AI Classification - Week_NN" family (see _output_names)."""
    output_dir = Path(settings["paths"]["automation_root"]) / "output" / week.name
    report_name, manifest_name = _output_names(week, settings, client, from_registers)
    handled = {"done": False}
    try:
        return _process_week(week, settings, client, force, sample, flagged, breaker,
                             output_dir, report_name, handled, reuse_results,
                             from_registers=from_registers, manifest_name=manifest_name)
    finally:
        if not handled["done"]:
            try:
                complete_files, partial_files = _report_file_names(report_name, sample is not None,
                                                                   manifest_name)
                _retire_report_files(output_dir, complete_files + partial_files, week.name,
                                     "processing of this week did not finish")
            except Exception as exc:  # noqa: BLE001 -- never mask the original exception
                logger.error("%s: could not mark the previous report as stale (%s: %s) -- do NOT "
                             "treat %s as current.", week.name, type(exc).__name__, exc, report_name)


def _week_items(week: discovery.WeekFolder, settings: dict, from_registers: bool) -> list[_WorkItem]:
    """Every row of the week, in order (before --sample is applied)."""
    if from_registers:
        rows = register_input.load_register_week(week)  # validated + re-hashed; read-only
        logger.info("%s: %d register row(s) loaded and verified (SHA-256) from %s; %d need AI "
                    "assessment (blank Official Location).", week.name, len(rows),
                    register_input.register_path(week).name, sum(r.needs_ai for r in rows))
        return [_WorkItem(serial=r.serial_number, filename=r.image_filename, image_bytes=r.image_bytes,
                          source_workbook=r.source_workbook, worksheet_name=r.worksheet_name,
                          official_location=r.official_location or "", ai_assessed=r.needs_ai,
                          report_date=r.report_date, content_hash=r.sha256, image_id=r.image_id)
                for r in rows]
    extraction_result = extraction.extract_week(
        week.source_workbooks, settings["processing"]["heading_min_merge_width"]
    )
    for w in extraction_result.warnings:
        logger.warning("%s: %s", week.name, w)
    report_date = extraction_result.report_date.isoformat() if extraction_result.report_date else "PENDING"
    items = []
    for serial, img in enumerate(extraction_result.images, start=1):
        filename, _ = _generate_image_filename(extraction_result.report_date, serial)
        items.append(_WorkItem(serial=serial, filename=filename, image_bytes=img.image_bytes,
                               source_workbook=img.source_workbook, worksheet_name=img.worksheet_name,
                               official_location=img.official_location,
                               ai_assessed=img.official_location == "Not Labelled",
                               report_date=report_date, content_hash=state.image_hash(img.image_bytes)))
    return items


def _process_week(week: discovery.WeekFolder, settings: dict, client: vision_client.VisionClient,
                  force: bool, sample: int | None, flagged: logging_utils.FlaggedItemsWriter,
                  breaker: ServiceUnavailableBreaker | None, output_dir: Path, report_name: str,
                  handled: dict, reuse_results: state.CrossWeekResults | None = None,
                  from_registers: bool = False, manifest_name: str = MANIFEST_NAME) -> dict:
    # breaker: run_weeks() passes one shared instance so the count spans weeks; a direct call
    # without one still gets a week-local breaker with the configured threshold.
    if breaker is None:
        breaker = ServiceUnavailableBreaker(_circuit_breaker_threshold(settings))
    provider_label = _provider_label(settings)  # P2-8: stop messages only
    logger.info("Processing %s (%d source workbook(s))", week.name, len(week.source_workbooks))

    all_items = _week_items(week, settings, from_registers)
    n_extracted = len(all_items)
    items = all_items[:sample] if sample is not None else all_items

    # Legacy mode writes each photo to output/<week>/Extracted Images/. Register mode never
    # writes or copies photos: the register's verified "Extracted Images - New" files are used.
    extracted_images_dir = None
    if not from_registers:
        extracted_images_dir = output_dir / settings["report"]["extracted_images_dirname"]
        extracted_images_dir.mkdir(parents=True, exist_ok=True)

    state_dir = Path(settings["paths"]["state_dir"])
    week_state = state.load_state(state_dir, week.name)
    # P0-1: second line of defence behind main()'s path redirection -- a NullVisionClient
    # result is never stored as ok:true (so it is never trusted as done by any later run, even
    # if a dry run were somehow pointed at real state) and never reported as a classification.
    is_dry_run = isinstance(client, vision_client.NullVisionClient)
    provenance = None if is_dry_run else _result_provenance(settings)  # M3: saved with live results
    manifest_rows = []
    report_rows = []
    n_classified = 0
    n_failed = 0
    n_skipped = 0
    n_reused = 0  # M3
    n_duplicates = 0  # register mode: same-week repeat appearances (no request of their own)
    # Phase AE9: per-image OUTCOME tally (by final Classification Status), separate from the
    # request-oriented counters above -- n_classified counts every API request made, failed or
    # not, and is kept unchanged. On an early stop this is attached to the raised exception
    # (see _attach_week_outcomes) so run_weeks() can still report what happened in this week.
    outcomes = _empty_outcomes()
    # Result of every AI-assessed hash already handled in THIS week of this run -> (result, the
    # Image ID / filename it came from). A repeat appearance of the same photo copies it and
    # never sends its own request (also under --force). Legacy mode has no repeats (dropped by
    # extract_week), so this never changes legacy behaviour.
    seen_this_run: dict[str, tuple[classifier.ClassificationResult, str]] = {}

    def write_photo(item):
        if extracted_images_dir is not None:
            (extracted_images_dir / item.filename).write_bytes(item.image_bytes)

    def add_row(item, ai_location, confidence, activity, classification_status, result_source,
                result=None):
        row = {
            "serial_number": item.serial, "image_filename": item.filename,
            "report_date": item.report_date,
            "original_excel_filename": item.source_workbook, "worksheet_name": item.worksheet_name,
            "official_location": item.official_location, "ai_suggested_location": ai_location or "",
            "activity_identified": activity, "location_confidence": confidence or "",
        }
        if from_registers:  # legacy manifest columns are unchanged
            row = {"image_id": item.image_id, **row, "classification_status": classification_status,
                   "result_source": result_source, "sha256": item.content_hash}
        manifest_rows.append(row)
        report_rows.append(report_builder.ReportRow(
            serial_number=item.serial, image_filename=item.filename, report_date=item.report_date,
            original_excel_filename=item.source_workbook, worksheet_name=item.worksheet_name,
            official_location=item.official_location, ai_suggested_location=ai_location,
            image_bytes=item.image_bytes, activity_identified=activity, location_confidence=confidence,
            classification_status=classification_status, result_source=result_source,
            activity_confidence=result.activity_confidence if result is not None else None,
            remarks=_remarks(result, classification_status),
        ))

    def saved_source(entry):
        reused_from = entry.get("reused_from_week") if isinstance(entry, dict) else None
        return SOURCE_REUSED.format(reused_from) if reused_from else SOURCE_SAVED

    def build(path, stop_reason=None):
        report_builder.build_report(
            week.name, report_rows, path,
            settings["report"]["image_cell_max_width_px"], settings["report"]["image_cell_max_height_px"],
            processing_status=_processing_status_rows(
                week.name, report_rows, n_extracted, sample, stop_reason,
                input_note=(f"Weekly register {register_input.register_path(week).name} "
                            f"({n_extracted} rows, every photo SHA-256 verified; not modified)"
                            if from_registers else None)),
            result_source_column=from_registers, review_columns=from_registers,
        )

    def stop_with_partial_report(stop_exc, stop_reason, index, item, result, result_source):
        """Phase P1-2: early stop (quota / rate limit / breaker). No further requests are made.
        Builds every remaining row offline -- labelled photos as usual, AI photos from a
        successful saved result if there is one (or, register mode, the result of the same photo
        earlier in this week), otherwise UNPROCESSED -- retires the old report files to STALE,
        writes "PARTIAL - <report>", and returns stop_exc for the caller to raise. Phase P1-3:
        each later image is tallied by what it really is -- a labelled photo as "labelled", a
        photo already done in saved state by its saved status, and only an AI-assessable photo
        that still needs a request as "unprocessed". The stop's image, PARTIAL report name and
        retired STALE files are attached to stop_exc for main()."""
        add_row(item, result.ai_suggested_location, result.location_confidence,
                result.activity_identified, _classification_status(result, needs_review=True),
                result_source, result)
        for later in items[index + 1:]:
            write_photo(later)
            if not later.ai_assessed:
                add_row(later, None, None, classifier.NOT_ASSESSED_OFFICIAL_LOCATION, "CLASSIFIED",
                        SOURCE_OFFICIAL)
                outcomes["labelled"] += 1
                continue
            memo = seen_this_run.get(later.content_hash)
            if memo is not None:
                earlier, origin = memo
                earlier_status = _classification_status(earlier, _needs_review(earlier))
                add_row(later, earlier.ai_suggested_location, earlier.location_confidence,
                        earlier.activity_identified, earlier_status, SOURCE_DUPLICATE.format(origin),
                        earlier)
                _count_outcome(outcomes, earlier_status, is_ai_assessed=True)
            elif not is_dry_run and not state.needs_processing(week_state, later.content_hash, force=False):
                saved = _saved_result(week_state[later.content_hash])
                saved_status = _classification_status(saved, _needs_review(saved))
                add_row(later, saved.ai_suggested_location, saved.location_confidence,
                        saved.activity_identified, saved_status,
                        saved_source(week_state[later.content_hash]), saved)
                _count_outcome(outcomes, saved_status, is_ai_assessed=True)
                seen_this_run[later.content_hash] = (saved, later.image_id or later.filename)
            else:
                add_row(later, None, None, NOT_PROCESSED_ACTIVITY, UNPROCESSED_STATUS,
                        SOURCE_NOT_REACHED)
                outcomes["unprocessed"] += 1

        stop_exc.stop_image = item.filename
        stop_exc.partial_report = None
        complete_files, partial_files = _report_file_names(report_name, sample is not None, manifest_name)
        stop_exc.stale_files = [p.name for p in _retire_report_files(
            output_dir, complete_files + partial_files, week.name, "run stopped early")]
        partial_path = output_dir / partial_files[0]
        try:
            build(partial_path, stop_reason)
            _write_manifest(output_dir / partial_files[1], manifest_rows)
            stop_exc.partial_report = partial_path.name
            n_unprocessed = sum(r.classification_status == UNPROCESSED_STATUS for r in report_rows)
            logger.warning("%s: run stopped early (%s) -- PARTIAL report written to %s (%d of %d "
                           "row(s) UNPROCESSED). It is NOT a complete report.", week.name,
                           stop_reason, partial_path, n_unprocessed, len(report_rows))
        except Exception as exc:  # noqa: BLE001 -- must never replace the stop with another error
            logger.error("%s: could not write the partial report (%s: %s). No current report "
                         "exists for this week; rerun to regenerate it.", week.name,
                         type(exc).__name__, exc)
        handled["done"] = True  # old files already retired; nothing left for the wrapper to do
        return stop_exc

    for index, item in enumerate(items):
        filename = item.filename
        write_photo(item)
        content_hash = item.content_hash
        is_ai_assessed = item.ai_assessed

        if is_ai_assessed:
            memo = seen_this_run.get(content_hash)
            reused = None
            if memo is None and (reuse_results is not None and not force and not is_dry_run
                                 and state.needs_processing(week_state, content_hash, force)):
                # M3: an identical photo already classified successfully in ANOTHER week, by the
                # same provider, model and prompt/schema, is reused instead of sending a request.
                # Never with --force (it means "send again") and never in a dry run.
                reused = reuse_results.lookup(content_hash, week.name)
                if reused is None and reuse_results.is_conflicting(content_hash, week.name):
                    logger.warning("%s %s: identical photos in other weeks have DIFFERENT saved "
                                   "results (sha256 %s) -- not reused; this photo is sent.",
                                   week.name, filename, content_hash)
            if memo is not None:
                # Same photo already handled earlier in this week: copy its result (success or
                # failure) -- no request, no state write (the state entry is per photo).
                result, origin = memo
                result_source = SOURCE_DUPLICATE.format(origin)
                n_duplicates += 1
            elif reused is not None:
                source_key, source_entry = reused
                week_state[content_hash] = {
                    "ok": True,
                    **{f: source_entry[f] for f in state.RESULT_FIELDS},
                    "provenance": source_entry["provenance"],
                    "reused_from_week": source_key.replace("_", " "),
                }
                state.save_state(state_dir, week.name, week_state)  # this week stays self-contained
                logger.info("%s %s: reused the saved classification of an identical photo from %s "
                            "(sha256 %s) -- no request sent.", week.name, filename,
                            source_key.replace("_", " "), content_hash)
                result = _saved_result(week_state[content_hash])
                result_source = SOURCE_REUSED.format(source_key.replace("_", " "))
                n_reused += 1
            elif state.needs_processing(week_state, content_hash, force):
                result = classifier.classify_image(client, item.image_bytes)
                result_source = SOURCE_DRY_RUN if is_dry_run else SOURCE_REQUEST
                # Registered before any stop below, so a later repeat of this photo in the week
                # (register mode) copies this outcome instead of being sent.
                seen_this_run[content_hash] = (result, item.image_id or filename)
                # "ok" distinguishes a failed API request (never trusted as done -- see
                # state.needs_processing) from a legitimate Needs Review classification (a
                # completed request whose answer happens to need human review). Only
                # classifier.classify_image()'s except-Exception path ever sets result.error --
                # an ordinary visual Needs Review never does, so it is correctly "ok": True here.
                week_state[content_hash] = {
                    "ok": result.error is None and not is_dry_run,
                    "activity_identified": result.activity_identified,
                    "activity_confidence": result.activity_confidence,
                    "ai_suggested_location": result.ai_suggested_location,
                    "location_confidence": result.location_confidence,
                    "person_visible": result.person_visible,
                }
                if provenance is not None and not is_dry_run:
                    week_state[content_hash]["provenance"] = provenance  # M3
                # Checkpoint immediately -- this classification (success or failure) must never
                # exist only in memory. If the process is interrupted before the next image even
                # starts, this result is already durable and won't be lost or redone on restart.
                state.save_state(state_dir, week.name, week_state)
                if reuse_results is not None:
                    reuse_results.add(week.name, content_hash, week_state[content_hash])  # M3
                if result.error:
                    logger.error("%s %s: classification error: %s", week.name, filename, result.error)
                    n_failed += 1
                n_classified += 1
                if result.quota_exhausted:
                    # Confirmed Gemini daily quota exhaustion -- already checkpointed as
                    # "ok": false above, so it's retryable next run with no --force needed.
                    # Stop immediately: no more requests for the rest of this week, and (via
                    # this same exception propagating through run_weeks()) no subsequent week
                    # either. Never sleep/wait for retry_delay_seconds here -- it's for the log
                    # message only.
                    delay = result.retry_delay_seconds
                    logger.error(
                        "%s %s: Gemini daily quota exhausted -- stopping further processing "
                        "this run (Google-suggested retry delay: %s).",
                        week.name, filename, f"{delay:.0f}s" if delay is not None else "not provided",
                    )
                    # Flag this image before stopping -- otherwise the quota-exhausted image
                    # would never appear in the CSV at all, since the raise below skips the
                    # shared flagging block further down.
                    failure_type, reason = _flag_reason(result)
                    flagged.add(week.name, filename, item.official_location, result.ai_suggested_location,
                                result.location_confidence, result.activity_identified, reason, failure_type)
                    raise stop_with_partial_report(_attach_week_outcomes(vision_client.GeminiQuotaExhaustedError(
                        f"quota exhausted while processing {filename}", delay,
                        quota_limit=result.quota_limit, quota_id=result.quota_id,
                    ), outcomes), "Gemini daily quota exhausted",
                        index, item, result, result_source)
                if result.rate_limited:
                    # Phase P1-1: a non-daily 429 the client could not resolve with its one
                    # bounded wait (per_minute) or could not identify at all (ambiguous). Same
                    # conservative stop as the quota stop: already checkpointed ok:false
                    # (retryable, no --force), flagged once here, no further requests this run.
                    delay = result.retry_delay_seconds
                    logger.error(
                        "%s %s: %s HTTP 429 (%s) -- stopping further processing this run "
                        "(provider-suggested retry delay: %s).",
                        week.name, filename, provider_label,
                        _rate_limit_description(result.rate_limit_kind),
                        f"{delay:.0f}s" if delay is not None else "not provided",
                    )
                    failure_type, reason = _flag_reason(result)
                    flagged.add(week.name, filename, item.official_location, result.ai_suggested_location,
                                result.location_confidence, result.activity_identified, reason, failure_type)
                    # Phase P2-8: Groq's own type for a Groq 429; Gemini's exactly as before.
                    stop_type = (vision_client.GroqRateLimitedError if provider_label == "Groq"
                                 else vision_client.GeminiRateLimitedError)
                    raise stop_with_partial_report(_attach_week_outcomes(stop_type(
                        f"rate limited while processing {filename}", delay, result.rate_limit_kind
                    ), outcomes),
                        f"{provider_label} HTTP 429: {_rate_limit_description(result.rate_limit_kind)}",
                        index, item, result, result_source)
                if result.provider_error_kind:
                    # M2: a confirmed provider-wide 4xx (bad key, no permission, unknown model):
                    # every remaining photo would fail the same way, so stop now instead of
                    # sending one doomed request per photo. Already checkpointed ok:false
                    # (retried next run, no --force), flagged once here, PARTIAL report written.
                    description = _provider_error_description(result.provider_error_kind)
                    logger.error(
                        "%s %s: %s %s (HTTP %s) -- this affects every photo, stopping further "
                        "processing this run.", week.name, filename, provider_label, description,
                        result.provider_status_code if result.provider_status_code is not None else "?",
                    )
                    failure_type, reason = _flag_reason(result)
                    flagged.add(week.name, filename, item.official_location, result.ai_suggested_location,
                                result.location_confidence, result.activity_identified, reason, failure_type)
                    stop_type = (vision_client.GroqPermanentError if provider_label == "Groq"
                                 else vision_client.GeminiPermanentError)
                    raise stop_with_partial_report(_attach_week_outcomes(stop_type(
                        f"{description} while processing {filename}", result.provider_error_kind,
                        result.provider_status_code), outcomes),
                        f"{provider_label} {description} (HTTP {result.provider_status_code})",
                        index, item, result, result_source)
                if breaker.record(result):
                    # Phase AE3 circuit breaker: Gemini has been unavailable (5xx after every
                    # retry) for this many consecutive images -- stop making requests for the
                    # rest of this run, mirroring the quota stop above: this image is already
                    # checkpointed as "ok": false (retryable next run, no --force), flagged once
                    # here, and (P1-2) only a PARTIAL report is written for this week.
                    logger.error(
                        "%s %s: %s unavailable for %d consecutive image(s) (%s) -- circuit "
                        "breaker tripped, stopping further processing this run.",
                        week.name, filename, provider_label, breaker.consecutive, breaker.describe(),
                    )
                    failure_type, reason = _flag_reason(result)
                    flagged.add(week.name, filename, item.official_location, result.ai_suggested_location,
                                result.location_confidence, result.activity_identified, reason, failure_type)
                    raise stop_with_partial_report(_attach_week_outcomes(ServiceUnavailableStop(
                        f"{provider_label} unavailable for {breaker.consecutive} consecutive image(s) "
                        f"({breaker.describe()}), stopped at {filename}",
                        breaker.consecutive, breaker.server_errors, breaker.timeouts,
                    ), outcomes),
                        f"{provider_label} unavailable for {breaker.consecutive} consecutive image(s) "
                        f"({breaker.describe()}) -- circuit breaker", index, item, result, result_source)
            else:
                result = _saved_result(week_state[content_hash])
                result_source = saved_source(week_state[content_hash])
                n_skipped += 1
            if memo is None:
                seen_this_run.setdefault(content_hash, (result, item.image_id or filename))

            ai_location = result.ai_suggested_location
            confidence = result.location_confidence
            activity = result.activity_identified
            needs_review = (
                confidence in ("Low", classifier.NEEDS_REVIEW)
                or result.activity_confidence in ("Low", classifier.NEEDS_REVIEW)
                or ai_location == classifier.NEEDS_REVIEW
            )
            classification_status = _classification_status(result, needs_review)
            if is_dry_run:
                classification_status = DRY_RUN_STATUS
                flagged.add(week.name, filename, item.official_location, ai_location,
                            confidence, activity, DRY_RUN_REASON, DRY_RUN_STATUS)
            elif needs_review:
                failure_type, reason = _flag_reason(result)
                flagged.add(week.name, filename, item.official_location, ai_location,
                            confidence, activity, reason, failure_type)
        else:
            # official location already known from the source heading -- no AI call needed, no
            # failure possible, no review needed: already-confirmed info.
            ai_location = None
            confidence = None
            activity = classifier.NOT_ASSESSED_OFFICIAL_LOCATION  # P0-2: never AI-assessed
            classification_status = "CLASSIFIED"
            result_source = SOURCE_OFFICIAL
            result = None

        _count_outcome(outcomes, classification_status, is_ai_assessed)
        add_row(item, ai_location, confidence, activity, classification_status, result_source,
                result)

    state.save_state(state_dir, week.name, week_state)  # final save is now redundant with the
    # per-image checkpoints above, but kept as a harmless, idempotent belt-and-suspenders confirmation.

    is_sample = sample is not None
    complete_files, partial_files = _report_file_names(report_name, is_sample, manifest_name)
    report_path = output_dir / complete_files[0]
    build(report_path)
    _write_manifest(output_dir / complete_files[1], manifest_rows)
    handled["done"] = True
    # P2-10: both files were just replaced by current ones -- any earlier warning for them is void.
    _clear_outdated_markers(output_dir, complete_files, week.name)
    # P1-2: a PARTIAL report left by an earlier stopped run of the same kind is now out of date --
    # retire it. A finished full (non-sample) run also supersedes any earlier SAMPLE reports.
    superseded = list(partial_files)
    if not is_sample:
        superseded += sum(_report_file_names(report_name, True, manifest_name), [])
    try:
        _retire_report_files(output_dir, superseded, week.name,
                             "week completed; earlier partial/sample report superseded")
    except OSError as exc:
        logger.error("%s: could not rename an earlier PARTIAL/SAMPLE report (%s) -- it is out of "
                     "date; use %s.", week.name, exc, complete_files[0])

    logger.info("%s done: %d photos, %d classified (%d failed), %d skipped (unchanged), report -> %s",
                week.name, len(items), n_classified, n_failed, n_skipped, report_path)

    if n_reused:
        logger.info("%s: %d photo(s) reused the result of an identical photo in another week "
                    "(no requests sent).", week.name, n_reused)
    summary = {"week": week.name, "total": len(items), "classified": n_classified,
               "failed": n_failed, "skipped": n_skipped, "reused": n_reused, "outcomes": outcomes}
    if from_registers:
        if n_duplicates:
            logger.info("%s: %d repeat appearance(s) of a photo earlier in this week copied its "
                        "result (no requests sent).", week.name, n_duplicates)
        summary["duplicates"] = n_duplicates
    return summary


def _cross_week_results(settings: dict, force: bool, dry_run: bool) -> state.CrossWeekResults | None:
    """M3: the run's cross-week result store, or None when reuse is off (--force, dry run, or no
    provider/model in settings). Unreadable state files are skipped (and logged), never reused."""
    provenance = _result_provenance(settings)
    if force or dry_run or provenance is None:
        return None
    results = state.CrossWeekResults.from_state_dir(Path(settings["paths"]["state_dir"]), provenance)
    if results.unreadable:
        logger.warning("Cross-week reuse ignores unreadable state file(s): %s", ", ".join(results.unreadable))
    return results


def run_weeks(weeks: list[discovery.WeekFolder], settings: dict, client: vision_client.VisionClient,
              force: bool, sample: int | None, flagged: logging_utils.FlaggedItemsWriter,
              from_registers: bool = False) -> dict:
    """Process each selected week in isolation. A week whose process_week() raises is logged and
    skipped -- it must never abort the remaining weeks, since one bad workbook (a confirmed real
    risk: extract_workbook() raises WorkbookExtractionError for it -- see Phase P) shouldn't block
    every week after it in the same run. Returns {"succeeded": [summary, ...], "failed": [...],
    "stopped_early": {"week", "retry_delay_seconds"} | None}.

    The one deliberate exception to "isolate and continue": a confirmed Gemini daily quota
    exhaustion (vision_client.GeminiQuotaExhaustedError) is caught separately, BEFORE the
    generic isolation handler, and stops this whole run -- every subsequent week is skipped,
    never attempted -- since quota is a global, run-wide resource, not a per-week one. The week
    that hit it is still recorded as failed, never as successful.
    """
    succeeded = []
    failed = []
    stopped_early = None
    stopped_unavailable = None
    stopped_rate_limited = None
    stopped_provider_error = None  # M2
    quota_stop_details = None
    # Phase AE3: one breaker for the whole run, so consecutive Gemini-unavailable images are
    # counted across week boundaries (availability is a global resource, like quota).
    breaker = ServiceUnavailableBreaker(_circuit_breaker_threshold(settings))
    # M3: reusable results from every week's state (same state_dir: a dry run's own dry_run/.state
    # never mixes with production). Off for --force, a dry run, or settings without provenance.
    reuse_results = _cross_week_results(settings, force=force,
                                        dry_run=isinstance(client, vision_client.NullVisionClient))
    reuse_kwargs = {"reuse_results": reuse_results} if reuse_results is not None else {}
    if from_registers:  # passed only when set, so process_week stubs keep their signature
        reuse_kwargs["from_registers"] = True
    # Phase AE9: run-wide image outcome totals + weeks skipped by an early stop (their image
    # counts are unknown -- they were never extracted).
    images = _empty_outcomes()
    weeks_not_attempted = []

    def _add_outcomes(week_outcomes):
        for key, value in (week_outcomes or {}).items():
            if key in images:
                images[key] += value

    def _remaining_weeks(index):
        return [w.name for w in weeks[index + 1:] if w.source_workbooks]

    attempted = []  # P2-10: weeks whose output folder this run may have touched
    _OUTDATED_THIS_RUN.clear()

    for index, week in enumerate(weeks):
        if not week.source_workbooks:
            logger.warning("%s: no source workbooks found, skipping", week.name)
            continue
        attempted.append(week.name)
        try:
            summary = process_week(week, settings, client, force, sample, flagged, breaker=breaker,
                                   **reuse_kwargs)
            succeeded.append(summary)
            _add_outcomes(summary.get("outcomes") if isinstance(summary, dict) else None)
        except vision_client.GeminiQuotaExhaustedError as exc:
            logger.error("%s: Gemini daily quota exhausted -- stopping the run, no further "
                         "weeks will be attempted: %s", week.name, exc)
            failed.append({"week": week.name, "exception_type": type(exc).__name__, "error": str(exc)})
            stopped_early = {"week": week.name, "retry_delay_seconds": exc.retry_delay_seconds}
            # Phase P1-3: what main()'s stop message needs, kept separate so stopped_early's
            # shape is unchanged -- Google's reported daily limit (or None), the stopping image,
            # and the week's PARTIAL / STALE file names.
            quota_stop_details = {"quota_limit": getattr(exc, "quota_limit", None),
                                  "quota_id": getattr(exc, "quota_id", None),
                                  "image": getattr(exc, "stop_image", None),
                                  "partial_report": getattr(exc, "partial_report", None),
                                  "stale_files": list(getattr(exc, "stale_files", None) or [])}
            _add_outcomes(getattr(exc, "week_outcomes", None))
            weeks_not_attempted = _remaining_weeks(index)
            break
        except vision_client.ProviderRateLimitedError as exc:
            # Phase P1-1: own clause, after the quota clause and before the generic handler.
            # Phase P2-8: the provider-neutral base, so a Groq 429 stops the run too.
            logger.error("%s: %s rate limit (%s) -- stopping the run, no further weeks will "
                         "be attempted: %s", week.name, _provider_label(settings),
                         _rate_limit_description(exc.kind), exc)
            failed.append({"week": week.name, "exception_type": type(exc).__name__, "error": str(exc)})
            stopped_rate_limited = {"week": week.name, "kind": exc.kind,
                                    "retry_delay_seconds": exc.retry_delay_seconds}
            _add_outcomes(getattr(exc, "week_outcomes", None))
            weeks_not_attempted = _remaining_weeks(index)
            break
        except vision_client.ProviderPermanentError as exc:
            # M2: own clause before the generic handler -- a bad key / permission / model stops
            # the whole run (every later week would fail identically).
            logger.error("%s: %s %s -- stopping the run, no further weeks will be attempted: %s",
                         week.name, _provider_label(settings), _provider_error_description(exc.kind), exc)
            failed.append({"week": week.name, "exception_type": type(exc).__name__, "error": str(exc)})
            stopped_provider_error = {"week": week.name, "kind": exc.kind, "status_code": exc.status_code}
            _add_outcomes(getattr(exc, "week_outcomes", None))
            weeks_not_attempted = _remaining_weeks(index)
            break
        except ServiceUnavailableStop as exc:
            # Deliberately its own clause, after the quota clause (unchanged) and before the
            # generic isolation handler below -- which would otherwise just skip to the next week.
            logger.error("%s: %s service unavailable -- circuit breaker tripped, stopping the "
                         "run, no further weeks will be attempted: %s", week.name,
                         _provider_label(settings), exc)
            failed.append({"week": week.name, "exception_type": type(exc).__name__, "error": str(exc)})
            stopped_unavailable = {"week": week.name, "consecutive_failures": exc.consecutive_failures,
                                   "server_errors": exc.server_errors, "timeouts": exc.timeouts}
            _add_outcomes(getattr(exc, "week_outcomes", None))
            weeks_not_attempted = _remaining_weeks(index)
            break
        except Exception as exc:  # noqa: BLE001 -- isolation boundary: this week's failure must
            # never propagate and abort the rest of the run. Deliberately broad and deliberately
            # only here -- extraction/classification/report-generation keep no new handlers of
            # their own this phase (per-image state checkpointing and atomic writes inside
            # process_week() are unaffected: whatever it already durably saved before raising
            # stays saved).
            logger.error("%s: unhandled error during processing, skipping this week: %s: %s",
                         week.name, type(exc).__name__, exc)
            failed.append({"week": week.name, "exception_type": type(exc).__name__, "error": str(exc)})
    # P2-10: every report-family file still OUT OF DATE under its normal name in the weeks this run
    # attempted -- flagged by this run (even if its warning file could not be written), or by a
    # still-unresolved warning file from an earlier run. A file renamed/replaced since is not listed.
    candidates = [p for p in _OUTDATED_THIS_RUN if p.is_file()]
    automation_root = (settings.get("paths") or {}).get("automation_root")
    if automation_root:
        for name in attempted:
            candidates += _outdated_report_files(Path(automation_root) / "output" / name)
    outdated_files = []
    for p in candidates:
        if str(p) not in outdated_files:
            outdated_files.append(str(p))
    return {"succeeded": succeeded, "failed": failed, "stopped_early": stopped_early,
            "outdated_files": outdated_files,
            "stopped_unavailable": stopped_unavailable, "stopped_rate_limited": stopped_rate_limited,
            "stopped_provider_error": stopped_provider_error,
            "images": images,
            "weeks_not_attempted": weeks_not_attempted,
            "quota_stop_details": quota_stop_details}


# Phase P1-3: provider-documented quota facts quoted in operator messages. Source: Google's
# Gemini API rate-limit documentation (https://aistudio.google.com/docs/rate-limits, the page
# Google's own quota 429 links to), checked 2026-11-12. The per-model daily LIMIT is not on that
# page -- it is project-specific and shown in AI Studio -- so no limit is hard-coded anywhere.
GEMINI_RATE_LIMIT_DOCS_URL = "https://aistudio.google.com/docs/rate-limits"
GEMINI_PROJECT_LIMITS_URL = "https://aistudio.google.com/rate-limit"
GEMINI_DOCUMENTED_DAILY_RESET = "midnight Pacific time"

# Phase P1-5: --strict-exit's code for "ran, but did not complete successfully". Distinct from 1
# (configuration error), 2 (argparse usage error) and 130 (Ctrl+C), which are all unchanged.
STRICT_EXIT_CODE = 3


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m" if minutes else f"{total}s"


def _conditional_quota_days(remaining: int, daily_limit: int | None) -> int | None:
    """Phase P1-3: lower bound on quota days for `remaining` requests at a daily limit -- or None
    when no limit is known (never assumed). Valid only if each photo succeeds on its FIRST request
    and nothing else uses the project's quota; callers must say so."""
    if not daily_limit or daily_limit < 1:
        return None
    return -(-remaining // daily_limit)  # ceiling division


def _daily_quota_stop_messages(results: dict, n_succeeded: int, total_photos: int,
                               sample: int | None = None,
                               now: datetime | None = None) -> list[tuple[int, str]]:
    """Phase P1-3: the operator-facing explanation of a daily-quota stop, as (level, message)
    pairs. Distinguishes what Google's 429 actually said (retry delay, quotaId, quotaValue), what
    Google's documentation says (reset at midnight Pacific), and what this pipeline knows from
    its own state (remaining photos). It never invents a reset clock time and never assumes a
    daily limit the response did not report."""
    # stopped_early + quota_stop_details (absent from an older/stubbed run_weeks() result)
    stop = {**results["stopped_early"], **(results.get("quota_stop_details") or {})}
    images = results.get("images") or {}
    not_attempted = results.get("weeks_not_attempted") or []
    week = stop["week"]
    image = stop.get("image")
    where = f"{week} ({image})" if image else week
    quota_id = stop.get("quota_id")
    msgs = []

    msgs.append((logging.ERROR,
        f"Run stopped early: Gemini's daily request quota was exhausted while processing {where}"
        + (f" -- Google's HTTP 429 named the per-day quota {quota_id}." if quota_id
           else " -- Google's HTTP 429 identified a per-day quota.")))

    later = (f"; the {len(not_attempted)} later week(s) ({', '.join(not_attempted)}) were not "
             "attempted" if not_attempted else "")
    msgs.append((logging.ERROR,
        f"Processing stopped safely: no further Gemini requests were made. "
        f"{image or 'The photo that hit the limit'} is saved as a failed attempt (not a "
        f"classification) and will be retried automatically. No later photo in {week} was "
        f"processed{later}. Work saved before the stop is kept."))

    partial = stop.get("partial_report")
    stale = stop.get("stale_files") or []
    if partial:
        report_line = (f"Reports: {n_succeeded} week(s) finished before the stop and have current "
                       f"reports ({total_photos} photo(s)). {week}: PARTIAL report written "
                       f"({partial}) -- it is NOT complete; photos not reached are marked "
                       f"{UNPROCESSED_STATUS}.")
    else:
        report_line = (f"Reports: {n_succeeded} week(s) finished before the stop and have current "
                       f"reports ({total_photos} photo(s)). {week}: the PARTIAL report could not "
                       "be written -- there is NO current report for this week.")
    if stale:
        report_line += (f" Previous report file(s) for {week} were renamed so they cannot be "
                        f"mistaken for current results (out of date, kept, not deleted): "
                        f"{', '.join(stale)}.")
    if not_attempted:
        report_line += (" Weeks not attempted were not touched: any report already in their "
                        "output folders is from an earlier run and does not reflect this one.")
    msgs.append((logging.WARNING, report_line))

    msgs.append((logging.WARNING,
        "To resume: rerun the same command after the daily quota resets. Do not add --force -- "
        "it is not needed (failed and unprocessed photos are retried automatically) and it "
        "would re-send photos that are already done, spending quota on them."))

    delay = stop.get("retry_delay_seconds")
    if delay is not None:
        at = ((now or datetime.now().astimezone()) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M %Z")
        wait = (f"Google's response suggested retrying after {delay:.0f}s "
                f"(about {_format_duration(delay)}, i.e. around {at} -- computed from that value, "
                "not from a known reset time).")
    else:
        wait = "Google's response did not include a suggested retry delay."
    msgs.append((logging.WARNING,
        f"When the quota resets: {wait} Google's rate-limit documentation states daily quotas "
        f"reset at {GEMINI_DOCUMENTED_DAILY_RESET} and apply per project ({GEMINI_RATE_LIMIT_DOCS_URL}); "
        f"this pipeline does not compute the reset itself. Check {GEMINI_PROJECT_LIMITS_URL} for "
        "this project's current limits before rerunning."))

    remaining = images.get("api_failure", 0) + images.get("unprocessed", 0)
    exclusions = []
    if not_attempted:
        exclusions.append(f"the {len(not_attempted)} week(s) not attempted (never extracted)")
    if sample is not None:
        exclusions.append(f"photos beyond --sample {sample}")
    scope = (" NOT included: " + " and ".join(exclusions) + ".") if exclusions else ""
    remaining_line = (f"Remaining AI work known to this run: {remaining} AI-assessable photo(s) "
                      f"still need a Gemini request ({images.get('api_failure', 0)} failed "
                      f"attempt(s) + {images.get('unprocessed', 0)} not reached).{scope}")
    limit = stop.get("quota_limit")
    days = _conditional_quota_days(remaining, limit)
    if days is not None:
        remaining_line += (
            f" Conditional estimate: Google's 429 reported a limit of {limit} request(s)/day. IF "
            f"that limit is unchanged, every photo succeeds on its first request and nothing else "
            f"uses this project's quota, these {remaining} photo(s) need at least {days} more "
            "quota day(s). Retries after 503 errors can send up to 5 requests per photo and "
            "Google does not document whether failed requests count, so this is a lower bound, "
            "not a completion date.")
    else:
        remaining_line += (" No day estimate: Google's response did not report a daily limit, and "
                           "this pipeline does not assume one. Run --quota-plan with "
                           "--daily-request-limit N (from AI Studio) for a conditional estimate.")
    msgs.append((logging.WARNING, remaining_line))
    return msgs


def quota_plan(weeks: list[discovery.WeekFolder], settings: dict,
               from_registers: bool = False) -> list[dict]:
    """Phase P1-3: read-only count of AI-assessable photos still needing a Gemini request, per
    week. Extracts each week's workbooks in memory and reads (never writes) its state file;
    builds no client and makes no API call, writes no state, report, log or image. A photo is
    "done" only when state holds it as ok (state.needs_processing, without --force).
    from_registers: reads the validated registers instead (rows with a blank Official Location);
    a repeat appearance of a photo already counted in the same week is counted separately as
    "duplicates" (no request of its own)."""
    state_dir = Path(settings["paths"]["state_dir"])
    # M3: an outstanding photo needs no request of its own if another week already holds a
    # reusable result for it, or if an identical photo earlier in this plan is already counted.
    reuse_results = _cross_week_results(settings, force=False, dry_run=False)
    counted_hashes: set[str] = set()
    plan = []
    for week in weeks:
        if not week.source_workbooks:
            continue
        entry = {"week": week.name, "ai_total": 0, "done": 0, "remaining": 0, "reused": 0,
                 "error": None}
        if from_registers:
            entry["duplicates"] = 0
        try:
            if from_registers:
                ai_hashes = [r.sha256 for r in register_input.load_register_week(week) if r.needs_ai]
            else:
                extraction_result = extraction.extract_week(
                    week.source_workbooks, settings["processing"]["heading_min_merge_width"])
                ai_hashes = [state.image_hash(img.image_bytes) for img in extraction_result.images
                             if img.official_location == "Not Labelled"]
            week_state = state.load_state(state_dir, week.name)
        except Exception as exc:  # noqa: BLE001 -- one unreadable week must not hide the rest
            entry["error"] = f"{type(exc).__name__}: {exc}"
            plan.append(entry)
            continue
        week_hashes: set[str] = set()
        for content_hash in ai_hashes:
            entry["ai_total"] += 1
            if content_hash in week_hashes:  # register mode only (legacy has no same-week repeats)
                entry["duplicates"] += 1
                continue
            week_hashes.add(content_hash)
            if not state.needs_processing(week_state, content_hash, force=False):
                entry["done"] += 1
            elif ((reuse_results is not None and reuse_results.lookup(content_hash, week.name))
                  or content_hash in counted_hashes):
                entry["reused"] += 1
            else:
                entry["remaining"] += 1
                counted_hashes.add(content_hash)
        plan.append(entry)
    return plan


def _quota_plan_lines(plan: list[dict], daily_request_limit: int | None) -> list[str]:
    lines = ["QUOTA PLAN (read-only: no API calls, nothing written)", ""]
    for e in plan:
        if e["error"]:
            lines.append(f"  {e['week']}: could not be read ({e['error']}) -- not counted")
        else:
            lines.append(f"  {e['week']}: {e['ai_total']} AI-assessable, {e['done']} done, "
                         f"{e['remaining']} remaining"
                         + (f", {e['reused']} identical to a photo in another week (no request)"
                            if e.get("reused") else "")
                         + (f", {e['duplicates']} repeat appearance(s) of a photo already counted "
                            "in this week (no request)" if e.get("duplicates") else ""))
    ai_total = sum(e["ai_total"] for e in plan)
    done = sum(e["done"] for e in plan)
    remaining = sum(e["remaining"] for e in plan)
    unreadable = [e["week"] for e in plan if e["error"]]
    lines += ["", f"Total: {ai_total} AI-assessable photo(s) in {len(plan) - len(unreadable)} "
                  f"readable week(s); {done} done (saved as successful in state); {remaining} "
                  "still need a Gemini request."
                  + (f" {sum(e.get('reused', 0) for e in plan)} more are identical to a photo in "
                     "another week and reuse its result (no request)."
                     if any(e.get("reused") for e in plan) else "")
                  + (f" {sum(e.get('duplicates', 0) for e in plan)} more are repeat appearances of "
                     "a photo in the same week and copy its result (no request)."
                     if any(e.get("duplicates") for e in plan) else ""),
              "Labelled photos (official location known) never need a request and are not counted."]
    if unreadable:
        lines.append(f"Not counted: {len(unreadable)} unreadable week(s): {', '.join(unreadable)}.")
    days = _conditional_quota_days(remaining, daily_request_limit)
    if days is not None:
        lines += [f"Conditional estimate: at {daily_request_limit} request(s)/day (the limit YOU "
                  f"supplied), {remaining} request(s) need at least {days} quota day(s) -- IF every "
                  "photo succeeds on its first request, the limit is unchanged and nothing else "
                  "uses this project's quota. Retries after 503 errors can send up to 5 requests "
                  "per photo, and Google does not document whether failed requests count. A lower "
                  "bound, not a completion date."]
    else:
        lines += ["No day estimate: no daily limit was supplied and this pipeline does not assume "
                  "one. Check this project's limit at " + GEMINI_PROJECT_LIMITS_URL + " and rerun "
                  "with --daily-request-limit N."]
    lines.append(f"Google's documentation ({GEMINI_RATE_LIMIT_DOCS_URL}) states daily quotas "
                 f"reset at {GEMINI_DOCUMENTED_DAILY_RESET}, per project; verify there before "
                 "planning.")
    return lines


def _positive_int(value: str) -> int:
    """Phase P2-1: argparse type for --sample. 0 or a negative N would silently process no
    photos (images[:0]) or all but the last |N| (images[:-1]) -- never what was meant."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (1 or more), got {number}")
    return number


def _normalize_week_name(name: str) -> str:
    return " ".join(name.split()).lower()


def _select_weeks(all_weeks: list[discovery.WeekFolder],
                  requested: list[str]) -> tuple[list[discovery.WeekFolder], list[str]]:
    """Phase P2-2: (selected weeks in DISCOVERY order, requested names matching no folder, in the
    order given). Matching ignores case and runs of whitespace ("week  31" == "Week 31"), the
    same case-insensitive matching as before plus whitespace tolerance. A blank name never
    matches. Duplicates select a week once."""
    available = {_normalize_week_name(w.name) for w in all_weeks}
    wanted = [_normalize_week_name(r) for r in requested]
    unknown = [r for r, n in zip(requested, wanted) if not n or n not in available]
    selected = [w for w in all_weeks if _normalize_week_name(w.name) in set(wanted)]
    return selected, unknown


def generate_reports(weeks: list[discovery.WeekFolder], settings: dict, fmt: str,
                     overwrite: bool) -> int:
    """--generate-excel-report / --generate-json-report: one format only, every week. Returns the
    exit code: 0 when every week succeeded, 1 otherwise (missing/invalid source, JSON conflict,
    or no week folders)."""
    if not weeks:
        print("No Week NN folders found; no reports generated.", file=sys.stderr)
        return 1
    generate = final_report.generate_excel if fmt == "excel" else final_report.generate_json
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path(settings["paths"]["automation_root"]) / "backups" / f"final_report_{fmt}_{stamp}"
    results = []
    for week in weeks:
        try:
            result = generate(week.name, week.path, overwrite, backup_root)
        except Exception as exc:  # noqa: BLE001 -- one bad week never stops the others
            result = {"week": week.name, "status": final_report.INVALID,
                      "path": week.path, "detail": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(f"{week.name}: {result['status'].upper()} - {result['detail']}")
    counts = Counter(r["status"] for r in results)
    records = sum(r.get("records", 0) for r in results if r["status"] in final_report.OK_STATUSES)
    print(f"{fmt.upper()} reports: {len(results)} week(s), {records} record(s); "
          + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0 if all(r["status"] in final_report.OK_STATUSES for r in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Synergy Vessel automated image analysis workflow")
    parser.add_argument("--weeks", nargs="+", metavar="WEEK",
                        help="Process only these week folder names (default: all). Matching "
                             "ignores case and extra spaces; every name must match a discovered "
                             "week folder, or nothing is processed")
    parser.add_argument("--dry-run", action="store_true", help="Skip AI calls; extract + report only")
    parser.add_argument("--force", action="store_true", help="Re-classify every image, ignoring saved state")
    parser.add_argument("--sample", type=_positive_int, default=None, metavar="N",
                        help="Only process the first N images per week (N >= 1)")
    parser.add_argument("--quota-plan", action="store_true",
                        help="Read-only: count AI-assessable photos still needing a Gemini request "
                             "(no API calls, nothing written), then exit")
    parser.add_argument("--daily-request-limit", type=int, default=None,
                        help="With --quota-plan: your project's daily request limit (from AI "
                             "Studio), for a conditional day estimate. Never assumed.")
    parser.add_argument("--from-registers", action="store_true",
                        help="Read each week's completed Week_NN_Image_Register.xlsx and its "
                             "'Extracted Images - New' photos (validated and SHA-256 verified) "
                             "instead of re-extracting the source workbooks; every register row "
                             "is kept and the output is 'AI Classification - Week_NN.xlsx'. "
                             "Registers are never modified")
    parser.add_argument("--strict-exit", action="store_true",
                        help=f"Exit with code {STRICT_EXIT_CODE} (instead of 0) if any API failure, "
                             "early stop (quota / rate limit / circuit breaker) or failed week "
                             "occurred. Default behaviour is unchanged without it.")
    reports = parser.add_mutually_exclusive_group()
    reports.add_argument("--generate-excel-report", action="store_true",
                         help="Report-only: read and verify each week's Final Official vs AI "
                              "Location Report.xlsx (the authoritative source). Never writes JSON, "
                              "never makes API calls. With --overwrite the report is backed up "
                              "and rebuilt.")
    reports.add_argument("--generate-json-report", action="store_true",
                         help="Report-only: write 'Final Official vs AI Location Report.json' into "
                              "each week folder from that week's Excel report (read-only). Never "
                              "writes Excel, never makes API calls.")
    parser.add_argument("--overwrite", action="store_true",
                        help="With --generate-excel-report / --generate-json-report: back up and "
                             "replace an existing output that differs")
    args = parser.parse_args()
    report_mode = args.generate_excel_report or args.generate_json_report
    if args.overwrite and not report_mode:
        parser.error("--overwrite is only used with --generate-excel-report or --generate-json-report")
    if report_mode and (args.dry_run or args.force or args.sample is not None or args.quota_plan
                        or args.from_registers or args.daily_request_limit is not None):
        parser.error("--generate-excel-report / --generate-json-report cannot be combined with "
                     "--dry-run, --force, --sample, --quota-plan, --from-registers or "
                     "--daily-request-limit")
    if args.daily_request_limit is not None and not args.quota_plan:
        parser.error("--daily-request-limit is only used with --quota-plan")
    if args.daily_request_limit is not None and args.daily_request_limit < 1:
        parser.error("--daily-request-limit must be at least 1")
    if args.quota_plan and (args.dry_run or args.force or args.sample is not None):
        parser.error("--quota-plan cannot be combined with --dry-run, --force or --sample")

    # Phase P2 remediation: an expected configuration problem (missing / unreadable / invalid /
    # empty settings.yaml, or a missing or mistyped required setting) is one clean message and
    # exit 1, before discovery, logging, any client or any output. Logging cannot be configured
    # yet (logs_dir comes from these settings), so the one message goes to stderr; the exit code
    # is the integer 1, the same as the logged configuration errors further down.
    try:
        settings = config.load_validated_settings()
    except config.ConfigError as exc:
        print(f"Configuration error: {exc}. Fix automation/config/settings.yaml and rerun. "
              "No images were processed and no API requests were made.", file=sys.stderr)
        sys.exit(1)
    # Phase P2-2: discover and validate --weeks BEFORE anything is written (no log file, no
    # dry_run/ folder, no client), so an unknown name is a clean usage error (exit 2), never a
    # silently empty "successful" run. project_root is the same in every mode (dry-run included).
    project_root = Path(settings["paths"]["project_root"])
    try:
        discovered = discovery.find_week_folders(project_root)
    except OSError as exc:
        # A missing / non-folder / unreadable paths.project_root is a settings problem: the same
        # clean "Configuration error" and exit 1 as above, not a traceback.
        print(f"Configuration error: paths.project_root {project_root} could not be read as a "
              f"folder ({type(exc).__name__}: {exc.strerror or exc}). Fix "
              "automation/config/settings.yaml and rerun. No images were processed and no API "
              "requests were made.", file=sys.stderr)
        sys.exit(1)
    all_weeks = discovered
    if args.weeks is not None:
        all_weeks, unknown = _select_weeks(discovered, args.weeks)
        if unknown:
            available = ", ".join(w.name for w in discovered) or "none"
            parser.error(f"unknown week name(s) in --weeks: {', '.join(repr(u) for u in unknown)}. "
                         f"No weeks were processed. Available week folders: {available}")
    if report_mode:
        # Report-only commands: before logging / client / .env -- no API, nothing but the one
        # requested format is written.
        sys.exit(generate_reports(all_weeks, settings, "excel" if args.generate_excel_report else "json",
                                  args.overwrite))
    if args.quota_plan:
        # Phase P1-3: before logging is configured, so not even a log file is created.
        plan = quota_plan(all_weeks, settings,
                          **({"from_registers": True} if args.from_registers else {}))
        print("\n".join(_quota_plan_lines(plan, args.daily_request_limit)))
        unreadable = [e["week"] for e in plan if e["error"]]
        if args.strict_exit and unreadable:
            # Phase P1-5: strict mode only -- a plan that could not read every week is incomplete.
            print(f"--strict-exit: exiting with code {STRICT_EXIT_CODE} because {len(unreadable)} "
                  f"week(s) could not be read: {', '.join(unreadable)}.", file=sys.stderr)
            sys.exit(STRICT_EXIT_CODE)
        return
    if args.dry_run:
        # P0-1: redirect BEFORE logging is configured, so even the run log lands in dry_run/.
        try:
            settings = _dry_run_settings(settings)
        except ValueError as exc:
            sys.exit(f"Configuration error: {exc}. No images were processed.")
    log_path = logging_utils.configure_logging(Path(settings["paths"]["logs_dir"]))
    logger.info("Log file: %s", log_path)
    if args.dry_run:
        logger.warning(
            "DRY RUN: no AI requests will be made. Every output (state, reports, manifests, "
            "flagged CSV, log) is written under %s and marked DRY RUN; the real state, reports "
            "and logs are not touched. Results are placeholders, NOT classifications.",
            settings["paths"]["automation_root"],
        )

    # Phase AE5 (R2): configuration validation + client construction only -- deliberately narrow
    # (never wraps run_weeks()), so a real bug elsewhere still surfaces normally. Validates the
    # breaker threshold and RetryPolicy (retry + request timeout) in every mode, dry-run included,
    # before any image is processed or any request is made.
    try:
        _circuit_breaker_threshold(settings)
        vision_client.validate_client_settings(settings["provider"], settings.get("rate_limit"),
                                               dry_run=args.dry_run)
        if not args.dry_run:
            # Safety remediation: the ONLY place .env is ever loaded -- an explicitly requested
            # live provider run, after argument, settings, --weeks and rate-limit/provider
            # validation have all passed, immediately before the client that needs the key is
            # built. --quota-plan, --dry-run, every configuration/argument error and the tests
            # never reach this line.
            config.load_provider_environment()
        client = vision_client.build_vision_client(
            settings["provider"], settings["model"], dry_run=args.dry_run,
            rate_limit=settings.get("rate_limit"),
        )
    except (vision_client.VisionAPIError, ValueError) as exc:
        logger.error(
            "Configuration error: %s. Fix automation/config/settings.yaml (or the API key in "
            "automation/.env) and rerun. No images were processed and no API requests were made.",
            exc,
        )
        sys.exit(1)
    flagged = logging_utils.FlaggedItemsWriter(Path(settings["paths"]["logs_dir"]))

    try:
        if args.from_registers:
            logger.info("Input: completed weekly registers (--from-registers). Registers and their "
                        "photos are read and verified only, never modified; output goes to "
                        "'AI Classification - Week_NN.xlsx' in each week's output folder.")
        results = run_weeks(all_weeks, settings, client, args.force, args.sample, flagged,
                            **({"from_registers": True} if args.from_registers else {}))
    except KeyboardInterrupt:
        # Deliberately caught only here, at the top -- not inside process_week()/classify_image()/
        # _call_and_handle_errors()/save_state(), which never need to know about this. Each
        # image's state checkpoint is already a complete, atomic write (Phase O) by the time any
        # of those functions returns, so whatever finished before Ctrl+C stays safely saved; the
        # only thing actually missing was a clear message instead of a raw traceback. This never
        # touches GeminiQuotaExhaustedError's own handling in run_weeks() -- that except clause
        # still runs exactly as before for a real quota exhaustion.
        logger.error(
            "Interrupted by user. Already-completed classifications are saved; "
            "rerun the same command (no --force needed) to resume."
        )
        sys.exit(130)
    summaries = results["succeeded"]
    failed_weeks = results["failed"]
    stopped_early = results["stopped_early"]
    stopped_unavailable = results.get("stopped_unavailable")

    flagged_path = flagged.write()
    if flagged_path:
        logger.info("Flagged items for human review: %s", flagged_path)

    total_photos = sum(s["total"] for s in summaries)
    if stopped_early:
        # Phase P1-3: one log line per topic (what happened, safety, reports, resume, reset,
        # remaining work) -- see _daily_quota_stop_messages().
        for level, message in _daily_quota_stop_messages(results, len(summaries), total_photos,
                                                          sample=args.sample):
            logger.log(level, message)
    elif stopped_unavailable:
        logger.error(
            "Run stopped early: %s was unavailable for %d consecutive image(s) (%d server "
            "error(s) after all retries, %d request timeout(s)) while processing %s. %d week(s) "
            "succeeded before stopping, %d photo(s) total. No further weeks were attempted. Rerun "
            "later -- no --force needed; failed and unprocessed images will be retried "
            "automatically.",
            _provider_label(settings), stopped_unavailable["consecutive_failures"],
            stopped_unavailable.get("server_errors", 0), stopped_unavailable.get("timeouts", 0),
            stopped_unavailable["week"], len(summaries), total_photos,
        )
    elif results.get("stopped_rate_limited"):
        rl = results["stopped_rate_limited"]
        delay = rl.get("retry_delay_seconds")
        logger.error(
            "Run stopped early: %s returned HTTP 429 (%s) while processing %s. %d week(s) "
            "succeeded before stopping, %d photo(s) total. No further requests or weeks were "
            "attempted. Rerun later -- no --force needed; failed and unprocessed images will be "
            "retried automatically (provider-suggested wait: %s).",
            _provider_label(settings), _rate_limit_description(rl.get("kind")), rl["week"],
            len(summaries), total_photos,
            f"{delay:.0f}s" if delay is not None else "not provided",
        )
    elif results.get("stopped_provider_error"):
        pe = results["stopped_provider_error"]
        logger.error(
            "Run stopped early: %s reported %s (HTTP %s) while processing %s. %d week(s) "
            "succeeded before stopping, %d photo(s) total. No further requests or weeks were "
            "attempted. This will not fix itself: correct the API key in automation/.env or the "
            "provider/model in settings.yaml, then rerun the same command (no --force needed; "
            "failed and unprocessed images will be retried automatically).",
            _provider_label(settings), _provider_error_description(pe.get("kind")),
            pe.get("status_code") if pe.get("status_code") is not None else "?", pe["week"],
            len(summaries), total_photos,
        )
    else:
        images = results.get("images") or {}
        # Phase AE9: week-level success is unchanged, but a "complete" run with API failures is
        # no longer reported as a plain, unqualified success.
        log = logger.warning if images.get("api_failure") else logger.info
        log("Run complete%s. %d week(s) succeeded, %d photo(s) total, %d week(s) failed.",
            " WITH API FAILURES" if images.get("api_failure") else "",
            len(summaries), total_photos, len(failed_weeks))
        if _no_week_processed(results):
            logger.warning(
                "No week was processed: no week folder with source workbooks was found%s under "
                "paths.project_root (%s). Check project_root in settings.yaml and the week folders.",
                " among the --weeks selected" if args.weeks is not None else "",
                settings["paths"]["project_root"])
    for f in failed_weeks:
        logger.error("Failed week: %s (%s: %s)", f["week"], f["exception_type"], f["error"])
    _log_image_outcomes(results)
    outdated = results.get("outdated_files") or []
    if outdated:
        # P2-10: the one summary line an operator cannot miss -- these files LOOK current.
        logger.error(
            "%d report file(s) are OUT OF DATE but still have their normal name (most likely open "
            "in Excel during the run) -- do NOT use them: %s. Each has a warning file next to it "
            "ending in '%s'. Close them and rerun the same command (no --force needed).",
            len(outdated), "; ".join(outdated), OUTDATED_MARKER_SUFFIX.strip(),
        )
    if args.strict_exit:
        reasons = _strict_exit_reasons(results, _provider_label(settings))
        if reasons:
            logger.error("--strict-exit: exiting with code %d because processing did not complete "
                         "successfully: %s.", STRICT_EXIT_CODE, "; ".join(reasons))
            sys.exit(STRICT_EXIT_CODE)
        logger.info("--strict-exit: processing completed successfully; exit code 0.")


def _strict_exit_reasons(results: dict, provider_label: str = "Gemini") -> list[str]:
    """Phase P1-5: why a run counts as NOT successful under --strict-exit -- derived only from
    run_weeks()' existing result (no separate tracking). Empty list means success. Deliberately
    NOT failures: DRY_RUN placeholders (no request was intended), VISUAL_REVIEW (a completed
    request whose answer needs a human), labelled photos, and a --sample run covering fewer
    photos than the week has."""
    reasons = []
    if results.get("stopped_early"):
        reasons.append(f"daily quota exhausted in {results['stopped_early']['week']}")
    rate_limited = results.get("stopped_rate_limited")
    if rate_limited:
        reasons.append(f"HTTP 429 ({_rate_limit_description(rate_limited.get('kind'))}) in "
                       f"{rate_limited['week']}")
    if results.get("stopped_unavailable"):
        reasons.append(f"circuit breaker tripped ({provider_label} unavailable) in "
                       f"{results['stopped_unavailable']['week']}")
    provider_error = results.get("stopped_provider_error")
    if provider_error:
        reasons.append(f"{provider_label} provider error: "
                       f"{_provider_error_description(provider_error.get('kind'))} "
                       f"(HTTP {provider_error.get('status_code')}) in {provider_error['week']}")
    stop_weeks = {s["week"] for s in (results.get("stopped_early"), rate_limited,
                                      results.get("stopped_unavailable"), provider_error) if s}
    other_failed = [f["week"] for f in results.get("failed") or [] if f["week"] not in stop_weeks]
    if other_failed:
        reasons.append(f"{len(other_failed)} week(s) failed: {', '.join(other_failed)}")
    images = results.get("images") or {}
    if images.get("api_failure"):
        reasons.append(f"{images['api_failure']} API failure(s)")
    if images.get("unprocessed"):
        reasons.append(f"{images['unprocessed']} photo(s) unprocessed")
    if results.get("weeks_not_attempted"):
        reasons.append(f"{len(results['weeks_not_attempted'])} week(s) not attempted")
    if results.get("outdated_files"):
        # P2-10: an out-of-date file under a current-looking name is never a clean success.
        reasons.append(f"{len(results['outdated_files'])} report file(s) OUT OF DATE under their "
                       "normal name")
    if _no_week_processed(results):
        reasons.append("no week was processed (no week folder with source workbooks found)")
    return reasons


def _no_week_processed(results: dict) -> bool:
    """True when run_weeks() attempted no week at all (every early stop and every failure is
    recorded in "failed", so both lists empty means nothing ran) -- e.g. a wrong
    paths.project_root, or only week folders without source workbooks. False for an
    older/stubbed result that lacks these keys."""
    return ("succeeded" in results and "failed" in results
            and not results["succeeded"] and not results["failed"])


def _log_image_outcomes(results: dict) -> None:
    """Phase AE9: concise image-level summary, logged after the (unchanged) week-level lines.
    Exit codes are deliberately NOT changed here: completed runs and quota / circuit-breaker
    stops still return normally (0); only configuration errors (1) and Ctrl+C (130) exit
    non-zero. The warnings below are the signal that a run needs a retry."""
    images = results.get("images")
    if images is None:  # results from an older/stubbed run_weeks() without image tallies
        return
    logger.info(
        "Image outcomes: %d classified, %d need visual review, %d API failure(s), "
        "%d unprocessed (early stop), %d already labelled (no AI needed).",
        images.get("classified", 0), images.get("visual_review", 0), images.get("api_failure", 0),
        images.get("unprocessed", 0), images.get("labelled", 0),
    )
    if images.get("dry_run"):
        logger.warning("%d image(s) are DRY RUN placeholders: no AI request was made and they "
                       "were NOT classified.", images["dry_run"])
    not_attempted = results.get("weeks_not_attempted") or []
    if not_attempted:
        logger.warning("%d week(s) not attempted because the run stopped early (image counts "
                       "unknown): %s.", len(not_attempted), ", ".join(not_attempted))
    api_failures = images.get("api_failure", 0)
    if api_failures:
        no_success = images.get("classified", 0) + images.get("visual_review", 0) == 0
        logger.warning(
            "%d image(s) failed due to API errors and were NOT classified (marked API_FAILURE in "
            "the report and flagged CSV).%s Rerun later with the same command (no --force needed) "
            "to retry them.",
            api_failures, " No image was successfully classified by the AI in this run." if no_success else "",
        )


if __name__ == "__main__":
    main()
