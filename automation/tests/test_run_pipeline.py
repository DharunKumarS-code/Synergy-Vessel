"""Tests for run_pipeline.process_week()'s per-image state checkpointing (Phase O): a newly
completed classification must be durably on disk before the next image is even attempted, so an
interruption between images never loses or re-spends a Gemini request on already-done work.

extraction.extract_week() is monkeypatched to return a synthetic, controllable set of images --
no real workbook fixtures are needed, and nothing here ever contacts a real API.
"""

import csv
import hashlib
import io
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl
import pytest
from PIL import Image

import run_pipeline
from src import classifier, discovery, extraction, logging_utils, state, vision_client


def _fake_jpeg_bytes(tag: str) -> bytes:
    # report_builder.build_report() re-opens every image with PIL to embed/scale it, so these
    # must be real, decodable JPEGs -- a distinct pixel color per tag keeps content hashes
    # (and therefore state entries) unique per synthetic image.
    # Phase AE3 fix: derived from hashlib, not the builtin hash() -- hash() is randomized per
    # process (PYTHONHASHSEED), and for ~1 in 60 seeds two tags produced byte-identical JPEGs,
    # silently merging their state entries and making unrelated tests fail intermittently.
    digest = hashlib.sha256(tag.encode("utf-8")).digest()
    color = (digest[0], digest[1], digest[2])
    img = Image.new("RGB", (8, 8), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_image(tag: str, official_location: str = "Not Labelled") -> extraction.ExtractedImage:
    return extraction.ExtractedImage(
        anchor_row=1, anchor_col=1, official_location=official_location,
        image_bytes=_fake_jpeg_bytes(tag), image_ext="jpg",
        source_workbook="fake.xlsx", worksheet_name="Sheet1",
    )


def _settings(tmp_path: Path) -> dict:
    return {
        "processing": {"heading_min_merge_width": 10},
        "paths": {"automation_root": str(tmp_path), "state_dir": str(tmp_path / ".state")},
        "report": {
            "extracted_images_dirname": "Extracted Images",
            "output_filename": "report.xlsx",
            "image_cell_max_width_px": 420, "image_cell_max_height_px": 320,
        },
    }


def _week(tmp_path: Path) -> discovery.WeekFolder:
    return discovery.WeekFolder(name="Week 99", path=tmp_path, source_workbooks=[tmp_path / "fake.xlsx"])


def _patch_extract_week(monkeypatch, images: list[extraction.ExtractedImage]):
    result = extraction.ExtractionResult(images=images, report_date=None, warnings=[])
    monkeypatch.setattr(extraction, "extract_week", lambda *a, **kw: result)


class _StubClient(vision_client.VisionClient):
    """Returns a fixed successful classification, tracking how many calls it has received and
    optionally failing on specific (1-indexed) call numbers to simulate an API error.
    """

    def __init__(self, fail_on: set[int] | None = None):
        self.calls = 0
        self._fail_on = fail_on or set()

    def classify(self, image_bytes: bytes) -> dict:
        self.calls += 1
        if self.calls in self._fail_on:
            raise RuntimeError("simulated API failure")
        return {
            "person_visible": "No", "activity_identified": "No activity observed",
            "activity_confidence": "High", "ai_suggested_location": "Main Deck",
            "location_confidence": "High",
        }


class _QuotaExhaustedOnClient(vision_client.VisionClient):
    """Returns a fixed successful classification, except on one specific (1-indexed) call
    number, where it raises vision_client.GeminiQuotaExhaustedError -- simulating confirmed
    Gemini daily quota exhaustion partway through a week.
    """

    def __init__(self, exhausted_on: int, retry_delay_seconds: float | None = 6843.0):
        self.calls = 0
        self._exhausted_on = exhausted_on
        self._retry_delay_seconds = retry_delay_seconds

    def classify(self, image_bytes: bytes) -> dict:
        self.calls += 1
        if self.calls == self._exhausted_on:
            raise vision_client.GeminiQuotaExhaustedError(
                "quota exhausted (simulated)", self._retry_delay_seconds
            )
        return {
            "person_visible": "No", "activity_identified": "No activity observed",
            "activity_confidence": "High", "ai_suggested_location": "Main Deck",
            "location_confidence": "High",
        }


class _InterruptedOnClient(vision_client.VisionClient):
    """Returns a fixed successful classification, except on one specific (1-indexed) call
    number, where it raises KeyboardInterrupt -- simulating Ctrl+C while a Gemini request is
    in flight (Phase W).
    """

    def __init__(self, interrupted_on: int):
        self.calls = 0
        self._interrupted_on = interrupted_on

    def classify(self, image_bytes: bytes) -> dict:
        self.calls += 1
        if self.calls == self._interrupted_on:
            raise KeyboardInterrupt()
        return {
            "person_visible": "No", "activity_identified": "No activity observed",
            "activity_confidence": "High", "ai_suggested_location": "Main Deck",
            "location_confidence": "High",
        }


class _DiskCheckingClient(vision_client.VisionClient):
    """Fails the test immediately if, at the moment classify() is called for image N, the
    state file on disk does not already contain exactly N-1 entries -- i.e. proves each prior
    image's result was checkpointed to disk before this image was even attempted, not just
    held in memory until the week's loop finished.
    """

    def __init__(self, state_dir: Path, week_name: str):
        self._state_dir = state_dir
        self._week_name = week_name
        self.calls = 0

    def classify(self, image_bytes: bytes) -> dict:
        on_disk = state.load_state(self._state_dir, self._week_name)
        assert len(on_disk) == self.calls, (
            f"expected {self.calls} already-checkpointed entries on disk before call "
            f"{self.calls + 1}, found {len(on_disk)}"
        )
        self.calls += 1
        return {
            "person_visible": "No", "activity_identified": "No activity observed",
            "activity_confidence": "High", "ai_suggested_location": "Main Deck",
            "location_confidence": "High",
        }


def _flagged(tmp_path: Path) -> logging_utils.FlaggedItemsWriter:
    return logging_utils.FlaggedItemsWriter(tmp_path / "logs")


def test_state_is_checkpointed_after_each_image_not_just_at_week_end(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    client = _DiskCheckingClient(Path(settings["paths"]["state_dir"]), "Week 99")

    run_pipeline.process_week(_week(tmp_path), settings, client, force=False,
                               sample=None, flagged=_flagged(tmp_path))

    assert client.calls == 4
    final_state = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    assert len(final_state) == 4
    assert all(entry["ok"] is True for entry in final_state.values())


def test_simulated_interruption_preserves_completed_images_and_resume_skips_them(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(5)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    # "Run 1": the process only gets through the first 3 images before stopping (sample=3
    # stands in for an interruption -- from the state file's perspective, on disk there is no
    # difference between "the operator capped this run" and "the process was killed after 3").
    client1 = _StubClient()
    run_pipeline.process_week(_week(tmp_path), settings, client1, force=False,
                               sample=3, flagged=_flagged(tmp_path))
    assert client1.calls == 3
    after_run1 = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    assert len(after_run1) == 3

    # "Run 2" (resume, full image list this time): the 3 already-completed images must be
    # skipped, not re-sent to the (stub) API, and only the remaining 2 should be classified.
    client2 = _StubClient()
    run_pipeline.process_week(_week(tmp_path), settings, client2, force=False,
                               sample=None, flagged=_flagged(tmp_path))
    assert client2.calls == 2  # only the 2 images never reached in run 1
    final_state = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    assert len(final_state) == 5


def test_failed_image_remains_retryable_after_checkpointed_resume(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client1 = _StubClient(fail_on={2})  # the 2nd image's classification fails
    run_pipeline.process_week(_week(tmp_path), settings, client1, force=False,
                               sample=None, flagged=_flagged(tmp_path))
    assert client1.calls == 3
    state_dir = Path(settings["paths"]["state_dir"])
    after_run1 = state.load_state(state_dir, "Week 99")
    assert len(after_run1) == 3
    ok_flags = sorted(entry["ok"] for entry in after_run1.values())
    assert ok_flags == [False, True, True]  # exactly one failure recorded

    client2 = _StubClient()
    run_pipeline.process_week(_week(tmp_path), settings, client2, force=False,
                               sample=None, flagged=_flagged(tmp_path))
    assert client2.calls == 1  # only the previously-failed image is retried
    final_state = state.load_state(state_dir, "Week 99")
    assert all(entry["ok"] is True for entry in final_state.values())


def test_n_failed_counter_reflects_api_errors_only(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client = _StubClient(fail_on={1})
    summary = run_pipeline.process_week(_week(tmp_path), settings, client, force=False,
                                         sample=None, flagged=_flagged(tmp_path))
    assert summary["classified"] == 3
    assert summary["failed"] == 1


def test_ordinary_needs_review_without_error_does_not_count_as_failed(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(2)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    class _UncertainClient(vision_client.VisionClient):
        def classify(self, image_bytes: bytes) -> dict:
            # a genuine, successful API response -- just one the schema/guardrails mark Needs
            # Review (an ambiguous person), not an API error.
            return {
                "person_visible": "Uncertain", "activity_identified": "irrelevant",
                "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                "location_confidence": "High",
            }

    summary = run_pipeline.process_week(_week(tmp_path), settings, _UncertainClient(),
                                         force=False, sample=None, flagged=_flagged(tmp_path))
    assert summary["failed"] == 0
    assert summary["classified"] == 2
    final_state = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    assert all(entry["ok"] is True for entry in final_state.values())


def test_dry_run_null_client_still_processes_without_error(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(2)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    summary = run_pipeline.process_week(_week(tmp_path), settings, vision_client.NullVisionClient(),
                                         force=False, sample=None, flagged=_flagged(tmp_path))
    assert summary["classified"] == 2
    assert summary["failed"] == 0
    assert (tmp_path / "output" / "Week 99" / "report.xlsx").exists()


# ---------------------------------------------------------------------------
# Phase Q: run_weeks() per-week exception isolation. process_week() itself is monkeypatched
# here -- these tests are specifically about the orchestration boundary (run_weeks), not about
# extraction/classification/report-generation, which keep no new exception handling this phase.
# ---------------------------------------------------------------------------


def _week_folders(*names: str) -> list[discovery.WeekFolder]:
    return [discovery.WeekFolder(name=n, path=Path(n), source_workbooks=[Path(n) / "fake.xlsx"])
            for n in names]


def _fake_summary(week_name: str) -> dict:
    return {"week": week_name, "total": 1, "classified": 1, "failed": 0, "skipped": 0}


def test_a_continue_after_one_week_fails(monkeypatch):
    weeks = _week_folders("Week 1", "Week 2", "Week 3")
    attempted = []

    def fake_process_week(week, settings, client, force, sample, flagged, breaker=None):
        attempted.append(week.name)
        if week.name == "Week 1":
            raise RuntimeError("simulated extraction failure")
        return _fake_summary(week.name)

    monkeypatch.setattr(run_pipeline, "process_week", fake_process_week)
    results = run_pipeline.run_weeks(weeks, {}, object(), force=False, sample=None, flagged=object())

    # all three weeks must be attempted -- week 1 failing must not stop week 2 or week 3.
    assert attempted == ["Week 1", "Week 2", "Week 3"]
    assert [s["week"] for s in results["succeeded"]] == ["Week 2", "Week 3"]
    assert [f["week"] for f in results["failed"]] == ["Week 1"]


def test_b_failure_recorded_in_summary_with_exception_details(monkeypatch):
    weeks = _week_folders("Week 1", "Week 2")

    def fake_process_week(week, settings, client, force, sample, flagged, breaker=None):
        if week.name == "Week 1":
            raise ValueError("corrupt workbook: bad magic bytes")
        return _fake_summary(week.name)

    monkeypatch.setattr(run_pipeline, "process_week", fake_process_week)
    results = run_pipeline.run_weeks(weeks, {}, object(), force=False, sample=None, flagged=object())

    assert len(results["failed"]) == 1
    failure = results["failed"][0]
    assert failure["week"] == "Week 1"
    assert failure["exception_type"] == "ValueError"
    assert "corrupt workbook" in failure["error"]  # original exception detail not hidden

    assert len(results["succeeded"]) == 1
    assert results["succeeded"][0] == _fake_summary("Week 2")  # successful week's summary unchanged


def test_c_all_weeks_fail_orchestration_completes_without_raising(monkeypatch):
    weeks = _week_folders("Week 1", "Week 2", "Week 3")

    def fake_process_week(week, settings, client, force, sample, flagged, breaker=None):
        raise RuntimeError(f"failure in {week.name}")

    monkeypatch.setattr(run_pipeline, "process_week", fake_process_week)
    # must not raise -- run_weeks() itself completes its orchestration cleanly.
    results = run_pipeline.run_weeks(weeks, {}, object(), force=False, sample=None, flagged=object())

    assert results["succeeded"] == []
    assert [f["week"] for f in results["failed"]] == ["Week 1", "Week 2", "Week 3"]
    assert all(f["exception_type"] == "RuntimeError" for f in results["failed"])


def test_d_successful_run_behavior_unchanged(monkeypatch):
    weeks = _week_folders("Week 1", "Week 2", "Week 3")

    def fake_process_week(week, settings, client, force, sample, flagged, breaker=None):
        return _fake_summary(week.name)

    monkeypatch.setattr(run_pipeline, "process_week", fake_process_week)
    results = run_pipeline.run_weeks(weeks, {}, object(), force=False, sample=None, flagged=object())

    assert results["failed"] == []
    assert results["succeeded"] == [_fake_summary("Week 1"), _fake_summary("Week 2"), _fake_summary("Week 3")]


def test_run_weeks_skips_weeks_with_no_source_workbooks_without_counting_as_failed(monkeypatch):
    weeks = _week_folders("Week 1") + [discovery.WeekFolder(name="Week 2", path=Path("Week 2"), source_workbooks=[])]

    def fake_process_week(week, settings, client, force, sample, flagged, breaker=None):
        return _fake_summary(week.name)

    monkeypatch.setattr(run_pipeline, "process_week", fake_process_week)
    results = run_pipeline.run_weeks(weeks, {}, object(), force=False, sample=None, flagged=object())

    assert [s["week"] for s in results["succeeded"]] == ["Week 1"]
    assert results["failed"] == []  # a week with no workbooks is skipped, not a "failure"


# ---------------------------------------------------------------------------
# Phase R: a genuinely corrupt real workbook, run through the REAL extraction.extract_week()
# (not monkeypatched) and the REAL run_weeks()/process_week() chain end-to-end -- proving Phase
# Q's isolation boundary already catches extraction.WorkbookExtractionError correctly with zero
# changes needed to run_pipeline.py itself.
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_failed_extraction_is_recorded_and_does_not_block_other_weeks(tmp_path):
    bad_file = tmp_path / "corrupt.xlsx"
    bad_file.write_bytes(b"not a real workbook")

    bad_week = discovery.WeekFolder(name="Week Bad", path=tmp_path, source_workbooks=[bad_file])
    good_week = discovery.WeekFolder(name="Week Good", path=FIXTURES,
                                      source_workbooks=[FIXTURES / "sample_week_part1.xlsx"])

    settings = _settings(tmp_path)
    client = _StubClient()

    results = run_pipeline.run_weeks([bad_week, good_week], settings, client,
                                      force=False, sample=None, flagged=_flagged(tmp_path))

    # the failed week must be recorded, with the real exception type, and must NOT appear as
    # successfully completed.
    assert [f["week"] for f in results["failed"]] == ["Week Bad"]
    assert results["failed"][0]["exception_type"] == "WorkbookExtractionError"
    assert "corrupt.xlsx" in results["failed"][0]["error"]

    # the good week must still be processed -- the bad one never blocked it.
    assert [s["week"] for s in results["succeeded"]] == ["Week Good"]
    assert client.calls == 1  # only Week Good's single "Not Labelled" image was ever classified

    # no misleading report for the failed week; a real report for the one that succeeded.
    output_root = Path(settings["paths"]["automation_root"]) / "output"
    assert not (output_root / "Week Bad" / "report.xlsx").exists()
    assert (output_root / "Week Good" / "report.xlsx").exists()


# ---------------------------------------------------------------------------
# Phase S: a malformed image causing report_builder.ReportGenerationError, through the REAL
# process_week()/report_builder.build_report() chain -- proving Phase Q's isolation boundary
# already catches this too, with zero further changes to run_pipeline.py.
# ---------------------------------------------------------------------------


def test_report_generation_failure_is_recorded_and_does_not_block_other_weeks(tmp_path, monkeypatch):
    bad_image = extraction.ExtractedImage(
        anchor_row=1, anchor_col=1, official_location="Main Deck",
        image_bytes=b"not a real image at all", image_ext="jpg",
        source_workbook="bad.xlsx", worksheet_name="Sheet1",
    )
    good_image = _make_image("good", official_location="Main Deck")

    bad_wb = tmp_path / "bad.xlsx"
    good_wb = tmp_path / "good.xlsx"
    results_by_workbook = {
        str(bad_wb): extraction.ExtractionResult(images=[bad_image], report_date=None, warnings=[]),
        str(good_wb): extraction.ExtractionResult(images=[good_image], report_date=None, warnings=[]),
    }

    def fake_extract_week(source_workbooks, *args, **kwargs):
        return results_by_workbook[str(source_workbooks[0])]

    monkeypatch.setattr(extraction, "extract_week", fake_extract_week)

    bad_week = discovery.WeekFolder(name="Week Bad", path=tmp_path, source_workbooks=[bad_wb])
    good_week = discovery.WeekFolder(name="Week Good", path=tmp_path, source_workbooks=[good_wb])

    settings = _settings(tmp_path)
    client = _StubClient()

    results = run_pipeline.run_weeks([bad_week, good_week], settings, client,
                                      force=False, sample=None, flagged=_flagged(tmp_path))

    assert [f["week"] for f in results["failed"]] == ["Week Bad"]
    assert results["failed"][0]["exception_type"] == "ReportGenerationError"
    assert [s["week"] for s in results["succeeded"]] == ["Week Good"]

    output_root = Path(settings["paths"]["automation_root"]) / "output"
    assert not (output_root / "Week Bad" / "report.xlsx").exists()
    assert (output_root / "Week Good" / "report.xlsx").exists()


# ---------------------------------------------------------------------------
# Phase V: confirmed Gemini daily quota exhaustion stops the current week immediately and
# every subsequent week, while preserving everything already checkpointed.
# ---------------------------------------------------------------------------


def test_quota_exhaustion_stops_remaining_images_in_the_same_week(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(5)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    client = _QuotaExhaustedOnClient(exhausted_on=3)  # images 1, 2 succeed; image 3 exhausts quota
    with pytest.raises(vision_client.GeminiQuotaExhaustedError):
        run_pipeline.process_week(_week(tmp_path), settings, client, force=False,
                                   sample=None, flagged=_flagged(tmp_path))

    assert client.calls == 3  # never attempted images 4 or 5
    final_state = state.load_state(state_dir, "Week 99")
    assert len(final_state) == 3  # images 1, 2 succeeded; image 3 checkpointed as failed
    ok_flags = [entry["ok"] for entry in final_state.values()]
    assert ok_flags.count(True) == 2
    assert ok_flags.count(False) == 1
    # no report for a week that never finished its per-image loop
    assert not (tmp_path / "output" / "Week 99" / "report.xlsx").exists()


def test_quota_exhaustion_stops_subsequent_weeks(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    weeks = _week_folders("Week 1", "Week 2", "Week 3")
    client = _QuotaExhaustedOnClient(exhausted_on=2)  # exhausts partway through Week 1

    results = run_pipeline.run_weeks(weeks, settings, client, force=False, sample=None,
                                      flagged=_flagged(tmp_path))

    assert client.calls == 2  # Week 2 and Week 3 never even started
    assert results["succeeded"] == []  # Week 1 never finished -- not recorded as successful
    assert [f["week"] for f in results["failed"]] == ["Week 1"]
    assert results["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}


def test_quota_exhaustion_preserves_prior_successes_and_leaves_rest_unprocessed(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    client = _QuotaExhaustedOnClient(exhausted_on=2)
    with pytest.raises(vision_client.GeminiQuotaExhaustedError):
        run_pipeline.process_week(_week(tmp_path), settings, client, force=False,
                                   sample=None, flagged=_flagged(tmp_path))

    final_state = state.load_state(state_dir, "Week 99")
    assert len(final_state) == 2  # image 1 (success) + image 2 (quota failure) only
    oks = sorted(entry["ok"] for entry in final_state.values())
    assert oks == [False, True]
    # images 3 and 4 were never reached -- genuinely absent from state, not recorded in any form

    # the successful entry's content, not just its "ok" flag, must be exactly what the client
    # actually returned for image 1 -- never touched/rewritten by the later quota-exhaustion
    # checkpoint for image 2.
    successful_entry = next(e for e in final_state.values() if e["ok"] is True)
    assert successful_entry == {
        "ok": True, "activity_identified": "No activity observed", "activity_confidence": "High",
        "ai_suggested_location": "Main Deck", "location_confidence": "High", "person_visible": "No",
    }


def test_later_run_retries_failed_and_unprocessed_images_after_quota_exhaustion(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    client1 = _QuotaExhaustedOnClient(exhausted_on=2)
    with pytest.raises(vision_client.GeminiQuotaExhaustedError):
        run_pipeline.process_week(_week(tmp_path), settings, client1, force=False,
                                   sample=None, flagged=_flagged(tmp_path))
    assert len(state.load_state(state_dir, "Week 99")) == 2

    # "later run" (no --force): must retry image 2 (failed) and images 3/4 (never attempted).
    client2 = _StubClient()
    run_pipeline.process_week(_week(tmp_path), settings, client2, force=False,
                               sample=None, flagged=_flagged(tmp_path))
    assert client2.calls == 3  # images 2, 3, 4 -- image 1's prior success was correctly skipped
    final_state = state.load_state(state_dir, "Week 99")
    assert len(final_state) == 4
    assert all(entry["ok"] is True for entry in final_state.values())
    assert (tmp_path / "output" / "Week 99" / "report.xlsx").exists()


def test_dry_run_null_client_never_triggers_quota_stop(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    # NullVisionClient.classify() never raises anything -- confirms dry-run behavior is
    # completely unaffected by the new quota-stop logic.
    summary = run_pipeline.process_week(_week(tmp_path), settings, vision_client.NullVisionClient(),
                                         force=False, sample=None, flagged=_flagged(tmp_path))
    assert summary["failed"] == 0
    assert summary["classified"] == 3
    assert (tmp_path / "output" / "Week 99" / "report.xlsx").exists()


def test_ordinary_non_quota_error_does_not_trigger_global_stop(tmp_path, monkeypatch):
    # a plain failure (not GeminiQuotaExhaustedError) must still only isolate its own week,
    # exactly like Phase Q -- never trigger the global quota-stop path.
    images = [_make_image(str(i)) for i in range(2)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    weeks = _week_folders("Week 1", "Week 2")
    client = _StubClient(fail_on={1})  # ordinary RuntimeError, not quota-related

    results = run_pipeline.run_weeks(weeks, settings, client, force=False, sample=None,
                                      flagged=_flagged(tmp_path))

    assert results["stopped_early"] is None
    assert [s["week"] for s in results["succeeded"]] == ["Week 1", "Week 2"]  # Week 1 still "succeeds"
    # (process_week itself doesn't raise for an ordinary per-image failure -- only
    # GeminiQuotaExhaustedError propagates out of the per-image loop.)


# ---------------------------------------------------------------------------
# Phase W: KeyboardInterrupt during a Gemini call must not be caught by any lower-level
# handler, must preserve already-checkpointed images, must leave the interrupted image
# retryable, and main()'s new top-level handler must log a clear message and exit(130) --
# without disturbing Phase V's separate GeminiQuotaExhaustedError handling.
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_is_not_caught_by_process_week_or_run_weeks(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client = _InterruptedOnClient(interrupted_on=2)
    weeks = _week_folders("Week 1", "Week 2")

    # must propagate all the way out of run_weeks() uncaught -- neither its
    # "except GeminiQuotaExhaustedError" nor its "except Exception" may intercept this.
    with pytest.raises(KeyboardInterrupt):
        run_pipeline.run_weeks(weeks, settings, client, force=False, sample=None,
                                flagged=_flagged(tmp_path))

    assert client.calls == 2  # stopped mid-Week-1, Week 2 never started


def test_keyboard_interrupt_preserves_prior_checkpoint_and_leaves_image_retryable(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    client1 = _InterruptedOnClient(interrupted_on=2)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline.process_week(_week(tmp_path), settings, client1, force=False,
                                   sample=None, flagged=_flagged(tmp_path))

    # image 1 succeeded and was checkpointed before the interrupt; image 2 (interrupted) and
    # image 3 (never reached) have no entry at all -- not "ok": false, genuinely absent, since
    # classify_image() itself never returned for image 2.
    after_interrupt = state.load_state(state_dir, "Week 99")
    assert len(after_interrupt) == 1
    assert all(entry["ok"] is True for entry in after_interrupt.values())

    # a later run (no --force) must retry images 2 and 3, and skip the already-done image 1.
    client2 = _StubClient()
    run_pipeline.process_week(_week(tmp_path), settings, client2, force=False,
                               sample=None, flagged=_flagged(tmp_path))
    assert client2.calls == 2
    final_state = state.load_state(state_dir, "Week 99")
    assert len(final_state) == 3
    assert all(entry["ok"] is True for entry in final_state.values())


def test_quota_exhaustion_handling_unaffected_by_keyboard_interrupt_support(tmp_path, monkeypatch):
    # regression guard: GeminiQuotaExhaustedError must still take its own early-stop path,
    # completely unaffected by KeyboardInterrupt now being a possibility too.
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    weeks = _week_folders("Week 1", "Week 2")
    client = _QuotaExhaustedOnClient(exhausted_on=2)

    results = run_pipeline.run_weeks(weeks, settings, client, force=False, sample=None,
                                      flagged=_flagged(tmp_path))

    assert results["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}
    assert results["succeeded"] == []
    assert [f["week"] for f in results["failed"]] == ["Week 1"]


def test_main_keyboard_interrupt_logs_clear_message_and_exits_130(tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path)
    settings["provider"] = "gemini"
    settings["model"] = "unused-in-dry-run"
    settings["paths"]["logs_dir"] = str(tmp_path / "logs")
    settings["paths"]["project_root"] = str(tmp_path)

    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: [])

    def _raise_keyboard_interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(run_pipeline, "run_weeks", _raise_keyboard_interrupt)

    with caplog.at_level(logging.ERROR, logger="pipeline"):
        with pytest.raises(SystemExit) as exc_info:
            run_pipeline.main()

    assert exc_info.value.code == 130
    messages = [r.message for r in caplog.records]
    assert any("Interrupted by user" in m for m in messages)
    assert any("no --force needed" in m for m in messages)


def test_main_quota_exhaustion_path_unaffected_by_new_interrupt_handling(tmp_path, monkeypatch, caplog):
    # regression guard at the main() level: a GeminiQuotaExhaustedError-driven stopped_early
    # result must still be reported via the existing log line, not swallowed or rerouted by
    # the new try/except KeyboardInterrupt wrapper.
    settings = _settings(tmp_path)
    settings["provider"] = "gemini"
    settings["model"] = "unused-in-dry-run"
    settings["paths"]["logs_dir"] = str(tmp_path / "logs")
    settings["paths"]["project_root"] = str(tmp_path)

    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: [])
    monkeypatch.setattr(run_pipeline, "run_weeks", lambda *a, **kw: {
        "succeeded": [], "failed": [{"week": "Week 1", "exception_type": "GeminiQuotaExhaustedError", "error": "x"}],
        "stopped_early": {"week": "Week 1", "retry_delay_seconds": 6843.0},
    })

    with caplog.at_level(logging.ERROR, logger="pipeline"):
        run_pipeline.main()  # must return normally -- no SystemExit(130) for this path

    messages = [r.message for r in caplog.records]
    assert any("quota was exhausted" in m for m in messages)
    assert not any("Interrupted by user" in m for m in messages)


# ---------------------------------------------------------------------------
# Phase X: the full combined recovery scenario from the task's "Recovery simulation" --
# one successful image, one confirmed-quota-exhausted image, additional unprocessed images,
# and a second week that never even starts -- then a second run once the quota condition is
# gone, confirming exactly the right set of images gets (re)classified and nothing else.
# ---------------------------------------------------------------------------


def test_full_recovery_simulation_across_two_weeks(tmp_path, monkeypatch):
    week1_images = [_make_image(f"w1-{i}") for i in range(4)]  # 1 success, 1 quota-hit, 2 unseen
    week2_images = [_make_image(f"w2-{i}") for i in range(2)]  # never reached in run 1

    week1_wb = tmp_path / "week1.xlsx"
    week2_wb = tmp_path / "week2.xlsx"
    results_by_workbook = {
        str(week1_wb): extraction.ExtractionResult(images=week1_images, report_date=None, warnings=[]),
        str(week2_wb): extraction.ExtractionResult(images=week2_images, report_date=None, warnings=[]),
    }
    monkeypatch.setattr(extraction, "extract_week",
                         lambda source_workbooks, *a, **kw: results_by_workbook[str(source_workbooks[0])])

    week1 = discovery.WeekFolder(name="Week 1", path=tmp_path, source_workbooks=[week1_wb])
    week2 = discovery.WeekFolder(name="Week 2", path=tmp_path, source_workbooks=[week2_wb])
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    # Run 1: quota exhausts on Week 1's 2nd image.
    client1 = _QuotaExhaustedOnClient(exhausted_on=2)
    results1 = run_pipeline.run_weeks([week1, week2], settings, client1, force=False,
                                       sample=None, flagged=_flagged(tmp_path))

    assert client1.calls == 2  # Week 2 never even started
    assert results1["succeeded"] == []
    assert results1["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}

    week1_state_after_run1 = state.load_state(state_dir, "Week 1")
    assert len(week1_state_after_run1) == 2  # image 1 (success) + image 2 (quota failure)
    oks_after_run1 = sorted(e["ok"] for e in week1_state_after_run1.values())
    assert oks_after_run1 == [False, True]
    assert state.load_state(state_dir, "Week 2") == {}  # Week 2 untouched -- no file at all

    # Run 2: quota condition is gone (plain successful stub client this time).
    client2 = _StubClient()
    results2 = run_pipeline.run_weeks([week1, week2], settings, client2, force=False,
                                       sample=None, flagged=_flagged(tmp_path))

    # Week 1: only images 2, 3, 4 are reclassified (image 1's success was skipped).
    # Week 2: both of its images are classified for the first time. 3 + 2 = 5.
    assert client2.calls == 5
    assert results2["stopped_early"] is None
    assert results2["failed"] == []
    assert [s["week"] for s in results2["succeeded"]] == ["Week 1", "Week 2"]

    final_week1_state = state.load_state(state_dir, "Week 1")
    final_week2_state = state.load_state(state_dir, "Week 2")
    assert len(final_week1_state) == 4 and all(e["ok"] is True for e in final_week1_state.values())
    assert len(final_week2_state) == 2 and all(e["ok"] is True for e in final_week2_state.values())


# ---------------------------------------------------------------------------
# Phase AA: flagged-CSV failure_type (API_FAILURE vs VISUAL_REVIEW) at the real
# process_week()/run_weeks() integration level.
# ---------------------------------------------------------------------------


def _read_flagged_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_api_failure_appears_as_api_failure_in_the_flagged_csv(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(1)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)

    client = _StubClient(fail_on={1})  # simulates an exhausted-retries 503 reaching classifier.py
    run_pipeline.process_week(_week(tmp_path), settings, client, force=False,
                               sample=None, flagged=flagged)

    rows = _read_flagged_rows(flagged.path)
    assert len(rows) == 1
    assert rows[0]["failure_type"] == "API_FAILURE"
    assert "simulated API failure" in rows[0]["reason"]


def test_confirmed_quota_exhaustion_appears_as_api_failure_in_the_flagged_csv(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(2)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)

    client = _QuotaExhaustedOnClient(exhausted_on=1)
    with pytest.raises(vision_client.GeminiQuotaExhaustedError):
        run_pipeline.process_week(_week(tmp_path), settings, client, force=False,
                                   sample=None, flagged=flagged)

    # without this fix the quota-exhausted image would never reach the CSV at all, since
    # process_week() raises before its shared flagging block.
    rows = _read_flagged_rows(flagged.path)
    assert len(rows) == 1
    assert rows[0]["failure_type"] == "API_FAILURE"
    assert "GeminiQuotaExhaustedError" in rows[0]["reason"]


def test_successful_needs_review_appears_as_visual_review_not_api_failure(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(1)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)

    class _GenuinelyUncertainClient(vision_client.VisionClient):
        def classify(self, image_bytes: bytes) -> dict:
            # a real, successful API response -- just one the guardrails mark Needs Review.
            return {
                "person_visible": "Uncertain", "activity_identified": "irrelevant",
                "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                "location_confidence": "High",
            }

    run_pipeline.process_week(_week(tmp_path), settings, _GenuinelyUncertainClient(),
                               force=False, sample=None, flagged=flagged)

    rows = _read_flagged_rows(flagged.path)
    assert len(rows) == 1
    assert rows[0]["failure_type"] == "VISUAL_REVIEW"
    assert rows[0]["reason"] == "Low/Needs-Review confidence on activity or location"


def test_flagged_csv_never_exposes_the_api_key_on_a_real_failure(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(1)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)

    fake_secret = "sk-totally-fake-secret-12345"
    client = vision_client.GeminiVisionClient(model="gemini-3.8-flash", api_key=fake_secret)
    monkeypatch.setattr(client, "_call",
                         lambda image_bytes: (_ for _ in ()).throw(RuntimeError(f"boom {fake_secret}")))

    run_pipeline.process_week(_week(tmp_path), settings, client, force=False,
                               sample=None, flagged=flagged)

    rows = _read_flagged_rows(flagged.path)
    assert len(rows) == 1
    assert rows[0]["failure_type"] == "API_FAILURE"
    assert fake_secret not in rows[0]["reason"]
    assert "REDACTED" in rows[0]["reason"]


def test_existing_flagged_csv_columns_remain_compatible(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(1)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)

    run_pipeline.process_week(_week(tmp_path), settings, vision_client.NullVisionClient(),
                               force=False, sample=None, flagged=flagged)

    rows = _read_flagged_rows(flagged.path)
    assert list(rows[0].keys()) == [
        "week", "image_filename", "official_location", "ai_suggested_location",
        "location_confidence", "activity_identified", "reason", "failure_type",
    ]


# ---------------------------------------------------------------------------
# Phase AC: the real Excel report's "Classification Status" column, produced end-to-end
# through process_week(), must agree with the same _classification_status() mapping the
# flagged CSV already uses -- and Phase V/W behavior must remain completely unaffected.
# ---------------------------------------------------------------------------


def _read_report_status_column(settings: dict, week_name: str) -> list[str]:
    report_path = Path(settings["paths"]["automation_root"]) / "output" / week_name / "report.xlsx"
    wb = openpyxl.load_workbook(report_path)
    ws = wb[wb.sheetnames[0]]
    return [ws.cell(r, 11).value for r in range(2, ws.max_row + 1)]


def test_api_failure_produces_api_failure_status_in_the_real_report(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(1)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    run_pipeline.process_week(_week(tmp_path), settings, _StubClient(fail_on={1}),
                               force=False, sample=None, flagged=_flagged(tmp_path))

    assert _read_report_status_column(settings, "Week 99") == ["API_FAILURE"]


def test_successful_uncertain_classification_produces_visual_review_status(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(1)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    class _GenuinelyUncertainClient(vision_client.VisionClient):
        def classify(self, image_bytes: bytes) -> dict:
            return {
                "person_visible": "Uncertain", "activity_identified": "irrelevant",
                "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                "location_confidence": "High",
            }

    run_pipeline.process_week(_week(tmp_path), settings, _GenuinelyUncertainClient(),
                               force=False, sample=None, flagged=_flagged(tmp_path))

    assert _read_report_status_column(settings, "Week 99") == ["VISUAL_REVIEW"]


def test_successful_confident_classification_produces_classified_status(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(1)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    run_pipeline.process_week(_week(tmp_path), settings, _StubClient(),  # default: always succeeds
                               force=False, sample=None, flagged=_flagged(tmp_path))

    assert _read_report_status_column(settings, "Week 99") == ["CLASSIFIED"]


def test_quota_exhausted_result_maps_to_api_failure_status(tmp_path):
    # unit-level, matching Phase AB's finding: a week that hits confirmed quota exhaustion
    # never reaches build_report() at all (no partial/misleading report -- unchanged Phase S
    # design), so this is verified directly against the shared mapping function instead.
    result = classifier.ClassificationResult(
        activity_identified="Needs Review", activity_confidence="Needs Review",
        ai_suggested_location="Needs Review", location_confidence="Needs Review",
        person_visible="Uncertain", error="GeminiQuotaExhaustedError: quota exhausted",
        quota_exhausted=True, retry_delay_seconds=6843.0,
    )
    assert run_pipeline._classification_status(result, needs_review=True) == "API_FAILURE"


def test_quota_handling_and_interruption_behavior_unchanged_by_status_column(tmp_path, monkeypatch):
    # regression guard: Phase V (global quota stop) and Phase W (KeyboardInterrupt) must behave
    # identically after adding the Classification Status column.
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    quota_client = _QuotaExhaustedOnClient(exhausted_on=1)
    with pytest.raises(vision_client.GeminiQuotaExhaustedError):
        run_pipeline.process_week(_week(tmp_path), settings, quota_client, force=False,
                                   sample=None, flagged=_flagged(tmp_path))
    assert not (tmp_path / "output" / "Week 99" / "report.xlsx").exists()

    interrupt_client = _InterruptedOnClient(interrupted_on=1)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline.process_week(_week(tmp_path), settings, interrupt_client, force=False,
                                   sample=None, flagged=_flagged(tmp_path))


# ---------------------------------------------------------------------------
# Phase AE1: one combined recovery scenario -- success, then a 503-exhausted failure, then a
# KeyboardInterrupt (which stops everything), leaving the 4th image genuinely unprocessed --
# followed by a second run (condition removed) that must finish the week correctly.
# ---------------------------------------------------------------------------


class _MixedRecoveryClient(vision_client.VisionClient):
    """Call 1: succeeds confidently (CLASSIFIED, no review). Call 2: raises a plain RuntimeError
    (stand-in for a real GeminiVisionClient._call() that already exhausted 5 real 503 retries
    and is now raising the final wrapped error -- that exact mechanism is covered separately by
    the real-tenacity tests). Call 3: raises KeyboardInterrupt, stopping everything. Call 4 is
    never reached in this run.
    """

    def __init__(self):
        self.calls = 0

    def classify(self, image_bytes: bytes) -> dict:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("503 UNAVAILABLE (simulated, retries exhausted)")
        if self.calls == 3:
            raise KeyboardInterrupt()
        return {
            "person_visible": "No", "activity_identified": "No activity observed",
            "activity_confidence": "High", "ai_suggested_location": "Main Deck",
            "location_confidence": "High",
        }


def test_combined_recovery_success_failure_interrupt_unprocessed(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    # --- Run 1 ---
    client1 = _MixedRecoveryClient()
    flagged1 = _flagged(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline.process_week(_week(tmp_path), settings, client1, force=False,
                                   sample=None, flagged=flagged1)

    assert client1.calls == 3  # image 4 genuinely never attempted
    after_run1 = state.load_state(state_dir, "Week 99")
    assert len(after_run1) == 2  # image 1 (success) + image 2 (503 failure) only -- image 3
    # (interrupted) and image 4 (unprocessed) have no entry at all, not "ok": false.
    oks = sorted(e["ok"] for e in after_run1.values())
    assert oks == [False, True]
    assert not (tmp_path / "output" / "Week 99" / "report.xlsx").exists()  # week never finished

    rows1 = _read_flagged_rows(flagged1.path)
    assert len(rows1) == 1  # only image 2's 503 failure was flagged; image 1 needed no review
    assert rows1[0]["failure_type"] == "API_FAILURE"
    assert "503" in rows1[0]["reason"]

    # --- Run 2: condition removed, everything succeeds now ---
    # a distinct logs subdirectory (not the same timestamp-based path as flagged1) --
    # avoids any possibility of a same-second filename collision between the two writers.
    client2 = _StubClient()
    flagged2 = logging_utils.FlaggedItemsWriter(tmp_path / "logs_run2")
    summary2 = run_pipeline.process_week(_week(tmp_path), settings, client2, force=False,
                                          sample=None, flagged=flagged2)

    assert client2.calls == 3  # only images 2, 3, 4 -- image 1's success was correctly skipped
    assert summary2["classified"] == 3
    assert summary2["skipped"] == 1

    final_state = state.load_state(state_dir, "Week 99")
    assert len(final_state) == 4
    assert all(e["ok"] is True for e in final_state.values())
    assert flagged2.write() is None  # nothing needed review in run 2 -- everything confident

    status_col = _read_report_status_column(settings, "Week 99")
    assert status_col == ["CLASSIFIED", "CLASSIFIED", "CLASSIFIED", "CLASSIFIED"]


# ---------------------------------------------------------------------------
# Phase AE3: run-wide consecutive-503 circuit breaker. _ScriptedClient raises a scripted
# failure on specific (1-indexed) call numbers -- "503" = GeminiServiceUnavailableError (retries
# already exhausted inside the real client), "quota" = GeminiQuotaExhaustedError, "err" = an
# ordinary non-5xx failure, "kbi" = KeyboardInterrupt. Every other call succeeds.
# ---------------------------------------------------------------------------


class _ScriptedClient(vision_client.VisionClient):
    def __init__(self, script=None):
        self.calls = 0
        self._script = script or {}

    def classify(self, image_bytes: bytes) -> dict:
        self.calls += 1
        kind = self._script.get(self.calls)
        if kind == "503":
            raise vision_client.GeminiServiceUnavailableError("503 UNAVAILABLE (simulated)")
        if kind == "quota":
            raise vision_client.GeminiQuotaExhaustedError("quota exhausted (simulated)", 6843.0)
        if kind == "timeout":  # Phase AE5
            raise vision_client.GeminiRequestTimeoutError("request exceeded the 300s client timeout (simulated)")
        if kind == "err":
            raise RuntimeError("simulated non-5xx failure")
        if kind == "kbi":
            raise KeyboardInterrupt()
        return {
            "person_visible": "No", "activity_identified": "No activity observed",
            "activity_confidence": "High", "ai_suggested_location": "Main Deck",
            "location_confidence": "High",
        }


def test_breaker_trips_after_two_consecutive_unavailable_images(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(5)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])
    flagged = _flagged(tmp_path)

    client = _ScriptedClient({1: "503", 2: "503"})
    results = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client, force=False,
                                      sample=None, flagged=flagged)

    assert client.calls == 2  # image 3 onwards never requested
    assert results["stopped_unavailable"] == {"week": "Week 99", "consecutive_failures": 2,
                                              "server_errors": 2, "timeouts": 0}
    assert results["stopped_early"] is None  # quota-stop field untouched
    assert results["succeeded"] == []
    assert [f["exception_type"] for f in results["failed"]] == ["ServiceUnavailableStop"]

    saved = state.load_state(state_dir, "Week 99")
    assert len(saved) == 2 and all(e["ok"] is False for e in saved.values())
    rows = _read_flagged_rows(flagged.write())
    assert len(rows) == 2  # each failed image flagged exactly once
    assert all(r["failure_type"] == "API_FAILURE" and "503" in r["reason"] for r in rows)
    assert not (tmp_path / "output" / "Week 99" / "report.xlsx").exists()


def test_breaker_resets_after_a_success_and_does_not_trip(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client = _ScriptedClient({1: "503", 3: "503"})  # fail, ok, fail, ok
    results = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))

    assert client.calls == 4
    assert results["stopped_unavailable"] is None
    assert results["succeeded"][0]["failed"] == 2
    assert (tmp_path / "output" / "Week 99" / "report.xlsx").exists()


def test_non_5xx_failures_neither_count_nor_reset_the_breaker(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(5)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    # 503, ordinary error (neutral), 503 -> still 2 consecutive unavailable images -> trip
    client = _ScriptedClient({1: "503", 2: "err", 3: "503"})
    results = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 3
    assert results["stopped_unavailable"]["consecutive_failures"] == 2

    # ordinary errors alone never trip it
    client2 = _ScriptedClient({1: "err", 2: "err", 3: "err"})
    results2 = run_pipeline.run_weeks(_week_folders("Week 98"), settings, client2, force=False,
                                       sample=None, flagged=_flagged(tmp_path))
    assert client2.calls == 5 and results2["stopped_unavailable"] is None


def test_successful_processing_after_recovery_retries_only_failed_and_unprocessed(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(5)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    # Run 1: image 1 succeeds, images 2-3 unavailable -> trip; images 4-5 never attempted.
    client1 = _ScriptedClient({2: "503", 3: "503"})
    r1 = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client1, force=False,
                                 sample=None, flagged=_flagged(tmp_path))
    assert client1.calls == 3 and r1["stopped_unavailable"] is not None
    assert sorted(e["ok"] for e in state.load_state(state_dir, "Week 99").values()) == [False, False, True]

    # Run 2 (service recovered, no --force): only the 2 failed + 2 unprocessed are requested.
    client2 = _ScriptedClient()
    r2 = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client2, force=False,
                                 sample=None, flagged=logging_utils.FlaggedItemsWriter(tmp_path / "logs_run2"))
    assert client2.calls == 4
    assert r2["stopped_unavailable"] is None
    assert r2["succeeded"][0]["skipped"] == 1
    final_state = state.load_state(state_dir, "Week 99")
    assert len(final_state) == 5 and all(e["ok"] is True for e in final_state.values())
    assert _read_report_status_column(settings, "Week 99") == ["CLASSIFIED"] * 5


def test_breaker_trip_stops_subsequent_weeks_and_resume_completes_them(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(3)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])
    weeks = _week_folders("Week 1", "Week 2")

    client1 = _ScriptedClient({2: "503", 3: "503"})  # trips at the last image of Week 1
    r1 = run_pipeline.run_weeks(weeks, settings, client1, force=False, sample=None,
                                 flagged=_flagged(tmp_path))
    assert client1.calls == 3  # Week 2 never started
    assert r1["stopped_unavailable"]["week"] == "Week 1"
    assert state.load_state(state_dir, "Week 2") == {}

    client2 = _ScriptedClient()
    r2 = run_pipeline.run_weeks(weeks, settings, client2, force=False, sample=None,
                                 flagged=logging_utils.FlaggedItemsWriter(tmp_path / "logs_run2"))
    assert client2.calls == 2 + 3  # Week 1's 2 failed images + all 3 of Week 2
    assert [s["week"] for s in r2["succeeded"]] == ["Week 1", "Week 2"]
    for w in ("Week 1", "Week 2"):
        assert all(e["ok"] is True for e in state.load_state(state_dir, w).values())


def test_breaker_count_carries_across_week_boundaries(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(2)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    weeks = _week_folders("Week 1", "Week 2", "Week 3")

    # last image of Week 1 + first image of Week 2 -> 2 consecutive -> trip in Week 2
    client = _ScriptedClient({2: "503", 3: "503"})
    results = run_pipeline.run_weeks(weeks, settings, client, force=False, sample=None,
                                      flagged=_flagged(tmp_path))
    assert client.calls == 3
    assert [s["week"] for s in results["succeeded"]] == ["Week 1"]  # Week 1 finished (1 failure)
    assert results["stopped_unavailable"] == {"week": "Week 2", "consecutive_failures": 2,
                                              "server_errors": 2, "timeouts": 0}
    assert state.load_state(Path(settings["paths"]["state_dir"]), "Week 3") == {}


def test_cached_and_labelled_images_are_neutral_for_the_breaker(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])
    labelled = _make_image("labelled", official_location="Main Deck")
    imgs = [_make_image("a"), _make_image("b"), labelled, _make_image("c")]
    _patch_extract_week(monkeypatch, imgs)
    # pre-complete only image "b" so it is served from cache on the next run
    run_pipeline.process_week(_week(tmp_path), settings, _ScriptedClient(), force=False,
                               sample=2, flagged=_flagged(tmp_path))
    saved = state.load_state(state_dir, "Week 99")
    del saved[state.image_hash(imgs[0].image_bytes)]
    state.save_state(state_dir, "Week 99", saved)

    # a: 503 (count 1); b: cached (neutral); labelled: no AI call (neutral); c: 503 (count 2) -> trip
    client = _ScriptedClient({1: "503", 2: "503"})
    results = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 2
    assert results["stopped_unavailable"]["consecutive_failures"] == 2


def test_quota_exhaustion_takes_priority_over_a_pending_breaker_count(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client = _ScriptedClient({1: "503", 2: "quota"})
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 2
    assert results["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}
    assert results["stopped_unavailable"] is None
    assert [f["exception_type"] for f in results["failed"]] == ["GeminiQuotaExhaustedError"]


def test_keyboard_interrupt_after_an_unavailable_image_preserves_state_and_resumes(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    client1 = _ScriptedClient({2: "503", 3: "kbi"})
    with pytest.raises(KeyboardInterrupt):
        run_pipeline.run_weeks(_week_folders("Week 99"), settings, client1, force=False,
                                sample=None, flagged=_flagged(tmp_path))
    assert client1.calls == 3
    assert sorted(e["ok"] for e in state.load_state(state_dir, "Week 99").values()) == [False, True]

    client2 = _ScriptedClient()
    r2 = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client2, force=False,
                                 sample=None, flagged=logging_utils.FlaggedItemsWriter(tmp_path / "logs_run2"))
    assert client2.calls == 3  # failed image 2 + interrupted image 3 + unprocessed image 4
    assert all(e["ok"] is True for e in state.load_state(state_dir, "Week 99").values())
    assert r2["stopped_unavailable"] is None


def test_main_keyboard_interrupt_still_exits_130_through_real_run_weeks(tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path)
    settings.update({"provider": "gemini", "model": "unused-in-dry-run",
                     "rate_limit": {"max_retries": 4, "circuit_breaker_threshold": 2}})
    settings["paths"]["logs_dir"] = str(tmp_path / "logs")
    settings["paths"]["project_root"] = str(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders",
                        lambda root: _week_folders("Week 99"))

    def _interrupting_process_week(*a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(run_pipeline, "process_week", _interrupting_process_week)
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        with pytest.raises(SystemExit) as exc_info:
            run_pipeline.main()
    assert exc_info.value.code == 130
    assert any("Interrupted by user" in r.message for r in caplog.records)


def test_main_logs_service_unavailable_stop_and_returns_normally(tmp_path, monkeypatch, caplog):
    settings = _settings(tmp_path)
    settings.update({"provider": "gemini", "model": "unused-in-dry-run"})
    settings["paths"]["logs_dir"] = str(tmp_path / "logs")
    settings["paths"]["project_root"] = str(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: [])
    monkeypatch.setattr(run_pipeline, "run_weeks", lambda *a, **kw: {
        "succeeded": [], "failed": [{"week": "Week 1", "exception_type": "ServiceUnavailableStop", "error": "x"}],
        "stopped_early": None,
        "stopped_unavailable": {"week": "Week 1", "consecutive_failures": 2},
    })
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        run_pipeline.main()  # must return normally -- no SystemExit
    messages = [r.message for r in caplog.records]
    assert any("Gemini was unavailable" in m and "no --force needed" in m for m in messages)
    assert not any("quota was exhausted" in m for m in messages)


def test_dry_run_null_client_never_trips_the_breaker(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), settings,
                                      vision_client.NullVisionClient(), force=False, sample=None,
                                      flagged=_flagged(tmp_path))
    assert results["stopped_unavailable"] is None
    assert len(results["succeeded"]) == 2


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "2"])
def test_invalid_circuit_breaker_threshold_is_rejected(tmp_path, bad):
    settings = _settings(tmp_path)
    settings["rate_limit"] = {"circuit_breaker_threshold": bad}
    with pytest.raises(ValueError):
        run_pipeline.run_weeks(_week_folders("Week 1"), settings, _ScriptedClient(), force=False,
                                sample=None, flagged=_flagged(tmp_path))


def test_circuit_breaker_threshold_is_read_from_settings(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(5)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    settings["rate_limit"] = {"circuit_breaker_threshold": 3}
    client = _ScriptedClient({1: "503", 2: "503", 3: "503"})
    results = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 3
    assert results["stopped_unavailable"]["consecutive_failures"] == 3


# ---------------------------------------------------------------------------
# Phase AE5: request timeouts in the circuit breaker (R4) and clean configuration errors in
# main() (R1/R2). All offline: scripted clients, tmp_path, monkeypatched collaborators.
# ---------------------------------------------------------------------------


def test_timeout_only_breaker_trip_tracks_timeouts_separately(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)

    client = _ScriptedClient({1: "timeout", 2: "timeout"})
    results = run_pipeline.run_weeks(_week_folders("Week 99", "Week 100"), settings, client,
                                      force=False, sample=None, flagged=flagged)

    assert client.calls == 2  # image 3+, and Week 100, never requested
    assert results["stopped_unavailable"] == {"week": "Week 99", "consecutive_failures": 2,
                                              "server_errors": 0, "timeouts": 2}
    assert results["stopped_early"] is None
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    assert len(saved) == 2 and all(e["ok"] is False for e in saved.values())
    assert state.load_state(Path(settings["paths"]["state_dir"]), "Week 100") == {}
    rows = _read_flagged_rows(flagged.write())
    assert len(rows) == 2
    assert all(r["failure_type"] == "API_FAILURE" and "timeout" in r["reason"] for r in rows)
    assert not (tmp_path / "output" / "Week 99" / "report.xlsx").exists()


def test_mixed_timeout_and_503_trip_the_breaker_with_both_counts(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client = _ScriptedClient({1: "timeout", 2: "503"})
    results = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 2
    assert results["stopped_unavailable"] == {"week": "Week 99", "consecutive_failures": 2,
                                              "server_errors": 1, "timeouts": 1}


def test_success_resets_breaker_after_a_timeout(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client = _ScriptedClient({1: "timeout", 3: "timeout"})  # timeout, ok, timeout, ok
    results = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 4
    assert results["stopped_unavailable"] is None
    assert results["succeeded"][0]["failed"] == 2


def test_breaker_counts_reset_fully_after_success():
    breaker = run_pipeline.ServiceUnavailableBreaker(threshold=3)

    def r(**kw):
        base = dict(activity_identified="x", activity_confidence="High",
                    ai_suggested_location="Main Deck", location_confidence="High")
        return classifier.ClassificationResult(**base, **kw)

    assert breaker.record(r(error="e", service_unavailable=True)) is False
    assert breaker.record(r(error="e", request_timeout=True)) is False
    assert (breaker.consecutive, breaker.server_errors, breaker.timeouts) == (2, 1, 1)
    assert breaker.record(r(error="e")) is False  # ordinary failure: neutral
    assert (breaker.consecutive, breaker.server_errors, breaker.timeouts) == (2, 1, 1)
    assert breaker.record(r()) is False  # success resets everything
    assert (breaker.consecutive, breaker.server_errors, breaker.timeouts) == (0, 0, 0)


def test_timed_out_images_recover_on_next_run_without_force(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    state_dir = Path(settings["paths"]["state_dir"])

    client1 = _ScriptedClient({2: "timeout", 3: "timeout"})
    r1 = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client1, force=False,
                                 sample=None, flagged=_flagged(tmp_path))
    assert client1.calls == 3 and r1["stopped_unavailable"]["timeouts"] == 2
    assert sorted(e["ok"] for e in state.load_state(state_dir, "Week 99").values()) == [False, False, True]

    client2 = _ScriptedClient()
    r2 = run_pipeline.run_weeks(_week_folders("Week 99"), settings, client2, force=False,
                                 sample=None, flagged=logging_utils.FlaggedItemsWriter(tmp_path / "logs_run2"))
    assert client2.calls == 3  # 2 timed-out + 1 never attempted; the success is skipped
    assert r2["stopped_unavailable"] is None and r2["succeeded"][0]["skipped"] == 1
    assert all(e["ok"] is True for e in state.load_state(state_dir, "Week 99").values())
    assert _read_report_status_column(settings, "Week 99") == ["CLASSIFIED"] * 4


def test_quota_keeps_priority_after_a_timeout(tmp_path, monkeypatch):
    images = [_make_image(str(i)) for i in range(4)]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)

    client = _ScriptedClient({1: "timeout", 2: "quota"})
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 2
    assert results["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}
    assert results["stopped_unavailable"] is None


def test_main_stop_message_distinguishes_server_errors_from_timeouts(tmp_path, monkeypatch, caplog):
    settings = _main_settings(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: [])
    monkeypatch.setattr(run_pipeline, "run_weeks", lambda *a, **kw: {
        "succeeded": [], "failed": [{"week": "Week 1", "exception_type": "ServiceUnavailableStop", "error": "x"}],
        "stopped_early": None,
        "stopped_unavailable": {"week": "Week 1", "consecutive_failures": 2, "server_errors": 0, "timeouts": 2},
    })
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        run_pipeline.main()
    messages = [r.message for r in caplog.records]
    assert any("Gemini was unavailable" in m and "0 server error(s)" in m and "2 request timeout(s)" in m
               for m in messages)


def _main_settings(tmp_path: Path, rate_limit: dict | None = None) -> dict:
    settings = _settings(tmp_path)
    settings.update({"provider": "gemini", "model": "gemini-3.8-flash"})
    settings["paths"]["logs_dir"] = str(tmp_path / "logs")
    settings["paths"]["project_root"] = str(tmp_path)
    if rate_limit is not None:
        settings["rate_limit"] = rate_limit
    return settings


def _run_main_expecting_config_error(tmp_path, monkeypatch, caplog, settings, argv):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1"))

    def _must_not_run(*a, **kw):
        raise AssertionError("run_weeks must not be reached on a configuration error")

    monkeypatch.setattr(run_pipeline, "run_weeks", _must_not_run)
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        with pytest.raises(SystemExit) as exc_info:
            run_pipeline.main()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert exc_info.value.code == 1
    assert len(errors) == 1 and errors[0].message.startswith("Configuration error: ")
    assert errors[0].exc_info is None  # logged cleanly, no traceback attached
    assert "no API requests were made" in errors[0].message
    assert not list((tmp_path / "logs").glob("flagged_items_*.csv"))
    return errors[0].message


@pytest.mark.parametrize("rate_limit, fragment", [
    ({"max_retries": -1}, "rate_limit.max_retries"),
    ({"request_timeout_seconds": 10}, "rate_limit.request_timeout_seconds"),
    ({"circuit_breaker_threshold": 0}, "circuit_breaker_threshold"),
])
@pytest.mark.parametrize("argv", [["--dry-run"], []])
def test_invalid_configuration_is_a_clean_error_with_exit_code_1(tmp_path, monkeypatch, caplog,
                                                                  rate_limit, fragment, argv):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-offline-test")
    message = _run_main_expecting_config_error(
        tmp_path, monkeypatch, caplog, _main_settings(tmp_path, rate_limit), argv)
    assert fragment in message


def test_missing_api_key_is_a_clean_configuration_error(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    message = _run_main_expecting_config_error(tmp_path, monkeypatch, caplog, _main_settings(tmp_path), [])
    assert "No Gemini API key found" in message


def test_main_keyboard_interrupt_still_exits_130_with_timeout_configured(tmp_path, monkeypatch, caplog):
    settings = _main_settings(tmp_path, {"request_timeout_seconds": 300, "circuit_breaker_threshold": 2})
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1"))

    def _interrupt(*a, **kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(run_pipeline, "process_week", _interrupt)
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        with pytest.raises(SystemExit) as exc_info:
            run_pipeline.main()
    assert exc_info.value.code == 130
    assert not any(r.message.startswith("Configuration error") for r in caplog.records)


def test_dry_run_main_never_constructs_or_calls_gemini(tmp_path, monkeypatch):
    settings = _main_settings(tmp_path, {"request_timeout_seconds": 300})
    _patch_extract_week(monkeypatch, [_make_image("0"), _make_image("1")])
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1"))

    def _forbidden(*a, **kw):
        raise AssertionError("dry-run must never construct a Gemini client")

    monkeypatch.setattr(vision_client, "GeminiVisionClient", _forbidden)
    from google import genai
    monkeypatch.setattr(genai, "Client", _forbidden)
    run_pipeline.main()  # completes normally
    # P0-1: dry-run output is isolated under dry_run/ with a prefixed filename
    assert (tmp_path / "dry_run" / "output" / "Week 1" / "DRY RUN - report.xlsx").exists()
    assert not (tmp_path / "output").exists()


# ---------------------------------------------------------------------------
# Phase AE9: operational completion reporting -- image-level outcomes (classified / visual
# review / API failure / unprocessed / already labelled) reported separately from the unchanged
# week-level success semantics. All offline: scripted clients, tmp_path, monkeypatched extraction.
# ---------------------------------------------------------------------------


class _LowConfidenceOnClient(_ScriptedClient):
    """Like _ScriptedClient, but answers successfully with Low location confidence on the given
    call numbers -- a completed request that still needs human (VISUAL_REVIEW) review."""

    def __init__(self, low_on: set[int], script=None):
        super().__init__(script)
        self._low_on = low_on

    def classify(self, image_bytes: bytes) -> dict:
        raw = super().classify(image_bytes)
        if self.calls in self._low_on:
            raw = {**raw, "location_confidence": "Low"}
        return raw


def _outcomes(**counts) -> dict:
    return {**run_pipeline._empty_outcomes(), **counts}


def _run_main_with_client(tmp_path, monkeypatch, caplog, client, week_names=("Week 1",)):
    """Drives the real main() -> run_weeks() -> process_week() with a scripted client injected
    in place of build_vision_client(), so no key is read and no request leaves the process.
    Returns (records, exit_code) -- exit_code is None when main() returns normally."""
    settings = _main_settings(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders(*week_names))
    monkeypatch.setattr(run_pipeline.vision_client, "build_vision_client", lambda *a, **kw: client)
    exit_code = None
    with caplog.at_level(logging.INFO, logger="pipeline"):
        try:
            run_pipeline.main()
        except SystemExit as exc:
            exit_code = exc.code
    return [r for r in caplog.records if r.name == "pipeline"], exit_code


def _msgs(records, level=None):
    return [r.message for r in records if level is None or r.levelno == level]


def test_ae9_all_images_classified_reports_clean_success(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _ScriptedClient())
    assert code is None  # unchanged: normal return
    assert any(m.startswith("Run complete. 1 week(s) succeeded") for m in _msgs(records, logging.INFO))
    assert any("Image outcomes: 3 classified, 0 need visual review, 0 API failure(s), 0 unprocessed"
               in m for m in _msgs(records))
    assert not any("API FAILURES" in m or "NOT classified" in m for m in _msgs(records))
    assert not [r for r in records if r.levelno >= logging.WARNING]


def test_ae9_classified_mixed_with_visual_review_and_labelled(tmp_path, monkeypatch):
    images = [_make_image("0"), _make_image("1"), _make_image("L", official_location="Main Deck"),
              _make_image("2")]
    _patch_extract_week(monkeypatch, images)
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path),
                                      _LowConfidenceOnClient({2}), force=False, sample=None,
                                      flagged=_flagged(tmp_path))
    assert results["images"] == _outcomes(classified=2, visual_review=1, labelled=1)
    assert results["succeeded"][0]["outcomes"] == results["images"]
    # existing request-oriented week counters are unchanged in meaning
    assert results["succeeded"][0]["classified"] == 3 and results["succeeded"][0]["failed"] == 0
    assert results["weeks_not_attempted"] == []


def test_ae9_visual_review_alone_is_not_an_api_failure_warning(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image("0"), _make_image("1")])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _LowConfidenceOnClient({1, 2}))
    assert code is None
    assert any("0 classified, 2 need visual review, 0 API failure(s)" in m for m in _msgs(records))
    assert not any("NOT classified" in m or "API FAILURES" in m for m in _msgs(records))


def test_ae9_api_failure_with_week_completed_is_warned_not_counted_as_success(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _ScriptedClient({2: "err"}))
    assert code is None  # deliberate: exit code unchanged (see _log_image_outcomes docstring)
    warnings = _msgs(records, logging.WARNING)
    # week-level semantics unchanged: the week still "succeeded" -- but never unqualified
    assert any(m.startswith("Run complete WITH API FAILURES. 1 week(s) succeeded") for m in warnings)
    assert any("Image outcomes: 2 classified, 0 need visual review, 1 API failure(s)" in m
               for m in _msgs(records))
    assert any("1 image(s) failed due to API errors and were NOT classified" in m
               and "no --force needed" in m for m in warnings)
    assert not any("No image was successfully classified" in m for m in warnings)
    assert not any(m.startswith("Run complete. ") for m in _msgs(records))


def test_ae9_ae8_shape_single_503_image_reports_zero_successes(tmp_path, monkeypatch, caplog):
    # The exact AE8 shape: one image, 5xx after every retry, breaker threshold (2) not reached,
    # so the week completes -- previously logged as a plain "Run complete. 1 week(s) succeeded".
    _patch_extract_week(monkeypatch, [_make_image("0")])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _ScriptedClient({1: "503"}))
    assert code is None
    warnings = _msgs(records, logging.WARNING)
    assert any(m.startswith("Run complete WITH API FAILURES") for m in warnings)
    assert any("0 classified, 0 need visual review, 1 API failure(s)" in m for m in _msgs(records))
    assert any("No image was successfully classified by the AI in this run" in m for m in warnings)


def test_ae9_quota_stop_reports_failure_unprocessed_and_unattempted_weeks(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    client = _ScriptedClient({2: "quota"})
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                      force=False, sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 2  # quota stop behavior unchanged
    assert results["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}
    assert [f["week"] for f in results["failed"]] == ["Week 1"]
    assert results["images"] == _outcomes(classified=1, api_failure=1, unprocessed=2)
    assert results["weeks_not_attempted"] == ["Week 2"]

    records, code = _run_main_with_client(tmp_path / "m", monkeypatch, caplog, _ScriptedClient({2: "quota"}),
                                          week_names=("Week 1", "Week 2"))
    assert code is None  # quota stop still returns normally
    msgs = _msgs(records)
    assert any("quota was exhausted" in m for m in msgs)
    assert any("1 classified, 0 need visual review, 1 API failure(s), 2 unprocessed (early stop)" in m
               for m in msgs)
    assert any("1 week(s) not attempted" in m and "Week 2" in m for m in _msgs(records, logging.WARNING))


def test_ae9_circuit_breaker_stop_reports_failures_and_unprocessed(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(5)])
    client = _ScriptedClient({1: "503", 2: "timeout"})
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2", "Week 3"), _settings(tmp_path),
                                      client, force=False, sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 2  # breaker behavior unchanged
    assert results["stopped_unavailable"] == {"week": "Week 1", "consecutive_failures": 2,
                                              "server_errors": 1, "timeouts": 1}
    assert results["images"] == _outcomes(api_failure=2, unprocessed=3)
    assert results["weeks_not_attempted"] == ["Week 2", "Week 3"]

    records, code = _run_main_with_client(tmp_path / "m", monkeypatch, caplog,
                                          _ScriptedClient({1: "503", 2: "503"}),
                                          week_names=("Week 1", "Week 2"))
    assert code is None
    msgs = _msgs(records)
    assert any("Gemini was unavailable" in m for m in msgs)
    assert any("0 classified, 0 need visual review, 2 API failure(s), 3 unprocessed" in m for m in msgs)
    assert any("No image was successfully classified" in m for m in _msgs(records, logging.WARNING))


def test_ae9_breaker_stop_in_second_week_keeps_first_week_outcomes(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    # Week 1: 3 successes; Week 2: 503 on its 1st and 2nd image -> breaker trips at image 2 of 3.
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path),
                                      _ScriptedClient({4: "503", 5: "503"}), force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert [s["week"] for s in results["succeeded"]] == ["Week 1"]
    assert results["images"] == _outcomes(classified=3, api_failure=2, unprocessed=1)
    assert results["weeks_not_attempted"] == []


def test_ae9_keyboard_interrupt_still_exits_130_without_outcome_summary(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _ScriptedClient({2: "kbi"}))
    assert code == 130
    assert any("Interrupted by user" in m for m in _msgs(records))
    assert not any("Image outcomes" in m or "Run complete" in m for m in _msgs(records))


def test_ae9_unprocessed_images_are_completed_on_resume_without_force(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    settings = _settings(tmp_path)
    weeks = _week_folders("Week 1", "Week 2")
    first = run_pipeline.run_weeks(weeks, settings, _ScriptedClient({2: "quota"}), force=False,
                                   sample=None, flagged=_flagged(tmp_path))
    assert first["images"] == _outcomes(classified=1, api_failure=1, unprocessed=2)

    client = _ScriptedClient()
    second = run_pipeline.run_weeks(weeks, settings, client, force=False, sample=None,
                                    flagged=_flagged(tmp_path))
    assert client.calls == 3 + 4  # Week 1: failed + 2 unprocessed; Week 2: all 4 -- no --force
    assert second["images"] == _outcomes(classified=8)  # Week 1 includes 1 cached success
    assert second["weeks_not_attempted"] == [] and second["failed"] == []


def test_ae9_generic_week_failure_is_not_an_early_stop(monkeypatch):
    weeks = _week_folders("Week 1", "Week 2")

    def fake_process_week(week, settings, client, force, sample, flagged, breaker=None):
        if week.name == "Week 1":
            raise RuntimeError("simulated extraction failure")
        return {**_fake_summary(week.name), "outcomes": _outcomes(classified=1)}

    monkeypatch.setattr(run_pipeline, "process_week", fake_process_week)
    results = run_pipeline.run_weeks(weeks, {}, object(), force=False, sample=None, flagged=object())
    assert [f["week"] for f in results["failed"]] == ["Week 1"]
    assert results["weeks_not_attempted"] == []
    assert results["images"] == _outcomes(classified=1)


def test_ae9_main_tolerates_results_without_image_tallies(tmp_path, monkeypatch, caplog):
    settings = _main_settings(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: [])
    monkeypatch.setattr(run_pipeline, "run_weeks", lambda *a, **kw: {
        "succeeded": [_fake_summary("Week 1")], "failed": [], "stopped_early": None,
    })
    with caplog.at_level(logging.INFO, logger="pipeline"):
        run_pipeline.main()
    msgs = [r.message for r in caplog.records]
    assert any(m.startswith("Run complete. 1 week(s) succeeded") for m in msgs)
    assert not any("Image outcomes" in m for m in msgs)


# ---------------------------------------------------------------------------
# Phase P0-1: dry-run isolation. A dry run must never write to the real state/output/logs, must
# never make a network call, must mark its placeholders unmistakably, and must leave every
# not-yet-classified image eligible for the next normal run. All offline, tmp_path only.
# ---------------------------------------------------------------------------


def _p01_images():
    # 3 AI-assessed + 1 already-labelled image
    return [_make_image("p0"), _make_image("p1"), _make_image("pL", official_location="Main Deck"),
            _make_image("p2")]


def _seed_production(tmp_path: Path) -> None:
    """Pre-existing 'production' artifacts the dry run must leave byte-for-byte untouched,
    including one previously failed (ok:false) image that must stay retryable."""
    failed_hash = state.image_hash(_p01_images()[0].image_bytes)
    state.save_state(tmp_path / ".state", "Week 1", {failed_hash: {
        "ok": False, "activity_identified": "Needs Review", "activity_confidence": "Needs Review",
        "ai_suggested_location": "Needs Review", "location_confidence": "Needs Review",
        "person_visible": "Uncertain"}})
    out = tmp_path / "output" / "Week 1"
    (out / "Extracted Images").mkdir(parents=True)
    (out / "report.xlsx").write_bytes(b"PRODUCTION REPORT")
    (out / "manifest.csv").write_text("PRODUCTION MANIFEST", encoding="utf-8")
    (out / "Extracted Images" / "photo.jpg").write_bytes(b"PRODUCTION PHOTO")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "flagged_items_20000101_000000.csv").write_text("PRODUCTION CSV", encoding="utf-8")
    (tmp_path / "logs" / "run_20000101_000000.log").write_text("PRODUCTION LOG", encoding="utf-8")


def _snapshot(tmp_path: Path) -> dict:
    return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for d in (".state", "output", "logs") for p in sorted((tmp_path / d).rglob("*")) if p.is_file()}


def _run_dry_main(tmp_path, monkeypatch):
    settings = _main_settings(tmp_path)
    _patch_extract_week(monkeypatch, _p01_images())
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1"))

    def _forbidden(*a, **kw):
        raise AssertionError("dry-run must never construct a real client or open a connection")

    import socket
    monkeypatch.setattr(vision_client, "GeminiVisionClient", _forbidden)
    monkeypatch.setattr(vision_client, "GroqVisionClient", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    run_pipeline.main()
    return settings


def _status_column(report_path: Path) -> list[str]:
    wb = openpyxl.load_workbook(report_path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    col = header.index("Classification Status") + 1
    values = [ws.cell(r, col).value for r in range(2, ws.max_row + 1)]
    wb.close()
    return values


def test_p01_dry_run_leaves_real_state_reports_manifests_csv_and_photos_unchanged(tmp_path, monkeypatch):
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    _run_dry_main(tmp_path, monkeypatch)  # also proves no client/socket was ever touched
    assert _snapshot(tmp_path) == before  # no file changed, added or removed in real locations


def test_p01_dry_run_outputs_are_separated_and_clearly_marked(tmp_path, monkeypatch, caplog):
    _seed_production(tmp_path)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        _run_dry_main(tmp_path, monkeypatch)
    dry = tmp_path / "dry_run"
    report = dry / "output" / "Week 1" / "DRY RUN - report.xlsx"
    assert report.exists() and (dry / "output" / "Week 1" / "manifest.csv").exists()
    # AI-assessed rows are DRY_RUN, never CLASSIFIED/VISUAL_REVIEW; the labelled row is unaffected
    assert _status_column(report) == ["DRY_RUN", "DRY_RUN", "CLASSIFIED", "DRY_RUN"]
    flagged = list((dry / "logs").glob("flagged_items_*.csv"))
    assert len(flagged) == 1
    rows = _read_flagged_rows(flagged[0])
    assert len(rows) == 3 and all(r["failure_type"] == "DRY_RUN" and "NOT a classification" in r["reason"]
                                  for r in rows)
    saved = state.load_state(dry / ".state", "Week 1")
    assert len(saved) == 3 and all(e["ok"] is False for e in saved.values())
    msgs = [r.message for r in caplog.records]
    assert any(m.startswith("DRY RUN: no AI requests will be made") and str(dry) in m for m in msgs)
    assert any("3 image(s) are DRY RUN placeholders" in m for m in msgs)
    assert any(m.startswith("Image outcomes: 0 classified, 0 need visual review, 0 API failure(s)")
               for m in msgs)  # placeholders never counted as classified or visual review


def test_p01_normal_run_after_dry_run_still_classifies_every_unclassified_image(tmp_path, monkeypatch, caplog):
    _seed_production(tmp_path)
    _run_dry_main(tmp_path, monkeypatch)
    monkeypatch.undo()  # restore real clients/sockets/argv before the normal run
    _patch_extract_week(monkeypatch, _p01_images())
    client = _ScriptedClient()
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, client)
    assert code is None
    assert client.calls == 3  # all 3 AI-assessed images, incl. the previously failed one
    saved = state.load_state(tmp_path / ".state", "Week 1")
    assert len(saved) == 3 and all(e["ok"] is True for e in saved.values())
    assert _status_column(tmp_path / "output" / "Week 1" / "report.xlsx") == ["CLASSIFIED"] * 4


def test_p01_null_client_never_stores_ok_true_even_in_a_real_state_dir(tmp_path, monkeypatch):
    # Defence in depth behind main()'s redirection: even if process_week() were handed real
    # paths with a NullVisionClient, nothing it saves is trusted as done.
    _patch_extract_week(monkeypatch, _p01_images())
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)
    summary = run_pipeline.process_week(_week(tmp_path), settings, vision_client.NullVisionClient(),
                                         force=False, sample=None, flagged=flagged)
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    assert len(saved) == 3 and all(e["ok"] is False for e in saved.values())
    assert summary["outcomes"] == _outcomes(dry_run=3, labelled=1)
    assert all(r["failure_type"] == "DRY_RUN" for r in _read_flagged_rows(flagged.path))

    client = _ScriptedClient()
    run_pipeline.process_week(_week(tmp_path), settings, client, force=False, sample=None,
                              flagged=_flagged(tmp_path))
    assert client.calls == 3


def test_p01_dry_run_settings_redirect_without_mutating_and_reject_overlap(tmp_path):
    settings = _main_settings(tmp_path)
    original = {k: dict(v) if isinstance(v, dict) else v for k, v in settings.items()}
    dry = run_pipeline._dry_run_settings(settings)
    assert settings == original  # real settings untouched
    root = tmp_path / "dry_run"
    assert dry["paths"]["automation_root"] == str(root)
    assert dry["paths"]["state_dir"] == str(root / ".state")
    assert dry["paths"]["logs_dir"] == str(root / "logs")
    assert dry["report"]["output_filename"] == "DRY RUN - report.xlsx"

    overlapping = _main_settings(tmp_path)
    overlapping["paths"]["state_dir"] = str(root / ".state")
    with pytest.raises(ValueError, match="overlap"):
        run_pipeline._dry_run_settings(overlapping)


def test_p01_normal_run_paths_and_statuses_unchanged(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, _p01_images())
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _LowConfidenceOnClient({2}))
    assert code is None
    assert not (tmp_path / "dry_run").exists()
    assert _status_column(tmp_path / "output" / "Week 1" / "report.xlsx") == [
        "CLASSIFIED", "VISUAL_REVIEW", "CLASSIFIED", "CLASSIFIED"]
    assert not any("DRY RUN" in r.message for r in records)


# ---------------------------------------------------------------------------
# Phase P0-2: honest activity identification. Photos with an official location are never sent
# to the AI, so their Activity Identified must say "Not Assessed (Official Location)" -- never
# "No activity observed". AI-assessed photos keep their real activity/status behaviour.
# ---------------------------------------------------------------------------

REAL_ACTIVITY = "Crew member coiling a mooring line"
EXPECTED_HEADERS = [
    "Serial Number", "Image Filename", "Report Date", "Original Excel Filename", "Worksheet Name",
    "Official Location", "AI Suggested Location", "Extracted Photograph", "Activity Identified",
    "Location Confidence", "Classification Status",
]


class _SpecificActivityClient(_LowConfidenceOnClient):
    """Successful answers report a visible person doing a specific, observable task."""

    def classify(self, image_bytes: bytes) -> dict:
        raw = super().classify(image_bytes)
        return {**raw, "person_visible": "Yes", "activity_identified": REAL_ACTIVITY}


def _p02_images():
    # AI (confident), labelled, AI (low confidence -> VISUAL_REVIEW), AI (API error), labelled
    return [_make_image("q0"), _make_image("qL1", official_location="CARGO HOLD NO. 1"),
            _make_image("q1"), _make_image("q2"), _make_image("qL2", official_location="BRIDGE DECK")]


def _p02_client():
    # AI calls are 1, 2, 3 (labelled images are never sent): 2 = low confidence, 3 = API error
    return _SpecificActivityClient({2}, script={3: "err"})


def _report_table(report_path: Path) -> tuple[list, list[dict]]:
    wb = openpyxl.load_workbook(report_path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    rows = [dict(zip(header, [ws.cell(r, c).value for c in range(1, len(header) + 1)]))
            for r in range(2, ws.max_row + 1)]
    wb.close()
    return header, rows


def _manifest_rows(output_dir: Path) -> list[dict]:
    with (output_dir / "manifest.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_p02_labelled_photos_are_not_assessed_and_never_sent_to_ai(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p02_images())
    settings = _settings(tmp_path)
    client = _p02_client()
    flagged = _flagged(tmp_path)
    run_pipeline.process_week(_week(tmp_path), settings, client, force=False, sample=None, flagged=flagged)

    assert client.calls == 3  # only the 3 unlabelled photos
    _, rows = _report_table(tmp_path / "output" / "Week 99" / "report.xlsx")
    labelled = [r for r in rows if r["Official Location"] != "Not Labelled"]
    assert [r["Official Location"] for r in labelled] == ["CARGO HOLD NO. 1", "BRIDGE DECK"]
    for r in labelled:
        assert r["Activity Identified"] == "Not Assessed (Official Location)"
        assert r["Activity Identified"] != classifier.NO_ACTIVITY
        assert r["AI Suggested Location"] is None and r["Location Confidence"] is None
        assert r["Classification Status"] == "CLASSIFIED"  # unchanged official-location status
    # labelled photos never reach state or the flagged CSV
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    labelled_hashes = {state.image_hash(i.image_bytes) for i in _p02_images() if i.official_location != "Not Labelled"}
    assert len(saved) == 3 and not labelled_hashes & saved.keys()
    assert {r["image_filename"] for r in _read_flagged_rows(flagged.path)} == {
        rows[2]["Image Filename"], rows[3]["Image Filename"]}


def test_p02_ai_assessed_photos_keep_real_activity_and_statuses(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p02_images())
    settings = _settings(tmp_path)
    run_pipeline.process_week(_week(tmp_path), settings, _p02_client(), force=False, sample=None,
                              flagged=_flagged(tmp_path))
    _, rows = _report_table(tmp_path / "output" / "Week 99" / "report.xlsx")
    assert [(r["Activity Identified"], r["Classification Status"]) for r in rows] == [
        (REAL_ACTIVITY, "CLASSIFIED"),
        ("Not Assessed (Official Location)", "CLASSIFIED"),
        (REAL_ACTIVITY, "VISUAL_REVIEW"),          # uncertain location -> still review
        (classifier.NEEDS_REVIEW, "API_FAILURE"),  # failed request -> Needs Review, API_FAILURE
        ("Not Assessed (Official Location)", "CLASSIFIED"),
    ]
    assert rows[2]["Location Confidence"] == "Low"


def test_p02_report_and_manifest_activity_values_agree_and_columns_unchanged(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p02_images())
    settings = _settings(tmp_path)
    run_pipeline.process_week(_week(tmp_path), settings, _p02_client(), force=False, sample=None,
                              flagged=_flagged(tmp_path))
    out = tmp_path / "output" / "Week 99"
    header, rows = _report_table(out / "report.xlsx")
    assert header == EXPECTED_HEADERS  # 11 columns, original order
    manifest = _manifest_rows(out)
    assert [m["image_filename"] for m in manifest] == [r["Image Filename"] for r in rows]
    assert [m["activity_identified"] for m in manifest] == [r["Activity Identified"] for r in rows]
    assert [m["official_location"] for m in manifest] == [r["Official Location"] for r in rows]
    assert list(manifest[0].keys()) == [
        "serial_number", "image_filename", "report_date", "original_excel_filename", "worksheet_name",
        "official_location", "ai_suggested_location", "activity_identified", "location_confidence"]


def test_p02_dry_run_keeps_dry_run_status_and_labelled_not_assessed(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p02_images())
    settings = _settings(tmp_path)
    run_pipeline.process_week(_week(tmp_path), settings, vision_client.NullVisionClient(), force=False,
                              sample=None, flagged=_flagged(tmp_path))
    _, rows = _report_table(tmp_path / "output" / "Week 99" / "report.xlsx")
    assert [r["Classification Status"] for r in rows] == [
        "DRY_RUN", "CLASSIFIED", "DRY_RUN", "DRY_RUN", "CLASSIFIED"]
    assert [r["Activity Identified"] for r in rows if r["Classification Status"] == "CLASSIFIED"] == [
        "Not Assessed (Official Location)"] * 2


def test_p02_resume_keeps_activity_values_and_only_retries_the_failed_image(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p02_images())
    settings = _settings(tmp_path)
    run_pipeline.process_week(_week(tmp_path), settings, _p02_client(), force=False, sample=None,
                              flagged=_flagged(tmp_path))
    client = _SpecificActivityClient(set())
    summary = run_pipeline.process_week(_week(tmp_path), settings, client, force=False, sample=None,
                                        flagged=_flagged(tmp_path))
    assert client.calls == 1 and summary["skipped"] == 2  # only the API_FAILURE image retried
    _, rows = _report_table(tmp_path / "output" / "Week 99" / "report.xlsx")
    assert [r["Activity Identified"] for r in rows] == [
        REAL_ACTIVITY, "Not Assessed (Official Location)", REAL_ACTIVITY, REAL_ACTIVITY,
        "Not Assessed (Official Location)"]
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 99")
    assert len(saved) == 3 and all(e["ok"] is True for e in saved.values())


def test_p02_all_labelled_week_makes_no_ai_requests(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"L{i}", official_location="POOP DECK AND MOORING WINCH")
                                      for i in range(3)])
    settings = _settings(tmp_path)
    client = _ScriptedClient()
    summary = run_pipeline.process_week(_week(tmp_path), settings, client, force=False, sample=None,
                                        flagged=_flagged(tmp_path))
    assert client.calls == 0
    assert summary["outcomes"] == _outcomes(labelled=3)
    assert state.load_state(Path(settings["paths"]["state_dir"]), "Week 99") == {}
    out = tmp_path / "output" / "Week 99"
    assert {m["activity_identified"] for m in _manifest_rows(out)} == {"Not Assessed (Official Location)"}


# ---------------------------------------------------------------------------
# Phase P1-1: non-daily HTTP 429 handling at the pipeline level. The client-side wait/retry is
# tested in test_vision_client_offline.py; here a scripted client raises the final
# GeminiRateLimitedError the real client would raise once its bounded handling is used up.
# ---------------------------------------------------------------------------


class _RateLimitScriptedClient(_ScriptedClient):
    """_ScriptedClient plus "rpm" (confirmed per-minute, 23s delay) and "amb" (ambiguous 429)."""

    def classify(self, image_bytes: bytes) -> dict:
        kind = self._script.get(self.calls + 1)
        if kind == "rpm":
            self.calls += 1
            raise vision_client.GeminiRateLimitedError("429 RESOURCE_EXHAUSTED per minute (simulated)",
                                                       23.0, "per_minute")
        if kind == "amb":
            self.calls += 1
            raise vision_client.GeminiRateLimitedError("429 RESOURCE_EXHAUSTED (simulated)", None, "ambiguous")
        return super().classify(image_bytes)


def test_p11_classify_image_marks_rate_limited_results():
    result = classifier.classify_image(_RateLimitScriptedClient({1: "rpm"}), b"x")
    assert result.rate_limited and result.rate_limit_kind == "per_minute"
    assert result.retry_delay_seconds == 23.0 and result.error.startswith("GeminiRateLimitedError")
    assert not (result.quota_exhausted or result.service_unavailable or result.request_timeout)
    ok = classifier.classify_image(_RateLimitScriptedClient(), b"x")
    assert not ok.rate_limited and ok.rate_limit_kind is None


@pytest.mark.parametrize("kind, delay", [("rpm", 23.0), ("amb", None)])
def test_p11_rate_limit_stops_run_like_quota_but_reported_distinctly(tmp_path, monkeypatch, kind, delay):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    settings = _settings(tmp_path)
    flagged = _flagged(tmp_path)
    client = _RateLimitScriptedClient({2: kind})
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), settings, client,
                                      force=False, sample=None, flagged=flagged)
    assert client.calls == 2  # no further requests after the 429
    expected_kind = "per_minute" if kind == "rpm" else "ambiguous"
    assert results["stopped_rate_limited"] == {"week": "Week 1", "kind": expected_kind,
                                               "retry_delay_seconds": delay}
    assert results["stopped_early"] is None and results["stopped_unavailable"] is None
    assert [f["exception_type"] for f in results["failed"]] == ["GeminiRateLimitedError"]
    assert results["images"] == _outcomes(classified=1, api_failure=1, unprocessed=2)
    assert results["weeks_not_attempted"] == ["Week 2"]
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 1")
    assert sorted(e["ok"] for e in saved.values()) == [False, True]
    rows = _read_flagged_rows(flagged.write())
    assert len(rows) == 1 and rows[0]["failure_type"] == "API_FAILURE"
    assert "GeminiRateLimitedError" in rows[0]["reason"]
    assert not (tmp_path / "output" / "Week 1" / "report.xlsx").exists()  # stopped week, as quota


def test_p11_state_recovery_after_rate_limit_stop_needs_no_force(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    settings = _settings(tmp_path)
    weeks = _week_folders("Week 1", "Week 2")
    run_pipeline.run_weeks(weeks, settings, _RateLimitScriptedClient({2: "rpm"}), force=False,
                           sample=None, flagged=_flagged(tmp_path))
    client = _RateLimitScriptedClient()
    second = run_pipeline.run_weeks(weeks, settings, client, force=False, sample=None,
                                    flagged=_flagged(tmp_path))
    assert client.calls == 3 + 4  # failed + 2 unprocessed in Week 1, all of Week 2
    assert second["failed"] == [] and second["stopped_rate_limited"] is None
    assert second["images"] == _outcomes(classified=8)
    for week in ("Week 1", "Week 2"):
        assert all(e["ok"] for e in state.load_state(Path(settings["paths"]["state_dir"]), week).values())


def test_p11_rate_limit_is_neutral_for_the_circuit_breaker(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    settings = _settings(tmp_path)
    # 503 then ambiguous 429 with threshold 2: the 429 must stop the run as a rate limit,
    # never be counted as a second consecutive "unavailable" image.
    results = run_pipeline.run_weeks(_week_folders("Week 1"), settings, _RateLimitScriptedClient({1: "503", 2: "amb"}),
                                      force=False, sample=None, flagged=_flagged(tmp_path))
    assert results["stopped_unavailable"] is None
    assert results["stopped_rate_limited"]["kind"] == "ambiguous"
    breaker = run_pipeline.ServiceUnavailableBreaker(2)
    assert breaker.record(classifier.classify_image(_RateLimitScriptedClient({1: "503"}), b"x")) is False
    assert breaker.record(classifier.classify_image(_RateLimitScriptedClient({1: "rpm"}), b"x")) is False
    assert breaker.consecutive == 1  # neither counted nor reset


def test_p11_daily_quota_keeps_priority_and_existing_breaker_still_trips(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    settings = _settings(tmp_path)
    quota = run_pipeline.run_weeks(_week_folders("Week 1"), settings, _RateLimitScriptedClient({1: "quota"}),
                                    force=False, sample=None, flagged=_flagged(tmp_path))
    assert quota["stopped_early"]["week"] == "Week 1" and quota["stopped_rate_limited"] is None
    breaker_run = run_pipeline.run_weeks(_week_folders("Week 2"), _settings(tmp_path / "b"),
                                          _RateLimitScriptedClient({1: "503", 2: "503"}), force=False,
                                          sample=None, flagged=_flagged(tmp_path))
    assert breaker_run["stopped_unavailable"]["consecutive_failures"] == 2
    assert breaker_run["stopped_rate_limited"] is None


@pytest.mark.parametrize("kind, fragments", [
    ("rpm", ["per-minute rate limit, still exceeded after one bounded wait", "provider-suggested wait: 23s"]),
    ("amb", ["rate limit not identifiable as per-minute or daily", "provider-suggested wait: not provided"]),
])
def test_p11_main_operational_summary_for_rate_limit_stop(tmp_path, monkeypatch, caplog, kind, fragments):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _RateLimitScriptedClient({2: kind}),
                                          week_names=("Week 1", "Week 2"))
    assert code is None  # same exit behaviour as the quota / breaker stops
    msgs = _msgs(records)
    stop = [m for m in msgs if m.startswith("Run stopped early: Gemini returned HTTP 429")]
    assert len(stop) == 1 and all(f in stop[0] for f in fragments) and "no --force needed" in stop[0]
    assert not any("quota was exhausted" in m or "Gemini was unavailable" in m for m in msgs)
    assert any("1 classified, 0 need visual review, 1 API failure(s), 1 unprocessed (early stop)" in m
               for m in msgs)
    assert any("1 week(s) not attempted" in m and "Week 2" in m for m in msgs)


def test_p11_dry_run_validates_pacing_config_and_stays_isolated(tmp_path, monkeypatch, caplog):
    real_run_weeks = run_pipeline.run_weeks
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-offline-test")
    message = _run_main_expecting_config_error(
        tmp_path, monkeypatch, caplog, _main_settings(tmp_path, {"requests_per_minute": 0}), ["--dry-run"])
    assert "requests_per_minute" in message
    caplog.clear()
    good = tmp_path / "good"
    _seed_production(good)
    before = _snapshot(good)
    settings = _main_settings(good, {"requests_per_minute": 15})
    _patch_extract_week(monkeypatch, _p01_images())
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1"))
    monkeypatch.setattr(run_pipeline, "run_weeks", real_run_weeks)  # undo the config-error stub
    monkeypatch.setattr(vision_client, "GeminiVisionClient",
                        lambda *a, **kw: pytest.fail("dry-run must never construct a Gemini client"))
    run_pipeline.main()
    assert _snapshot(good) == before
    assert _status_column(good / "dry_run" / "output" / "Week 1" / "DRY RUN - report.xlsx") == [
        "DRY_RUN", "DRY_RUN", "CLASSIFIED", "DRY_RUN"]


def test_p11_rate_limit_stop_preserves_p02_labelled_behaviour(tmp_path, monkeypatch):
    images = [_make_image("rL", official_location="BRIDGE DECK"), _make_image("r0"), _make_image("r1")]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    client = _RateLimitScriptedClient({1: "rpm"})
    results = run_pipeline.run_weeks(_week_folders("Week 1"), settings, client, force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 1  # the labelled photo was never sent
    assert results["images"] == _outcomes(labelled=1, api_failure=1, unprocessed=1)
    # after recovery the labelled photo is still "Not Assessed (Official Location)"
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _RateLimitScriptedClient(), force=False,
                           sample=None, flagged=_flagged(tmp_path))
    _, rows = _report_table(tmp_path / "output" / "Week 1" / "report.xlsx")
    assert rows[0]["Activity Identified"] == "Not Assessed (Official Location)"
    assert [r["Classification Status"] for r in rows] == ["CLASSIFIED"] * 3


# ---------------------------------------------------------------------------
# Phase P1-2: stale reports after early stopping. The canonical report name only ever holds a
# report from a run that finished the week; an early stop writes "PARTIAL - <report>"; every
# report file an unfinished run made out of date is renamed "STALE <ts> - <name>", never deleted.
# ---------------------------------------------------------------------------

OLD_REPORT = b"OLD COMPLETE REPORT FROM AN EARLIER RUN"
OLD_MANIFEST = b"OLD MANIFEST"


def _seed_old_report(tmp_path: Path, week: str = "Week 1") -> Path:
    out = tmp_path / "output" / week
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.xlsx").write_bytes(OLD_REPORT)
    (out / "manifest.csv").write_bytes(OLD_MANIFEST)
    return out


def _files(out: Path) -> set[str]:
    return {p.name for p in out.iterdir() if p.is_file()}


def _stale(out: Path, name: str) -> list[Path]:
    import re
    pattern = re.compile(r"^STALE \d{8}_\d{6}( \(\d+\))? - " + re.escape(name) + "$")
    return sorted(p for p in out.iterdir() if pattern.match(p.name))


def _status_sheet(report_path: Path) -> dict:
    wb = openpyxl.load_workbook(report_path)
    assert wb.sheetnames == ["Image Register", "Processing Status"]
    assert wb.active.title == "Image Register"  # register stays first/active
    ws = wb["Processing Status"]
    data = {ws.cell(r, 1).value: ws.cell(r, 2).value for r in range(2, ws.max_row + 1)}
    wb.close()
    return data


@pytest.mark.parametrize("script, reason_fragment", [
    ({2: "quota"}, "Gemini daily quota exhausted"),
    ({2: "rpm"}, "per-minute rate limit"),
    ({2: "amb"}, "not identifiable as per-minute or daily"),
    ({1: "503", 2: "503"}, "circuit breaker"),
])
def test_p12_early_stop_writes_partial_report_and_retires_old_one(tmp_path, monkeypatch, script, reason_fragment):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    client = _RateLimitScriptedClient(script)
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                      force=False, sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 2 and results["weeks_not_attempted"] == ["Week 2"]  # stops unchanged
    # the canonical name no longer holds the old report; the old files are preserved byte-for-byte
    assert not (out / "report.xlsx").exists() and not (out / "manifest.csv").exists()
    [stale_report], [stale_manifest] = _stale(out, "report.xlsx"), _stale(out, "manifest.csv")
    assert stale_report.read_bytes() == OLD_REPORT and stale_manifest.read_bytes() == OLD_MANIFEST
    partial = out / "PARTIAL - report.xlsx"
    status = _status_sheet(partial)
    assert status["Report status"].startswith("PARTIAL") and "NOT a complete report" in status["Report status"]
    assert reason_fragment in status["Stop reason"]
    _, rows = _report_table(partial)
    statuses = [r["Classification Status"] for r in rows]
    assert len(rows) == 4 and statuses[2:] == ["UNPROCESSED", "UNPROCESSED"]
    assert statuses[1] == "API_FAILURE"
    assert {r["Activity Identified"] for r in rows[2:]} == {"Not Processed (Run Stopped Early)"}
    assert [m["activity_identified"] for m in _manifest_rows_named(out, "PARTIAL - manifest.csv")] == [
        r["Activity Identified"] for r in rows]
    assert not list(out.glob("*.tmp"))  # atomic save left nothing behind


def _manifest_rows_named(out: Path, name: str) -> list[dict]:
    with (out / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_p12_partial_report_uses_saved_results_for_unreached_images(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    settings = _settings(tmp_path)
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _LowConfidenceOnClient({4}), force=False,
                           sample=None, flagged=_flagged(tmp_path))
    # --force rerun stops on its 2nd request: images 3-4 were classified earlier, so the partial
    # report shows their saved results (incl. VISUAL_REVIEW), never UNPROCESSED
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _RateLimitScriptedClient({2: "quota"}), force=True,
                           sample=None, flagged=_flagged(tmp_path))
    out = tmp_path / "output" / "Week 1"
    _, rows = _report_table(out / "PARTIAL - report.xlsx")
    assert [r["Classification Status"] for r in rows] == ["CLASSIFIED", "API_FAILURE", "CLASSIFIED", "VISUAL_REVIEW"]
    assert len(_stale(out, "report.xlsx")) == 1 and not (out / "report.xlsx").exists()


def test_p12_keyboard_interrupt_retires_old_report_and_still_exits_130(tmp_path, monkeypatch, caplog):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _ScriptedClient({2: "kbi"}))
    assert code == 130
    assert _files(out) == {_stale(out, "report.xlsx")[0].name, _stale(out, "manifest.csv")[0].name}
    assert _stale(out, "report.xlsx")[0].read_bytes() == OLD_REPORT
    assert any("processing of this week did not finish" in r.message for r in records)
    # the checkpoint from before the interrupt is intact and the interrupted image is retryable
    saved = state.load_state(tmp_path / ".state", "Week 1")
    assert [e["ok"] for e in saved.values()] == [True]


def test_p12_other_week_failure_also_retires_the_old_report(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)

    def broken_extract(*a, **kw):
        raise extraction.WorkbookExtractionError(Path("corrupt.xlsx"), ValueError("simulated"))

    monkeypatch.setattr(extraction, "extract_week", broken_extract)
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), _ScriptedClient(),
                                      force=False, sample=None, flagged=_flagged(tmp_path))
    assert [(f["week"], f["exception_type"]) for f in results["failed"]] == [("Week 1", "WorkbookExtractionError")]
    assert not (out / "report.xlsx").exists() and _stale(out, "report.xlsx")[0].read_bytes() == OLD_REPORT


def test_p12_repeated_stops_never_overwrite_earlier_stale_or_partial_files(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    settings = _settings(tmp_path)
    for _ in range(2):
        run_pipeline.run_weeks(_week_folders("Week 1"), settings, _RateLimitScriptedClient({1: "quota"}),
                               force=False, sample=None, flagged=_flagged(tmp_path))
    assert [p.read_bytes() for p in _stale(out, "report.xlsx")] == [OLD_REPORT]
    assert len(_stale(out, "PARTIAL - report.xlsx")) == 1  # first partial retired, not overwritten
    assert (out / "PARTIAL - report.xlsx").exists()


def test_p12_partial_build_failure_never_masks_the_stop(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(3)])
    real_build = run_pipeline.report_builder.build_report

    def failing_build(week_name, rows, path, *a, **kw):
        if path.name.startswith("PARTIAL - "):
            raise run_pipeline.report_builder.ReportGenerationError("simulated", original=None, image_filename="x")
        return real_build(week_name, rows, path, *a, **kw)

    monkeypatch.setattr(run_pipeline.report_builder, "build_report", failing_build)
    client = _RateLimitScriptedClient({1: "quota"})
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                      force=False, sample=None, flagged=_flagged(tmp_path))
    assert client.calls == 1  # still a quota stop: Week 2 never requested
    assert results["stopped_early"]["week"] == "Week 1" and results["weeks_not_attempted"] == ["Week 2"]
    assert not (out / "report.xlsx").exists() and not (out / "PARTIAL - report.xlsx").exists()
    assert _stale(out, "report.xlsx")[0].read_bytes() == OLD_REPORT


def test_p12_normal_run_writes_complete_report_without_stale_or_partial_files(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, _p02_images())
    settings = _settings(tmp_path)
    for _ in range(2):  # a fresh run and an all-cached rerun: neither creates false stale files
        run_pipeline.run_weeks(_week_folders("Week 1"), settings, _p02_client(), force=False,
                               sample=None, flagged=_flagged(tmp_path))
        assert _files(out) == {"report.xlsx", "manifest.csv"}
    status = _status_sheet(out / "report.xlsx")
    assert status["Report status"] == "COMPLETE"  # M1: the rerun retried the failed photo successfully
    assert "API_FAILURE" in status["Rows by Classification Status"] or "CLASSIFIED" in status["Rows by Classification Status"]
    header, rows = _report_table(out / "report.xlsx")
    assert header == EXPECTED_HEADERS and "UNPROCESSED" not in {r["Classification Status"] for r in rows}


def test_p12_sample_report_is_labelled_as_sample_only(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), _ScriptedClient(), force=False,
                           sample=1, flagged=_flagged(tmp_path))
    # P1-2 correction: sample reports carry the SAMPLE prefix, never the normal report name
    status = _status_sheet(tmp_path / "output" / "Week 1" / "SAMPLE - report.xlsx")
    assert status["Report status"].startswith("COMPLETE FOR A SAMPLE ONLY - first 1 of 4")
    assert status["Photos in this report"] == "1 of 4 extracted"
    assert not (tmp_path / "output" / "Week 1" / "report.xlsx").exists()


# ---------------------------------------------------------------------------
# P1-2 correction: --sample runs use their own "SAMPLE - " file family and never write, replace
# or retire the normal complete report.
# ---------------------------------------------------------------------------


def test_p12s_sample_run_writes_sample_files_and_never_touches_complete_report(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    client = _ScriptedClient()
    run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), client, force=False,
                           sample=2, flagged=_flagged(tmp_path))
    assert client.calls == 2
    assert _files(out) == {"report.xlsx", "manifest.csv", "SAMPLE - report.xlsx", "SAMPLE - manifest.csv"}
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT  # complete report protected
    assert (out / "manifest.csv").read_bytes() == OLD_MANIFEST
    _, rows = _report_table(out / "SAMPLE - report.xlsx")
    assert len(rows) == 2 and _report_table(out / "SAMPLE - report.xlsx")[0] == EXPECTED_HEADERS
    assert [m["image_filename"] for m in _manifest_rows_named(out, "SAMPLE - manifest.csv")] == [
        r["Image Filename"] for r in rows]
    assert _status_sheet(out / "SAMPLE - report.xlsx")["Report status"].startswith("COMPLETE FOR A SAMPLE ONLY")


def test_p12s_sample_covering_every_photo_is_still_a_sample_file(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(2)])
    run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), _ScriptedClient(), force=False,
                           sample=10, flagged=_flagged(tmp_path))
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT
    assert _status_sheet(out / "SAMPLE - report.xlsx")["Report status"].startswith(
        "COMPLETE FOR A SAMPLE RUN - --sample 10 covered all 2 photos")


@pytest.mark.parametrize("script", [{1: "quota"}, {1: "rpm"}, {1: "503", 2: "503"}])
def test_p12s_sample_early_stop_writes_partial_sample_and_keeps_complete_report(tmp_path, monkeypatch, script):
    out = _seed_old_report(tmp_path)
    (out / "SAMPLE - report.xlsx").write_bytes(b"OLD SAMPLE")
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path),
                                      _RateLimitScriptedClient(script), force=False, sample=3,
                                      flagged=_flagged(tmp_path))
    assert results["weeks_not_attempted"] == ["Week 2"]  # stop behaviour unchanged
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT  # never retired by a sample run
    assert not (out / "SAMPLE - report.xlsx").exists()
    assert _stale(out, "SAMPLE - report.xlsx")[0].read_bytes() == b"OLD SAMPLE"
    partial = out / "PARTIAL - SAMPLE - report.xlsx"
    assert _status_sheet(partial)["Report status"].startswith("PARTIAL")
    assert len(_report_table(partial)[1]) == 3 and (out / "PARTIAL - SAMPLE - manifest.csv").exists()


def test_p12s_sample_interrupt_retires_only_sample_files(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)
    (out / "SAMPLE - report.xlsx").write_bytes(b"OLD SAMPLE")
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    with pytest.raises(KeyboardInterrupt):
        run_pipeline.process_week(_week_folders("Week 1")[0], _settings(tmp_path), _ScriptedClient({1: "kbi"}),
                                  force=False, sample=2, flagged=_flagged(tmp_path))
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT and (out / "manifest.csv").read_bytes() == OLD_MANIFEST
    assert _stale(out, "SAMPLE - report.xlsx")[0].read_bytes() == b"OLD SAMPLE"


def test_p12s_complete_run_uses_normal_name_and_supersedes_sample_files(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p02_images())  # includes labelled photos (P0-2)
    settings = _settings(tmp_path)
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _ScriptedClient(), force=False, sample=2,
                           flagged=_flagged(tmp_path))
    out = tmp_path / "output" / "Week 1"
    sample_bytes = (out / "SAMPLE - report.xlsx").read_bytes()
    client = _ScriptedClient()
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, client, force=False, sample=None,
                           flagged=_flagged(tmp_path))
    assert client.calls == 2  # the sample's AI photo is cached; the other 2 AI photos are new
    assert {"report.xlsx", "manifest.csv"} <= _files(out)
    assert not (out / "SAMPLE - report.xlsx").exists() and not (out / "SAMPLE - manifest.csv").exists()
    assert _stale(out, "SAMPLE - report.xlsx")[0].read_bytes() == sample_bytes  # preserved, renamed
    assert _stale(out, "SAMPLE - manifest.csv")
    assert _status_sheet(out / "report.xlsx")["Report status"] == "COMPLETE"  # M1: exact; no API failures
    _, rows = _report_table(out / "report.xlsx")
    assert [r["Activity Identified"] for r in rows if r["Official Location"] != "Not Labelled"] == [
        "Not Assessed (Official Location)"] * 2


def test_p12s_sample_rerun_after_rate_limit_recovers_into_sample_file(tmp_path, monkeypatch):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(4)])
    settings = _settings(tmp_path)
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _RateLimitScriptedClient({1: "amb"}), force=False,
                           sample=2, flagged=_flagged(tmp_path))
    client = _RateLimitScriptedClient()
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, client, force=False, sample=2,
                           flagged=_flagged(tmp_path))
    assert client.calls == 2  # P1-1: failed + unprocessed sample photos retried, no --force
    assert not (out / "PARTIAL - SAMPLE - report.xlsx").exists()
    assert _stale(out, "PARTIAL - SAMPLE - report.xlsx")
    assert [r["Classification Status"] for r in _report_table(out / "SAMPLE - report.xlsx")[1]] == ["CLASSIFIED"] * 2
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT


def test_p12s_dry_run_sample_stays_isolated(tmp_path, monkeypatch):
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    settings = _main_settings(tmp_path)
    _patch_extract_week(monkeypatch, _p01_images())
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run", "--sample", "2"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1"))
    monkeypatch.setattr(vision_client, "GeminiVisionClient",
                        lambda *a, **kw: pytest.fail("dry-run must never construct a Gemini client"))
    run_pipeline.main()
    assert _snapshot(tmp_path) == before  # production (incl. its complete report) untouched
    dry_out = tmp_path / "dry_run" / "output" / "Week 1"
    assert _files(dry_out) == {"SAMPLE - DRY RUN - report.xlsx", "SAMPLE - manifest.csv"}
    assert [r["Classification Status"] for r in _report_table(dry_out / "SAMPLE - DRY RUN - report.xlsx")[1]] == [
        "DRY_RUN", "DRY_RUN"]


def test_p12_completed_rerun_supersedes_partial_and_recovers(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p01_images())  # 3 AI + 1 labelled (index 2)
    settings = _settings(tmp_path)
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _RateLimitScriptedClient({1: "rpm"}),
                           force=False, sample=None, flagged=_flagged(tmp_path))
    out = tmp_path / "output" / "Week 1"
    _, partial_rows = _report_table(out / "PARTIAL - report.xlsx")
    assert [r["Classification Status"] for r in partial_rows] == ["API_FAILURE", "UNPROCESSED", "CLASSIFIED", "UNPROCESSED"]
    assert partial_rows[2]["Activity Identified"] == "Not Assessed (Official Location)"  # P0-2 kept
    client = _RateLimitScriptedClient()
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, client, force=False, sample=None,
                           flagged=_flagged(tmp_path))
    assert client.calls == 3  # P1-1 recovery: every AI photo retried without --force
    assert not (out / "PARTIAL - report.xlsx").exists() and not (out / "PARTIAL - manifest.csv").exists()
    assert len(_stale(out, "PARTIAL - report.xlsx")) == 1
    assert _status_sheet(out / "report.xlsx")["Report status"].startswith("COMPLETE")
    _, rows = _report_table(out / "report.xlsx")
    assert [r["Classification Status"] for r in rows] == ["CLASSIFIED"] * 4


def test_p12_weeks_are_handled_independently(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(str(i)) for i in range(2)])
    out1, out2, out3 = (_seed_old_report(tmp_path, w) for w in ("Week 1", "Week 2", "Week 3"))
    # Week 1 completes (calls 1-2); Week 2 hits the quota on its first request (call 3)
    run_pipeline.run_weeks(_week_folders("Week 1", "Week 2", "Week 3"), _settings(tmp_path),
                           _RateLimitScriptedClient({3: "quota"}), force=False, sample=None,
                           flagged=_flagged(tmp_path))
    assert _files(out1) == {"report.xlsx", "manifest.csv"}
    assert _status_sheet(out1 / "report.xlsx")["Report status"].startswith("COMPLETE")
    assert (out2 / "PARTIAL - report.xlsx").exists() and not (out2 / "report.xlsx").exists()
    # Week 3 was never attempted: its existing report is current for its state -- left untouched
    assert _files(out3) == {"report.xlsx", "manifest.csv"} and (out3 / "report.xlsx").read_bytes() == OLD_REPORT


def test_p12_dry_run_interrupt_only_touches_dry_run_files(tmp_path, monkeypatch, caplog):
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    dry_out = tmp_path / "dry_run" / "output" / "Week 1"
    dry_out.mkdir(parents=True)
    (dry_out / "DRY RUN - report.xlsx").write_bytes(b"OLD DRY RUN REPORT")

    def interrupt(self, image_bytes):
        raise KeyboardInterrupt()

    monkeypatch.setattr(vision_client.NullVisionClient, "classify", interrupt)
    with pytest.raises(SystemExit) as exc_info:
        _run_dry_main(tmp_path, monkeypatch)
    assert exc_info.value.code == 130
    assert _snapshot(tmp_path) == before  # production untouched
    assert not (dry_out / "DRY RUN - report.xlsx").exists()
    assert _stale(dry_out, "DRY RUN - report.xlsx")[0].read_bytes() == b"OLD DRY RUN REPORT"


def test_p12_status_sheet_is_opt_in_and_register_schema_unchanged(tmp_path):
    from src import report_builder
    row = report_builder.ReportRow(
        serial_number=1, image_filename="a.jpg", report_date="2026-08-02", original_excel_filename="f.xlsx",
        worksheet_name="S", official_location="Main Deck", ai_suggested_location=None,
        image_bytes=_fake_jpeg_bytes("sb"), activity_identified="x", location_confidence=None)
    plain, labelled = tmp_path / "plain.xlsx", tmp_path / "labelled.xlsx"
    report_builder.build_report("Week 1", [row], plain)
    report_builder.build_report("Week 1", [row], labelled, processing_status=[("Report status", "COMPLETE")])
    assert openpyxl.load_workbook(plain).sheetnames == ["Image Register"]
    assert openpyxl.load_workbook(labelled).sheetnames == ["Image Register", "Processing Status"]
    assert _report_table(plain)[0] == _report_table(labelled)[0] == EXPECTED_HEADERS


def test_p12_dry_run_report_has_complete_status_sheet(tmp_path, monkeypatch):
    _run_dry_main(tmp_path, monkeypatch)
    report = tmp_path / "dry_run" / "output" / "Week 1" / "DRY RUN - report.xlsx"
    status = _status_sheet(report)
    assert status["Report status"].startswith("COMPLETE") and "DRY_RUN: 3" in status["Rows by Classification Status"]


# ---------------------------------------------------------------------------
# Phase P1-3: daily-quota stop guidance, accurate remaining-work counts, read-only --quota-plan.
# Mocked clients and tmp_path only -- no request ever leaves the process.
# ---------------------------------------------------------------------------

P13_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


class _P13Client(_RateLimitScriptedClient):
    """Adds "quota20" (daily quota, Google reported quotaValue 20) and "quota_bare" (daily quota,
    no quotaValue and no retry delay in the response)."""

    def classify(self, image_bytes: bytes) -> dict:
        kind = self._script.get(self.calls + 1)
        if kind == "quota20":
            self.calls += 1
            raise vision_client.GeminiQuotaExhaustedError("quota exhausted (simulated)", 6843.0,
                                                          quota_limit=20, quota_id=P13_QUOTA_ID)
        if kind == "quota_bare":
            self.calls += 1
            raise vision_client.GeminiQuotaExhaustedError("quota exhausted (simulated)", None)
        return super().classify(image_bytes)


def _p13_stop_text(records):
    return "\n".join(_msgs(records))


def test_p13_daily_quota_message_explains_stop_resume_and_reports(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(f"q{i}") for i in range(4)])
    client = _P13Client({2: "quota20"})
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, client, ("Week 1", "Week 2"))
    assert code is None and client.calls == 2  # exit behaviour + stop point unchanged
    text = _p13_stop_text(records)
    # what was detected (keeps the phrase older tests rely on)
    assert ("Run stopped early: Gemini's daily request quota was exhausted while processing "
            f"Week 1 (Timestamp_Pending_002.jpg) -- Google's HTTP 429 named the per-day quota {P13_QUOTA_ID}.") in text
    assert "quota was exhausted" in text
    # stopped safely, current image retryable, nothing later processed
    assert "Processing stopped safely: no further Gemini requests were made." in text
    assert "Timestamp_Pending_002.jpg is saved as a failed attempt (not a classification) and will be retried automatically." in text
    assert "No later photo in Week 1 was processed; the 1 later week(s) (Week 2) were not attempted." in text
    # reports: partial named, not-attempted weeks explained
    assert "Week 1: PARTIAL report written (PARTIAL - report.xlsx) -- it is NOT complete" in text
    assert "Weeks not attempted were not touched" in text
    # resume without --force
    assert "To resume: rerun the same command after the daily quota resets. Do not add --force" in text
    assert not any(p in text for p in ("use --force", "with --force", "rerun with --force"))
    # provider-supplied wait, provider-documented reset, explicitly not computed by us
    assert "Google's response suggested retrying after 6843s (about 1h 54m" in text
    assert "not from a known reset time" in text
    assert "reset at midnight Pacific time and apply per project (https://aistudio.google.com/docs/rate-limits)" in text
    assert "this pipeline does not compute the reset itself" in text
    # remaining work + conditional estimate from Google's reported limit
    assert ("Remaining AI work known to this run: 3 AI-assessable photo(s) still need a Gemini request "
            "(1 failed attempt(s) + 2 not reached). NOT included: the 1 week(s) not attempted (never extracted).") in text
    assert "Google's 429 reported a limit of 20 request(s)/day. IF that limit is unchanged" in text
    assert "these 3 photo(s) need at least 1 more quota day(s)" in text
    assert "a lower bound, not a completion date" in text


def test_p13_daily_quota_message_names_stale_files(tmp_path, monkeypatch, caplog):
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image(f"s{i}") for i in range(2)])
    records, _ = _run_main_with_client(tmp_path, monkeypatch, caplog, _P13Client({1: "quota20"}))
    stale = [p.name for p in _stale(out, "report.xlsx") + _stale(out, "manifest.csv")]
    assert len(stale) == 2
    text = _p13_stop_text(records)
    assert "Previous report file(s) for Week 1 were renamed so they cannot be mistaken" in text
    assert all(name in text for name in stale)
    assert "(out of date, kept, not deleted)" in text


def test_p13_no_reported_limit_means_no_day_estimate_and_no_invented_time(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(f"n{i}") for i in range(3)])
    records, _ = _run_main_with_client(tmp_path, monkeypatch, caplog, _P13Client({1: "quota_bare"}))
    text = _p13_stop_text(records)
    assert "quota was exhausted" in text
    assert "Google's response did not include a suggested retry delay." in text
    assert "No day estimate: Google's response did not report a daily limit, and this pipeline does not assume one." in text
    assert "request(s)/day" not in text and "quota day(s)" not in text and "around " not in text
    assert "local midnight" not in text.lower() and "midnight Pacific time" in text  # only the documented fact
    assert "Remaining AI work known to this run: 3 AI-assessable photo(s)" in text


def test_p13_retry_time_is_derived_only_from_google_delay():
    ist = timezone(timedelta(hours=5, minutes=30))
    results = {"stopped_early": {"week": "Week 30", "retry_delay_seconds": 6843.0},
               "quota_stop_details": {"quota_limit": 20, "quota_id": P13_QUOTA_ID, "image": "x.jpg",
                                      "partial_report": "PARTIAL - r.xlsx", "stale_files": []},
               "images": _outcomes(api_failure=1, unprocessed=65), "weeks_not_attempted": []}
    # the real 2026-11-12 429: 11:35:57 IST + 6843 s = 13:30 IST
    msgs = [m for _, m in run_pipeline._daily_quota_stop_messages(
        results, 0, 0, now=datetime(2026, 11, 12, 11, 35, 57, tzinfo=ist))]
    text = "\n".join(msgs)
    assert "around 2026-11-12 13:30 UTC+05:30 -- computed from that value, not from a known reset time" in text
    assert "66 AI-assessable photo(s) still need a Gemini request (1 failed attempt(s) + 65 not reached)" in text
    assert "need at least 4 more quota day(s)" in text  # ceil(66 / 20), conditional lower bound
    # an older/stubbed run_weeks() result without quota_stop_details still produces a message
    old = {"stopped_early": {"week": "Week 1", "retry_delay_seconds": None}}
    assert "quota was exhausted" in run_pipeline._daily_quota_stop_messages(old, 0, 0)[0][1]


def test_p13_conditional_days_never_assumes_a_limit():
    assert run_pipeline._conditional_quota_days(66, None) is None
    assert run_pipeline._conditional_quota_days(66, 0) is None
    assert run_pipeline._conditional_quota_days(66, 20) == 4
    assert run_pipeline._conditional_quota_days(0, 20) == 0
    assert run_pipeline._conditional_quota_days(40, 20) == 2


@pytest.mark.parametrize("kind, expected", [
    ("rpm", ["per-minute rate limit, still exceeded after one bounded wait", "provider-suggested wait: 23s"]),
    ("amb", ["rate limit not identifiable as per-minute or daily", "provider-suggested wait: not provided"]),
])
def test_p13_rate_limit_messages_stay_distinct_from_daily_guidance(tmp_path, monkeypatch, caplog, kind, expected):
    _patch_extract_week(monkeypatch, [_make_image(f"r{i}") for i in range(3)])
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, _P13Client({1: kind}))
    assert code is None
    text = _p13_stop_text(records)
    stop = [m for m in _msgs(records) if m.startswith("Run stopped early:")]
    assert len(stop) == 1 and stop[0].startswith("Run stopped early: Gemini returned HTTP 429")
    assert all(e in stop[0] for e in expected)
    # none of the daily-quota guidance leaks into a per-minute / ambiguous stop
    for daily_only in ("quota was exhausted", "midnight Pacific", "quota day(s)", "When the quota resets",
                       "Remaining AI work known to this run"):
        assert daily_only not in text


def test_p13_stop_counts_labelled_and_saved_photos_accurately(tmp_path, monkeypatch):
    images = [_make_image("c0"), _make_image("c1"), _make_image("cL", official_location="MAIN DECK"),
              _make_image("cDone"), _make_image("c3")]
    _patch_extract_week(monkeypatch, images)
    settings = _settings(tmp_path)
    done_hash = state.image_hash(images[3].image_bytes)
    state.save_state(Path(settings["paths"]["state_dir"]), "Week 1", {done_hash: {
        "ok": True, "activity_identified": "No activity observed", "activity_confidence": "High",
        "ai_suggested_location": "Main Deck", "location_confidence": "High", "person_visible": "No"}})
    results = run_pipeline.run_weeks(_week_folders("Week 1"), settings, _P13Client({2: "quota20"}), force=False,
                                      sample=None, flagged=_flagged(tmp_path))
    # before P1-3 the labelled photo and the already-done photo were both counted "unprocessed" (3)
    assert results["images"] == _outcomes(classified=2, labelled=1, api_failure=1, unprocessed=1)
    details = results["quota_stop_details"]
    assert details == {"quota_limit": 20, "quota_id": P13_QUOTA_ID, "image": "Timestamp_Pending_002.jpg",
                       "partial_report": "PARTIAL - report.xlsx", "stale_files": []}
    assert results["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}  # shape unchanged
    # the counted remaining work (2) is exactly what a resumed run sends
    resume = _P13Client()
    second = run_pipeline.run_weeks(_week_folders("Week 1"), settings, resume, force=False,
                                     sample=None, flagged=_flagged(tmp_path))
    assert resume.calls == 2 and second["stopped_early"] is None
    assert second["images"] == _outcomes(classified=4, labelled=1)


def test_p13_resume_without_force_completes_and_retires_partial(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"z{i}") for i in range(3)])
    settings = _settings(tmp_path)
    out = tmp_path / "output" / "Week 1"
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _P13Client({2: "quota20"}), force=False,
                           sample=None, flagged=_flagged(tmp_path))
    assert "PARTIAL - report.xlsx" in _files(out) and "report.xlsx" not in _files(out)
    resume = _P13Client()
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, resume, force=False, sample=None,
                           flagged=_flagged(tmp_path))
    assert resume.calls == 2  # image 1 was saved ok; only images 2 and 3 are sent again
    assert "report.xlsx" in _files(out) and "PARTIAL - report.xlsx" not in _files(out)
    assert _stale(out, "PARTIAL - report.xlsx")  # P1-2: retired, never deleted
    assert _status_column(out / "report.xlsx") == ["CLASSIFIED"] * 3


def test_p13_sample_quota_stop_names_sample_partial_and_excludes_unsampled(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"m{i}") for i in range(5)])
    settings = _settings(tmp_path)
    out = _seed_old_report(tmp_path)
    results = run_pipeline.run_weeks(_week_folders("Week 1"), settings, _P13Client({1: "quota20"}), force=False,
                                      sample=2, flagged=_flagged(tmp_path))
    assert results["quota_stop_details"]["partial_report"] == "PARTIAL - SAMPLE - report.xlsx"
    assert results["quota_stop_details"]["stale_files"] == []  # complete report never touched by a sample
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT
    text = "\n".join(m for _, m in run_pipeline._daily_quota_stop_messages(results, 0, 0, sample=2))
    assert "PARTIAL report written (PARTIAL - SAMPLE - report.xlsx)" in text
    assert "2 AI-assessable photo(s) still need a Gemini request" in text
    assert "NOT included: photos beyond --sample 2." in text


# --- read-only --quota-plan ---------------------------------------------------------------


def _p13_plan_setup(tmp_path, monkeypatch, argv):
    settings = _main_settings(tmp_path)
    by_week = {
        "Week 1": [_make_image("w1a"), _make_image("w1b"), _make_image("w1c"),
                   _make_image("w1L", official_location="BRIDGE")],
        "Week 2": [_make_image("w2a"), _make_image("w2b")],
    }
    monkeypatch.setattr(extraction, "extract_week", lambda workbooks, *a, **kw: extraction.ExtractionResult(
        images=by_week[workbooks[0].parent.name], report_date=None, warnings=[]))
    entry = {"activity_identified": "x", "activity_confidence": "High", "ai_suggested_location": "Main Deck",
             "location_confidence": "High", "person_visible": "No"}
    state.save_state(tmp_path / ".state", "Week 1", {
        state.image_hash(by_week["Week 1"][0].image_bytes): {"ok": True, **entry},
        state.image_hash(by_week["Week 1"][1].image_bytes): {"ok": False, **entry}})
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1", "Week 2"))

    def _forbidden(*a, **kw):
        raise AssertionError("--quota-plan must never build a client or open a connection")

    import socket
    monkeypatch.setattr(run_pipeline.vision_client, "build_vision_client", _forbidden)
    monkeypatch.setattr(run_pipeline.logging_utils, "configure_logging", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    return settings


def test_p13_quota_plan_counts_remaining_from_state_and_writes_nothing(tmp_path, monkeypatch, capsys):
    _p13_plan_setup(tmp_path, monkeypatch, ["--quota-plan"])
    before = _snapshot(tmp_path)
    run_pipeline.main()
    assert _snapshot(tmp_path) == before  # state untouched, no output, no log
    assert not (tmp_path / "logs").exists() and not (tmp_path / "output").exists()
    assert not (tmp_path / "dry_run").exists()
    out = capsys.readouterr().out
    assert "QUOTA PLAN (read-only: no API calls, nothing written)" in out
    assert "Week 1: 3 AI-assessable, 1 done, 2 remaining" in out  # ok:false is NOT done
    assert "Week 2: 2 AI-assessable, 0 done, 2 remaining" in out
    assert "Total: 5 AI-assessable photo(s) in 2 readable week(s); 1 done" in out and "4 still need a Gemini request" in out
    assert "No day estimate: no daily limit was supplied and this pipeline does not assume one." in out
    assert "quota day(s)" not in out
    assert "midnight Pacific time" in out and "local midnight" not in out.lower()


def test_p13_quota_plan_conditional_estimate_uses_only_the_supplied_limit(tmp_path, monkeypatch, capsys):
    _p13_plan_setup(tmp_path, monkeypatch, ["--quota-plan", "--daily-request-limit", "3"])
    run_pipeline.main()
    out = capsys.readouterr().out
    assert "at 3 request(s)/day (the limit YOU supplied), 4 request(s) need at least 2 quota day(s) -- IF" in out
    assert "A lower bound, not a completion date." in out


def test_p13_quota_plan_respects_weeks_and_reports_unreadable_week(tmp_path, monkeypatch, capsys):
    _p13_plan_setup(tmp_path, monkeypatch, ["--quota-plan", "--weeks", "Week 2"])
    run_pipeline.main()
    out = capsys.readouterr().out
    assert "Week 2: 2 AI-assessable" in out and "Week 1:" not in out
    settings = _main_settings(tmp_path)

    def _broken(*a, **kw):
        raise extraction.WorkbookExtractionError(Path("bad.xlsx"), ValueError("corrupt"))

    monkeypatch.setattr(extraction, "extract_week", _broken)
    plan = run_pipeline.quota_plan(_week_folders("Week 9"), settings)
    assert plan[0]["error"].startswith("WorkbookExtractionError") and plan[0]["remaining"] == 0
    assert "Not counted: 1 unreadable week(s): Week 9." in "\n".join(run_pipeline._quota_plan_lines(plan, None))


@pytest.mark.parametrize("argv", [
    ["--daily-request-limit", "20"],                 # only meaningful with --quota-plan
    ["--quota-plan", "--daily-request-limit", "0"],
    ["--quota-plan", "--dry-run"],
    ["--quota-plan", "--force"],
    ["--quota-plan", "--sample", "1"],
])
def test_p13_quota_plan_argument_errors_exit_before_any_work(tmp_path, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])

    def _forbidden(*a, **kw):
        raise AssertionError("must not load settings or process anything")

    monkeypatch.setattr(run_pipeline.config, "load_settings", _forbidden)
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    assert exc_info.value.code == 2


def test_p13_dry_run_still_isolated_and_never_quota_stops(tmp_path, monkeypatch, caplog):
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        _run_dry_main(tmp_path, monkeypatch)
    assert _snapshot(tmp_path) == before
    assert not any("quota was exhausted" in r.message or "Remaining AI work" in r.message
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# Phase P1-5: optional --strict-exit. Default exit behaviour must be unchanged without it.
# ---------------------------------------------------------------------------

STRICT = run_pipeline.STRICT_EXIT_CODE


def _run_main_argv(tmp_path, monkeypatch, caplog, client, argv=(), week_names=("Week 1",)):
    """_run_main_with_client, but with CLI arguments. Returns (records, exit_code); exit_code is
    None when main() returns normally (the process would exit 0)."""
    settings = _main_settings(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders(*week_names))
    monkeypatch.setattr(run_pipeline.vision_client, "build_vision_client", lambda *a, **kw: client)
    exit_code = None
    with caplog.at_level(logging.INFO, logger="pipeline"):
        try:
            run_pipeline.main()
        except SystemExit as exc:
            exit_code = exc.code
    return [r for r in caplog.records if r.name == "pipeline"], exit_code


def _strict_line(records):
    lines = [m for m in _msgs(records) if m.startswith("--strict-exit:")]
    assert len(lines) == 1
    return lines[0]


def _three_ai_images(monkeypatch, tag="s"):
    _patch_extract_week(monkeypatch, [_make_image(f"{tag}{i}") for i in range(3)])


def test_p15_normal_run_without_flag_returns_0(tmp_path, monkeypatch, caplog):
    _three_ai_images(monkeypatch)
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, _ScriptedClient())
    assert code is None
    assert not any(m.startswith("--strict-exit") for m in _msgs(records))


def test_p15_successful_strict_run_returns_0(tmp_path, monkeypatch, caplog):
    _three_ai_images(monkeypatch)
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, _ScriptedClient(), ["--strict-exit"])
    assert code is None
    assert _strict_line(records) == "--strict-exit: processing completed successfully; exit code 0."


def test_p15_visual_review_and_labelled_photos_are_not_failures(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image("v0"), _make_image("vL", official_location="MAIN DECK")])
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, _LowConfidenceOnClient({1}), ["--strict-exit"])
    assert code is None
    assert _status_column(tmp_path / "output" / "Week 1" / "report.xlsx") == ["VISUAL_REVIEW", "CLASSIFIED"]
    _, rows = _report_table(tmp_path / "output" / "Week 1" / "report.xlsx")
    assert rows[1]["Activity Identified"] == "Not Assessed (Official Location)"  # P0-2 unchanged


@pytest.mark.parametrize("strict, expected_code", [(False, None), (True, STRICT)])
def test_p15_api_failure(tmp_path, monkeypatch, caplog, strict, expected_code):
    _three_ai_images(monkeypatch)
    client = _ScriptedClient({2: "err"})
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, client, ["--strict-exit"] if strict else [])
    assert code == expected_code
    assert client.calls == 3  # an ordinary API failure never stops the run, in either mode
    assert any(m.startswith("Run complete WITH API FAILURES") for m in _msgs(records))
    if strict:
        assert _strict_line(records) == (f"--strict-exit: exiting with code {STRICT} because processing did "
                                         "not complete successfully: 1 API failure(s).")


@pytest.mark.parametrize("strict, expected_code", [(False, None), (True, STRICT)])
def test_p15_daily_quota_stop(tmp_path, monkeypatch, caplog, strict, expected_code):
    _three_ai_images(monkeypatch)
    client = _P13Client({2: "quota20"})
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, client, ["--strict-exit"] if strict else [],
                                   ("Week 1", "Week 2"))
    assert code == expected_code and client.calls == 2  # stop point unchanged
    text = _p13_stop_text(records)
    assert "quota was exhausted" in text and "Do not add --force" in text  # P1-3 guidance intact
    if strict:
        line = _strict_line(records)
        assert "daily quota exhausted in Week 1" in line
        assert "1 API failure(s); 1 photo(s) unprocessed; 1 week(s) not attempted" in line
    # P1-2 unchanged: the stopped week has only a PARTIAL report
    assert "PARTIAL - report.xlsx" in _files(tmp_path / "output" / "Week 1")


@pytest.mark.parametrize("kind, fragment", [
    ("rpm", "HTTP 429 (per-minute rate limit, still exceeded after one bounded wait) in Week 1"),
    ("amb", "HTTP 429 (rate limit not identifiable as per-minute or daily) in Week 1"),
])
def test_p15_rate_limit_stops(tmp_path, monkeypatch, caplog, kind, fragment):
    _three_ai_images(monkeypatch)
    _, default_code = _run_main_argv(tmp_path / "d", monkeypatch, caplog, _P13Client({1: kind}))
    assert default_code is None  # default unchanged
    caplog.clear()
    records, code = _run_main_argv(tmp_path / "s", monkeypatch, caplog, _P13Client({1: kind}), ["--strict-exit"])
    assert code == STRICT
    assert fragment in _strict_line(records)
    assert "daily quota" not in _strict_line(records)


def test_p15_circuit_breaker_stop(tmp_path, monkeypatch, caplog):
    _three_ai_images(monkeypatch)
    _, default_code = _run_main_argv(tmp_path / "d", monkeypatch, caplog, _ScriptedClient({1: "503", 2: "503"}))
    assert default_code is None
    caplog.clear()
    client = _ScriptedClient({1: "503", 2: "503"})
    records, code = _run_main_argv(tmp_path / "s", monkeypatch, caplog, client, ["--strict-exit"])
    assert code == STRICT and client.calls == 2
    assert "circuit breaker tripped (Gemini unavailable) in Week 1" in _strict_line(records)
    assert "2 API failure(s); 1 photo(s) unprocessed" in _strict_line(records)


def test_p15_per_week_failure_is_isolated_and_fails_strict(tmp_path, monkeypatch, caplog):
    good = extraction.ExtractionResult(images=[_make_image("g0")], report_date=None, warnings=[])

    def _extract(workbooks, *a, **kw):
        if workbooks[0].parent.name == "Week 1":
            raise extraction.WorkbookExtractionError(Path("bad.xlsx"), ValueError("corrupt"))
        return good

    monkeypatch.setattr(extraction, "extract_week", _extract)
    for strict, expected in ((False, None), (True, STRICT)):
        caplog.clear()
        client = _ScriptedClient()
        records, code = _run_main_argv(tmp_path / str(strict), monkeypatch, caplog, client,
                                       ["--strict-exit"] if strict else [], ("Week 1", "Week 2"))
        assert code == expected
        assert client.calls == 1  # Week 2 still processed: isolation unchanged
        if strict:
            assert _strict_line(records).endswith("processing did not complete successfully: "
                                                  "1 week(s) failed: Week 1.")


def test_p15_keyboard_interrupt_still_130(tmp_path, monkeypatch, caplog):
    _three_ai_images(monkeypatch)
    for argv in ([], ["--strict-exit"]):
        caplog.clear()
        records, code = _run_main_argv(tmp_path / str(len(argv)), monkeypatch, caplog,
                                       _ScriptedClient({2: "kbi"}), argv)
        assert code == 130
        assert not any(m.startswith("--strict-exit") for m in _msgs(records))


def test_p15_successful_dry_run_strict_returns_0_and_stays_isolated(tmp_path, monkeypatch, caplog):
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    settings = _main_settings(tmp_path)
    _patch_extract_week(monkeypatch, _p01_images())
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run", "--strict-exit"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders("Week 1"))

    def _forbidden(*a, **kw):
        raise AssertionError("dry-run must never construct a real client")

    monkeypatch.setattr(vision_client, "GeminiVisionClient", _forbidden)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        run_pipeline.main()  # returns normally -> exit 0: DRY_RUN placeholders are not failures
    assert _strict_line([r for r in caplog.records if r.name == "pipeline"]) == \
        "--strict-exit: processing completed successfully; exit code 0."
    assert _snapshot(tmp_path) == before  # P0-1 isolation unchanged
    assert _status_column(tmp_path / "dry_run" / "output" / "Week 1" / "DRY RUN - report.xlsx") == [
        "DRY_RUN", "DRY_RUN", "CLASSIFIED", "DRY_RUN"]


def test_p15_successful_sample_run_strict_returns_0(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(f"p{i}") for i in range(5)])
    client = _ScriptedClient()
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, client, ["--strict-exit", "--sample", "2"])
    assert code is None and client.calls == 2  # fewer than all photos is not a failure
    out = tmp_path / "output" / "Week 1"
    assert "SAMPLE - report.xlsx" in _files(out) and "report.xlsx" not in _files(out)  # P1-2 naming


@pytest.mark.parametrize("script, fragment", [
    ({2: "err"}, "1 API failure(s)"),
    ({1: "quota20"}, "daily quota exhausted in Week 1"),
])
def test_p15_failed_sample_run_respects_strict(tmp_path, monkeypatch, caplog, script, fragment):
    _patch_extract_week(monkeypatch, [_make_image(f"f{i}") for i in range(5)])
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, _P13Client(script),
                                   ["--strict-exit", "--sample", "2"])
    assert code == STRICT and fragment in _strict_line(records)


def test_p15_strict_with_weeks_and_force(tmp_path, monkeypatch, caplog):
    _three_ai_images(monkeypatch)
    _run_main_argv(tmp_path, monkeypatch, caplog, _ScriptedClient())  # all saved ok
    caplog.clear()
    client = _ScriptedClient()
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, client,
                                   ["--strict-exit", "--weeks", "Week 1", "--force"])
    assert code is None and client.calls == 3  # --force still re-sends everything
    caplog.clear()
    resume = _ScriptedClient()
    _, code = _run_main_argv(tmp_path, monkeypatch, caplog, resume, ["--strict-exit", "--weeks", "Week 1"])
    assert code is None and resume.calls == 0  # nothing left: cached results are success


def test_p15_strict_recovery_after_failure_returns_0(tmp_path, monkeypatch, caplog):
    # P1-1 / P1-2 compatibility: a strict run that stops on a per-minute 429 fails, the rerun
    # (no --force) retries only what is outstanding, writes the complete report, and returns 0.
    _three_ai_images(monkeypatch)
    _, code = _run_main_argv(tmp_path, monkeypatch, caplog, _P13Client({2: "rpm"}), ["--strict-exit"])
    assert code == STRICT
    out = tmp_path / "output" / "Week 1"
    assert "PARTIAL - report.xlsx" in _files(out)
    caplog.clear()
    resume = _P13Client()
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, resume, ["--strict-exit"])
    assert code is None and resume.calls == 2
    assert "report.xlsx" in _files(out) and _stale(out, "PARTIAL - report.xlsx")
    assert _strict_line(records).endswith("exit code 0.")


def test_p15_quota_plan_exit_codes(tmp_path, monkeypatch, capsys):
    # P1-3 compatibility: a successful plan returns normally with or without --strict-exit.
    for argv in (["--quota-plan"], ["--quota-plan", "--strict-exit"],
                 ["--quota-plan", "--strict-exit", "--daily-request-limit", "20", "--weeks", "Week 1"]):
        _p13_plan_setup(tmp_path / str(len(argv)), monkeypatch, argv)
        run_pipeline.main()
        assert "QUOTA PLAN" in capsys.readouterr().out


def test_p15_quota_plan_with_unreadable_week(tmp_path, monkeypatch, capsys):
    _p13_plan_setup(tmp_path, monkeypatch, ["--quota-plan"])

    def _broken(*a, **kw):
        raise extraction.WorkbookExtractionError(Path("bad.xlsx"), ValueError("corrupt"))

    monkeypatch.setattr(extraction, "extract_week", _broken)
    run_pipeline.main()  # default: unchanged, returns normally
    assert "could not be read" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--quota-plan", "--strict-exit"])
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    assert exc_info.value.code == STRICT
    assert "2 week(s) could not be read: Week 1, Week 2" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--strict-exit", "--daily-request-limit", "5"],
                                  ["--strict-exit", "--quota-plan", "--dry-run"],
                                  ["--strict-exit", "--sample", "x"]])
def test_p15_invalid_combinations_are_still_usage_errors(tmp_path, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])

    def _forbidden(*a, **kw):
        raise AssertionError("must not load settings")

    monkeypatch.setattr(run_pipeline.config, "load_settings", _forbidden)
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    assert exc_info.value.code == 2


def test_p15_configuration_error_still_exits_1(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-offline-test")
    settings = _main_settings(tmp_path, rate_limit={"max_retries": -1})
    message = _run_main_expecting_config_error(tmp_path, monkeypatch, caplog, settings, ["--strict-exit"])
    assert "rate_limit.max_retries" in message


def test_p15_strict_reasons_use_only_existing_result_fields():
    assert run_pipeline._strict_exit_reasons({}) == []  # older/stubbed result shape
    ok = {"succeeded": [{}], "failed": [], "stopped_early": None, "stopped_unavailable": None,
          "stopped_rate_limited": None, "weeks_not_attempted": [],
          "images": _outcomes(classified=3, visual_review=1, labelled=2, dry_run=4)}
    assert run_pipeline._strict_exit_reasons(ok) == []
    # the stopped week is reported once, as the stop -- not again as a "failed week"
    stopped = {**ok, "failed": [{"week": "Week 1"}], "stopped_early": {"week": "Week 1"},
               "images": _outcomes(api_failure=1)}
    assert run_pipeline._strict_exit_reasons(stopped) == ["daily quota exhausted in Week 1", "1 API failure(s)"]


# ---------------------------------------------------------------------------
# Phase P2-1 / P2-2: --sample must be a positive integer; every --weeks name must match a
# discovered week folder. Invalid input is a usage error (exit 2) before anything is written.
# ---------------------------------------------------------------------------


def _forbid_settings(monkeypatch):
    def _forbidden(*a, **kw):
        raise AssertionError("invalid arguments must be rejected before settings are loaded")

    monkeypatch.setattr(run_pipeline.config, "load_settings", _forbidden)


@pytest.mark.parametrize("argv", [["--sample", "0"], ["--sample", "-1"], ["--sample"], ["--sample", "1.5"],
                                  ["--sample", "0", "--dry-run"], ["--sample", "-3", "--strict-exit"]])
def test_p21_invalid_sample_is_a_usage_error(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    _forbid_settings(monkeypatch)
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    assert exc_info.value.code == 2
    assert "--sample" in capsys.readouterr().err


def test_p21_positive_int_messages():
    assert run_pipeline._positive_int("1") == 1 and run_pipeline._positive_int("10") == 10
    for bad, fragment in (("0", "got 0"), ("-1", "got -1"), ("abc", "invalid int value")):
        with pytest.raises(run_pipeline.argparse.ArgumentTypeError, match=fragment):
            run_pipeline._positive_int(bad)


@pytest.mark.parametrize("n", [1, 3])
def test_p21_valid_sample_processes_n_photos_with_sample_naming(tmp_path, monkeypatch, caplog, n):
    _patch_extract_week(monkeypatch, [_make_image(f"n{i}") for i in range(5)])
    client = _ScriptedClient()
    _, code = _run_main_argv(tmp_path, monkeypatch, caplog, client, ["--sample", str(n)])
    assert code is None and client.calls == n
    out = tmp_path / "output" / "Week 1"
    assert "SAMPLE - report.xlsx" in _files(out) and "report.xlsx" not in _files(out)  # P1-2 naming
    assert _status_sheet(out / "SAMPLE - report.xlsx")["Report status"].startswith(
        f"COMPLETE FOR A SAMPLE ONLY - first {n} of 5 photos")


def _p22_root(tmp_path, names=("Week 2", "week 10", "Week 1", "Week 30")):
    """Real week folders (plus non-week folders) so the REAL discovery.find_week_folders runs."""
    for name in names:
        (tmp_path / name).mkdir()
        (tmp_path / name / "source (1).xlsx").write_bytes(b"not parsed: extract_week is patched")
    (tmp_path / "Archive").mkdir()  # not a week folder: never selectable
    return discovery.find_week_folders(tmp_path)


def _p22_main(tmp_path, monkeypatch, argv, names=("Week 2", "week 10", "Week 1", "Week 30")):
    """main() with real discovery and a recording run_weeks. Returns (weeks passed to run_weeks
    or None if never reached, exit_code)."""
    discovered = _p22_root(tmp_path, names)
    settings = _main_settings(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.vision_client, "build_vision_client", lambda *a, **kw: _ScriptedClient())
    seen = {}

    def _record(weeks, *a, **kw):
        seen["weeks"] = [w.name for w in weeks]
        return {"succeeded": [], "failed": [], "stopped_early": None, "stopped_unavailable": None,
                "stopped_rate_limited": None, "images": run_pipeline._empty_outcomes(),
                "weeks_not_attempted": [], "quota_stop_details": None}

    monkeypatch.setattr(run_pipeline, "run_weeks", _record)
    code = None
    try:
        run_pipeline.main()
    except SystemExit as exc:
        code = exc.code
    return seen.get("weeks"), code, [w.name for w in discovered]


def test_p22_no_weeks_argument_selects_every_discovered_week_in_order(tmp_path, monkeypatch):
    weeks, code, discovered = _p22_main(tmp_path, monkeypatch, [])
    assert code is None and weeks == discovered and "Archive" not in weeks


def test_p22_valid_selection_ignores_case_and_spacing(tmp_path, monkeypatch):
    weeks, code, _ = _p22_main(tmp_path, monkeypatch, ["--weeks", "  WEEK   10 "])
    assert code is None and weeks == ["week 10"]


def test_p22_multiple_valid_weeks_keep_discovery_order_and_dedupe(tmp_path, monkeypatch):
    weeks, code, discovered = _p22_main(tmp_path, monkeypatch, ["--weeks", "Week 30", "Week 1", "week 1", "Week 2"])
    assert code is None
    assert weeks == [n for n in discovered if n in ("Week 30", "Week 1", "Week 2")]  # discovery order
    assert len(weeks) == 3


@pytest.mark.parametrize("argv, unknown", [
    (["--weeks", "Week 99"], "'Week 99'"),                                 # unknown
    (["--weeks", "Week 1", "Week 99", "Wk 2"], "'Week 99', 'Wk 2'"),       # mixed: nothing runs
    (["--weeks", ""], "''"),                                               # blank -> matches nothing
    (["--weeks", "Archive"], "'Archive'"),                                 # a folder, but not a week
    (["--weeks", "Week 99", "--strict-exit"], "'Week 99'"),                # usage error (2), not 3
    (["--weeks", "Week 99", "--force"], "'Week 99'"),
    (["--weeks", "Week 99", "--sample", "1"], "'Week 99'"),
    (["--weeks", "Week 99", "--quota-plan"], "'Week 99'"),
])
def test_p22_unknown_weeks_are_rejected_before_processing(tmp_path, monkeypatch, capsys, argv, unknown):
    weeks, code, discovered = _p22_main(tmp_path, monkeypatch, argv)
    assert code == 2 and weeks is None  # run_weeks never reached
    err = capsys.readouterr().err
    assert f"unknown week name(s) in --weeks: {unknown}. No weeks were processed." in err
    assert "Available week folders: " + ", ".join(discovered) in err
    assert not (tmp_path / "logs").exists() and not (tmp_path / "output").exists()  # nothing written


def test_p22_weeks_without_names_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--weeks"])
    _forbid_settings(monkeypatch)
    with pytest.raises(SystemExit) as exc_info:
        run_pipeline.main()
    assert exc_info.value.code == 2 and "--weeks" in capsys.readouterr().err


def test_p22_no_week_folders_at_all_lists_none(tmp_path, monkeypatch, capsys):
    weeks, code, _ = _p22_main(tmp_path, monkeypatch, ["--weeks", "Week 1"], names=())
    assert code == 2 and weeks is None
    assert "Available week folders: none" in capsys.readouterr().err


def test_p22_dry_run_unknown_week_creates_no_dry_run_folder(tmp_path, monkeypatch, capsys):
    weeks, code, _ = _p22_main(tmp_path, monkeypatch, ["--dry-run", "--weeks", "Week 99"])
    assert code == 2 and weeks is None and not (tmp_path / "dry_run").exists()


def test_p22_dry_run_valid_week_end_to_end_stays_isolated(tmp_path, monkeypatch, caplog):
    _p22_root(tmp_path)
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    settings = _main_settings(tmp_path)
    _patch_extract_week(monkeypatch, _p01_images())
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run", "--strict-exit", "--weeks", "week 1"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)

    def _forbidden(*a, **kw):
        raise AssertionError("dry-run must never construct a real client")

    monkeypatch.setattr(vision_client, "GeminiVisionClient", _forbidden)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        run_pipeline.main()  # strict dry run of a valid week: exit 0
    assert _snapshot(tmp_path) == before  # P0-1
    dry_out = tmp_path / "dry_run" / "output"
    assert [p.name for p in dry_out.iterdir()] == ["Week 1"]  # only the selected week
    assert _status_column(dry_out / "Week 1" / "DRY RUN - report.xlsx") == [
        "DRY_RUN", "DRY_RUN", "CLASSIFIED", "DRY_RUN"]
    _, rows = _report_table(dry_out / "Week 1" / "DRY RUN - report.xlsx")
    assert rows[2]["Activity Identified"] == "Not Assessed (Official Location)"  # P0-2


def test_p22_quota_plan_with_valid_weeks_uses_real_discovery(tmp_path, monkeypatch, capsys):
    _p22_root(tmp_path)
    settings = _main_settings(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image("q0"), _make_image("qL", official_location="BRIDGE")])
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--quota-plan", "--weeks", "Week 30", "week 2"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    run_pipeline.main()
    out = capsys.readouterr().out
    assert "Week 2: 1 AI-assessable, 0 done, 1 remaining" in out
    # M3: the patched extraction gives both weeks byte-identical photos, so Week 30's photo is
    # covered by Week 2's planned request and is not counted again.
    assert ("Week 30: 1 AI-assessable, 0 done, 0 remaining, 1 identical to a photo in another week "
            "(no request)") in out
    assert "1 still need a Gemini request. 1 more are identical to a photo in another week" in out
    assert out.index("Week 2:") < out.index("Week 30:")  # discovery order
    assert "Week 1:" not in out and "week 10:" not in out
    assert not (tmp_path / "logs").exists()  # P1-3: still writes nothing


# ---------------------------------------------------------------------------
# Phase P2-6: atomic manifest writing. Synthetic rows and tmp_path only.
# ---------------------------------------------------------------------------

P26_ROWS = [
    {"serial_number": 1, "image_filename": "2026-08-02_001.jpg", "report_date": "2026-08-02",
     "original_excel_filename": "Deck Photos (1).xlsx", "worksheet_name": "AUG 02 2026 ",
     "official_location": "Not Labelled", "ai_suggested_location": "Main Deck",
     "activity_identified": "Painting, \"touch-up\"", "location_confidence": "High"},
    {"serial_number": 2, "image_filename": "2026-08-02_002.jpg", "report_date": "2026-08-02",
     "original_excel_filename": "Deck Photos (1).xlsx", "worksheet_name": "AUG 02 2026 ",
     "official_location": "BRIDGE DECK", "ai_suggested_location": "",
     "activity_identified": "Not Assessed (Official Location)", "location_confidence": ""},
]
P26_HEADER = ["serial_number", "image_filename", "report_date", "original_excel_filename", "worksheet_name",
              "official_location", "ai_suggested_location", "activity_identified", "location_confidence"]


def _p26_expected_bytes(rows) -> bytes:
    # exactly what the pre-P2-6 implementation wrote (csv.DictWriter, UTF-8, newline="")
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _tmp_files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.rglob("*.tmp"))


def test_p26_creates_manifest_with_identical_content_and_schema(tmp_path):
    path = tmp_path / "manifest.csv"
    run_pipeline._write_manifest(path, P26_ROWS)
    assert path.read_bytes() == _p26_expected_bytes(P26_ROWS)
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")  # still no BOM
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == P26_HEADER
        assert [r["activity_identified"] for r in reader] == ['Painting, "touch-up"',
                                                              "Not Assessed (Official Location)"]
    assert [p.name for p in tmp_path.iterdir()] == ["manifest.csv"]  # no temp file left


def test_p26_empty_rows_still_write_an_empty_file(tmp_path):
    path = tmp_path / "manifest.csv"
    path.write_bytes(b"OLD")
    run_pipeline._write_manifest(path, [])
    assert path.read_bytes() == b"" and _tmp_files(tmp_path) == []


def test_p26_replaces_an_existing_manifest(tmp_path):
    path = tmp_path / "manifest.csv"
    path.write_bytes(OLD_MANIFEST)
    run_pipeline._write_manifest(path, P26_ROWS)
    assert path.read_bytes() == _p26_expected_bytes(P26_ROWS)
    assert _tmp_files(tmp_path) == []


class _P26FailingWriter(csv.DictWriter):
    """Writes the header and the first row, then fails -- a write interrupted midway."""

    exc = OSError(28, "No space left on device")

    def writerows(self, rows):
        self.writerow(rows[0])
        raise self.exc


@pytest.mark.parametrize("exc", [OSError(28, "No space left on device"), KeyboardInterrupt()])
def test_p26_failure_while_writing_keeps_existing_manifest_and_cleans_up(tmp_path, monkeypatch, exc):
    path = tmp_path / "manifest.csv"
    path.write_bytes(OLD_MANIFEST)
    monkeypatch.setattr(_P26FailingWriter, "exc", exc)
    monkeypatch.setattr(run_pipeline.csv, "DictWriter", _P26FailingWriter)
    with pytest.raises(type(exc)):
        run_pipeline._write_manifest(path, P26_ROWS)
    assert path.read_bytes() == OLD_MANIFEST  # untouched, not truncated
    assert _tmp_files(tmp_path) == [] and [p.name for p in tmp_path.iterdir()] == ["manifest.csv"]


def test_p26_failure_during_replace_keeps_existing_manifest_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "manifest.csv"
    path.write_bytes(OLD_MANIFEST)

    def _locked(src, dst):  # e.g. Windows: the manifest is open in Excel
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(run_pipeline.os, "replace", _locked)
    with pytest.raises(PermissionError):
        run_pipeline._write_manifest(path, P26_ROWS)
    assert path.read_bytes() == OLD_MANIFEST
    assert _tmp_files(tmp_path) == []


def test_p26_failure_without_existing_manifest_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(run_pipeline.csv, "DictWriter", _P26FailingWriter)
    with pytest.raises(OSError):
        run_pipeline._write_manifest(tmp_path / "PARTIAL - manifest.csv", P26_ROWS)
    assert list(tmp_path.iterdir()) == []  # no half-written manifest under a current-looking name


def test_p26_flush_and_fsync_happen_before_replace_into_the_same_folder(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = run_pipeline.os.fsync, run_pipeline.os.replace
    monkeypatch.setattr(run_pipeline.os, "fsync", lambda fd: events.append("fsync") or real_fsync(fd))

    def _replace(src, dst):
        events.append(("replace", Path(src).parent == Path(dst).parent, Path(src).name, Path(dst).name))
        return real_replace(src, dst)

    monkeypatch.setattr(run_pipeline.os, "replace", _replace)
    run_pipeline._write_manifest(tmp_path / "manifest.csv", P26_ROWS)
    assert events[0] == "fsync"
    _, same_dir, src_name, dst_name = events[1]
    assert same_dir and dst_name == "manifest.csv"
    assert src_name.startswith("manifest.csv.") and src_name.endswith(".tmp")


def test_p26_temp_names_are_collision_safe_and_unrelated_files_untouched(tmp_path, monkeypatch):
    unrelated = {"manifest.csv.keep.tmp": b"someone else's temp", "report.xlsx": b"R",
                 "STALE 20000101_000000 - manifest.csv": b"old stale", "notes.csv": b"n"}
    for name, data in unrelated.items():
        (tmp_path / name).write_bytes(data)
    seen = []
    real_mkstemp = run_pipeline.tempfile.mkstemp
    monkeypatch.setattr(run_pipeline.tempfile, "mkstemp",
                        lambda **kw: seen.append(real_mkstemp(**kw)) or seen[-1])
    run_pipeline._write_manifest(tmp_path / "manifest.csv", P26_ROWS)
    run_pipeline._write_manifest(tmp_path / "manifest.csv", P26_ROWS[:1])
    assert len({name for _, name in seen}) == 2  # a fresh unique name every write
    for name, data in unrelated.items():
        assert (tmp_path / name).read_bytes() == data
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*unrelated, "manifest.csv"])


# --- pipeline integration: normal / PARTIAL / SAMPLE / DRY RUN / STALE --------------------


def _p26_fail_replace_into(monkeypatch, target_name):
    # os is shared with report_builder/state, so only the named destination fails
    real_replace = run_pipeline.os.replace

    def _replace(src, dst):
        if Path(dst).name == target_name:
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(src, dst)

    monkeypatch.setattr(run_pipeline.os, "replace", _replace)


def test_p26_complete_run_writes_matching_manifest(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image("m0"), _make_image("mL", official_location="BRIDGE DECK")])
    settings = _settings(tmp_path)
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, _ScriptedClient(), force=False, sample=None,
                           flagged=_flagged(tmp_path))
    out = tmp_path / "output" / "Week 1"
    rows = _manifest_rows(out)
    assert [r["serial_number"] for r in rows] == ["1", "2"]
    assert list(rows[0]) == P26_HEADER
    assert _tmp_files(tmp_path) == []


@pytest.mark.parametrize("failing_writer", [True, False], ids=["write-fails", "replace-fails"])
def test_p26_complete_run_manifest_failure_preserves_previous_manifest_as_stale(tmp_path, monkeypatch,
                                                                                failing_writer):
    # Before P2-6 the old manifest was truncated in place, so the STALE copy P1-2 makes on a failed
    # week was a half-written file. Now the previous valid manifest survives byte-for-byte.
    out = _seed_old_report(tmp_path)
    _patch_extract_week(monkeypatch, [_make_image("f0"), _make_image("f1")])
    if failing_writer:
        monkeypatch.setattr(run_pipeline.csv, "DictWriter", _P26FailingWriter)
    else:
        _p26_fail_replace_into(monkeypatch, "manifest.csv")
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), _ScriptedClient(),
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert [f["week"] for f in results["failed"]] == ["Week 1"]  # isolated week failure, as before
    assert "manifest.csv" not in _files(out) and "report.xlsx" not in _files(out)  # nothing looks current
    [stale_manifest] = _stale(out, "manifest.csv")
    assert stale_manifest.read_bytes() == OLD_MANIFEST
    assert _tmp_files(tmp_path) == []


def test_p26_partial_manifest_written_atomically_and_failure_leaves_no_half_file(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"q{i}") for i in range(3)])
    out = _seed_old_report(tmp_path)
    run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), _P13Client({2: "quota20"}),
                           force=False, sample=None, flagged=_flagged(tmp_path))
    rows = _manifest_rows_named(out, "PARTIAL - manifest.csv")
    assert len(rows) == 3 and list(rows[0]) == P26_HEADER
    assert [p.read_bytes() for p in _stale(out, "manifest.csv")] == [OLD_MANIFEST]  # P1-2 unchanged
    assert _tmp_files(tmp_path) == []

    # second stop, this time the PARTIAL manifest cannot be put in place
    tmp2 = tmp_path / "second"
    out2 = _seed_old_report(tmp2)
    _p26_fail_replace_into(monkeypatch, "PARTIAL - manifest.csv")
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp2), _P13Client({1: "quota20"}),
                                     force=False, sample=None, flagged=_flagged(tmp2))
    assert results["stopped_early"]["week"] == "Week 1"  # still a clean quota stop
    assert "PARTIAL - manifest.csv" not in _files(out2)  # absent, never half-written
    assert [p.read_bytes() for p in _stale(out2, "manifest.csv")] == [OLD_MANIFEST]
    assert _tmp_files(tmp2) == []


def test_p26_sample_manifest_never_touches_the_complete_manifest(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"s{i}") for i in range(4)])
    out = _seed_old_report(tmp_path)
    run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), _ScriptedClient(), force=False,
                           sample=2, flagged=_flagged(tmp_path))
    assert len(_manifest_rows_named(out, "SAMPLE - manifest.csv")) == 2
    assert (out / "manifest.csv").read_bytes() == OLD_MANIFEST
    assert _tmp_files(tmp_path) == []


def test_p26_dry_run_manifest_is_isolated_and_atomic(tmp_path, monkeypatch):
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    _run_dry_main(tmp_path, monkeypatch)
    assert _snapshot(tmp_path) == before  # P0-1: the production manifest is untouched
    dry_out = tmp_path / "dry_run" / "output" / "Week 1"
    assert len(_manifest_rows(dry_out)) == 4
    assert _tmp_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Phase P2-8: Groq failure handling end to end. The REAL GroqVisionClient runs, but groq.Groq is
# replaced by an offline fake SDK (no SDK client, no key, no network); errors are real groq SDK
# exception objects. tmp_path only.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

P28_OK_JSON = ('{"person_visible": "No", "activity_identified": "No activity observed", '
               '"activity_confidence": "High", "ai_suggested_location": "Main Deck", '
               '"location_confidence": "High"}')


class _P28FakeSDK:
    """groq.Groq stand-in. `script` is shared by every instance (one run builds one client):
    an exception is raised, the string "ok" (or an exhausted script) returns a valid answer."""

    script: list = []
    calls = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs

        class _Completions:
            def create(self, **kw):
                _P28FakeSDK.calls += 1
                item = _P28FakeSDK.script.pop(0) if _P28FakeSDK.script else "ok"
                if isinstance(item, BaseException):
                    raise item
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=P28_OK_JSON))],
                                       usage=None)

        self.chat = SimpleNamespace(completions=_Completions())


@pytest.fixture
def p28_groq_client(monkeypatch):
    """make(*script) -> a real GroqVisionClient over the fake SDK, with backoff sleeps recorded."""
    import groq

    monkeypatch.setattr(groq, "Groq", _P28FakeSDK)
    monkeypatch.setattr(vision_client.GroqVisionClient._call.retry, "sleep", lambda s: None)

    def make(*script):
        _P28FakeSDK.script = list(script)
        _P28FakeSDK.calls = 0
        return vision_client.GroqVisionClient(model="qwen/qwen3.8-27b", api_key="fake-groq-key-p28")

    return make


def _p28_err(kind, retry_after=None):
    import groq
    import httpx

    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    if kind == "timeout":
        return groq.APITimeoutError(request=req)
    status, cls = {"429": (429, groq.RateLimitError), "503": (503, groq.InternalServerError),
                   "400": (400, groq.BadRequestError)}[kind]
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return cls(f"Error code: {status}", response=httpx.Response(status, request=req, headers=headers),
               body=None)


def _p28_settings(tmp_path):
    settings = _settings(tmp_path)
    settings.update({"provider": "groq", "model": "qwen/qwen3.8-27b"})
    return settings


def test_p28_groq_429_stops_the_run_with_partial_report_and_consistent_state(tmp_path, monkeypatch,
                                                                             p28_groq_client):
    _patch_extract_week(monkeypatch, [_make_image(f"g{i}") for i in range(4)])
    out = _seed_old_report(tmp_path)
    settings = _p28_settings(tmp_path)
    flagged = _flagged(tmp_path)
    client = p28_groq_client("ok", _p28_err("429", retry_after="7"))
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), settings, client,
                                     force=False, sample=None, flagged=flagged)
    assert _P28FakeSDK.calls == 2  # no request after the 429; Week 2 never attempted
    assert results["stopped_rate_limited"] == {"week": "Week 1", "kind": "ambiguous",
                                               "retry_delay_seconds": 7.0}
    assert results["stopped_early"] is None and results["stopped_unavailable"] is None
    assert [f["exception_type"] for f in results["failed"]] == ["GroqRateLimitedError"]
    assert results["weeks_not_attempted"] == ["Week 2"]
    assert results["images"] == _outcomes(classified=1, api_failure=1, unprocessed=2)
    # state: photo 1 done, photo 2 failed (retryable), photos 3-4 never attempted
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 1")
    assert sorted(e["ok"] for e in saved.values()) == [False, True]
    # P1-2: old files retired intact, PARTIAL written, nothing current-looking
    assert "report.xlsx" not in _files(out) and "manifest.csv" not in _files(out)
    assert [p.read_bytes() for p in _stale(out, "manifest.csv")] == [OLD_MANIFEST]
    assert _status_column(out / "PARTIAL - report.xlsx") == ["CLASSIFIED", "API_FAILURE",
                                                            "UNPROCESSED", "UNPROCESSED"]
    assert len(_manifest_rows_named(out, "PARTIAL - manifest.csv")) == 4
    status = _status_sheet(out / "PARTIAL - report.xlsx")
    assert status["Stop reason"] == "Groq HTTP 429: rate limit not identifiable as per-minute or daily"
    rows = _read_flagged_rows(flagged.write())
    assert [r["failure_type"] for r in rows] == ["API_FAILURE"]
    assert "fake-groq-key-p28" not in rows[0]["reason"]


def test_p28_rerun_after_groq_stop_retries_without_force_and_completes(tmp_path, monkeypatch,
                                                                       p28_groq_client):
    _patch_extract_week(monkeypatch, [_make_image(f"r{i}") for i in range(4)])
    settings = _p28_settings(tmp_path)
    out = tmp_path / "output" / "Week 1"
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, p28_groq_client("ok", _p28_err("429")),
                           force=False, sample=None, flagged=_flagged(tmp_path))
    assert "PARTIAL - report.xlsx" in _files(out)
    client = p28_groq_client()  # everything succeeds now
    results = run_pipeline.run_weeks(_week_folders("Week 1"), settings, client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _P28FakeSDK.calls == 3  # photo 1 was saved as ok -- not re-sent
    assert results["stopped_rate_limited"] is None and results["failed"] == []
    assert _status_sheet(out / "report.xlsx")["Report status"].startswith("COMPLETE")
    assert "PARTIAL - report.xlsx" not in _files(out) and _stale(out, "PARTIAL - report.xlsx")
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 1")
    assert [e["ok"] for e in saved.values()] == [True] * 4


@pytest.mark.parametrize("kind, calls, server_errors, timeouts",
                         [("503", 10, 2, 0), ("timeout", 2, 0, 2)])
def test_p28_groq_5xx_or_timeouts_trip_the_circuit_breaker(tmp_path, monkeypatch, p28_groq_client,
                                                           kind, calls, server_errors, timeouts):
    _patch_extract_week(monkeypatch, [_make_image(f"b{i}") for i in range(4)])
    settings = _p28_settings(tmp_path)
    client = p28_groq_client(*[_p28_err(kind) for _ in range(20)])
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), settings, client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    # bounded: 5 attempts per 5xx image, 1 per timeout, and only 2 images before the breaker
    assert _P28FakeSDK.calls == calls
    assert results["stopped_unavailable"] == {"week": "Week 1", "consecutive_failures": 2,
                                              "server_errors": server_errors, "timeouts": timeouts}
    assert [f["exception_type"] for f in results["failed"]] == ["ServiceUnavailableStop"]
    assert results["weeks_not_attempted"] == ["Week 2"]
    out = tmp_path / "output" / "Week 1"
    assert _status_column(out / "PARTIAL - report.xlsx") == ["API_FAILURE", "API_FAILURE",
                                                            "UNPROCESSED", "UNPROCESSED"]
    assert _status_sheet(out / "PARTIAL - report.xlsx")["Stop reason"].startswith(
        "Groq unavailable for 2 consecutive image(s)")
    saved = state.load_state(Path(settings["paths"]["state_dir"]), "Week 1")
    assert [e["ok"] for e in saved.values()] == [False, False]


def test_p28_groq_success_resets_the_breaker(tmp_path, monkeypatch, p28_groq_client):
    _patch_extract_week(monkeypatch, [_make_image(f"z{i}") for i in range(3)])
    client = p28_groq_client(_p28_err("timeout"), "ok", _p28_err("timeout"))
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _p28_settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert results["stopped_unavailable"] is None and _P28FakeSDK.calls == 3
    assert results["images"] == _outcomes(classified=1, api_failure=2)


def test_p28_groq_ordinary_failures_stay_per_image_and_the_week_completes(tmp_path, monkeypatch,
                                                                         p28_groq_client):
    _patch_extract_week(monkeypatch, [_make_image(f"o{i}") for i in range(3)])
    client = p28_groq_client(*[_p28_err("400") for _ in range(3)])
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _p28_settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _P28FakeSDK.calls == 3  # one request per image, never retried
    assert results["stopped_rate_limited"] is None and results["stopped_unavailable"] is None
    assert results["images"] == _outcomes(api_failure=3)
    assert _status_column(tmp_path / "output" / "Week 1" / "report.xlsx") == ["API_FAILURE"] * 3


def _p28_run_main(tmp_path, monkeypatch, caplog, client, argv=(), week_names=("Week 1", "Week 2")):
    # P2-9: a live Groq run requires its own pacing cap (the injected client is unaffected)
    settings = _main_settings(tmp_path, {"groq_requests_per_minute": 30})
    settings.update({"provider": "groq", "model": "qwen/qwen3.8-27b"})
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    monkeypatch.setattr(run_pipeline.discovery, "find_week_folders", lambda root: _week_folders(*week_names))
    monkeypatch.setattr(run_pipeline.vision_client, "build_vision_client", lambda *a, **kw: client)
    exit_code = None
    with caplog.at_level(logging.INFO, logger="pipeline"):
        try:
            run_pipeline.main()
        except SystemExit as exc:
            exit_code = exc.code
    return [r for r in caplog.records if r.name == "pipeline"], exit_code


@pytest.mark.parametrize("strict", [False, True])
def test_p28_groq_429_main_messages_name_groq_and_exit_codes_unchanged(tmp_path, monkeypatch, caplog,
                                                                       p28_groq_client, strict):
    _patch_extract_week(monkeypatch, [_make_image(f"m{i}") for i in range(3)])
    client = p28_groq_client("ok", _p28_err("429"))
    records, code = _p28_run_main(tmp_path, monkeypatch, caplog, client,
                                  ("--strict-exit",) if strict else ())
    msgs = _msgs(records)
    stop = [m for m in msgs if m.startswith("Run stopped early:")]
    assert len(stop) == 1 and stop[0].startswith(
        "Run stopped early: Groq returned HTTP 429 (rate limit not identifiable as per-minute or daily)")
    assert "provider-suggested wait: not provided" in stop[0]
    assert not any("Gemini" in m for m in msgs)  # no Gemini wording for a Groq run
    assert _P28FakeSDK.calls == 2
    if strict:
        assert code == run_pipeline.STRICT_EXIT_CODE
        line = _strict_line(records)
        assert "HTTP 429 (rate limit not identifiable as per-minute or daily) in Week 1" in line
        assert "1 week(s) not attempted" in line
    else:
        assert code is None  # default exit 0, exactly as for Gemini stops


def test_p28_groq_breaker_strict_exit_names_groq(tmp_path, monkeypatch, caplog, p28_groq_client):
    _patch_extract_week(monkeypatch, [_make_image(f"k{i}") for i in range(3)])
    client = p28_groq_client(_p28_err("timeout"), _p28_err("timeout"))
    records, code = _p28_run_main(tmp_path, monkeypatch, caplog, client, ("--strict-exit",))
    assert code == run_pipeline.STRICT_EXIT_CODE
    assert "circuit breaker tripped (Groq unavailable) in Week 1" in _strict_line(records)
    assert any(m.startswith("Run stopped early: Groq was unavailable for 2 consecutive image(s) "
                            "(0 server error(s) after all retries, 2 request timeout(s))")
               for m in _msgs(records))


def test_p28_provider_label_and_strict_reasons_default_to_gemini():
    assert run_pipeline._provider_label({}) == "Gemini"
    assert run_pipeline._provider_label({"provider": "gemini"}) == "Gemini"
    assert run_pipeline._provider_label({"provider": " Groq "}) == "Groq"
    results = {"stopped_unavailable": {"week": "Week 1"}, "failed": [{"week": "Week 1"}]}
    assert run_pipeline._strict_exit_reasons(results) == [
        "circuit breaker tripped (Gemini unavailable) in Week 1"]
    assert run_pipeline._strict_exit_reasons(results, "Groq") == [
        "circuit breaker tripped (Groq unavailable) in Week 1"]


def test_p28_gemini_rate_limit_stop_keeps_gemini_type_and_wording(tmp_path, monkeypatch, caplog):
    # regression: an explicit provider "gemini" run is byte-for-byte the P1-1 behaviour
    _patch_extract_week(monkeypatch, [_make_image(f"q{i}") for i in range(3)])
    client = _RateLimitScriptedClient({2: "amb"})
    records, code = _run_main_with_client(tmp_path, monkeypatch, caplog, client, ("Week 1", "Week 2"))
    assert code is None and client.calls == 2
    msgs = _msgs(records)
    assert any(m.startswith("Run stopped early: Gemini returned HTTP 429 (rate limit not identifiable "
                            "as per-minute or daily) while processing Week 1.") for m in msgs)
    assert any("Week 1: Gemini rate limit (rate limit not identifiable as per-minute or daily) -- "
               "stopping the run" in m for m in msgs)
    assert not any("Groq" in m for m in msgs)


# ---------------------------------------------------------------------------
# Phase P2-9: Groq pacing inside a real pipeline run (fake SDK, fake monotonic clock).
# ---------------------------------------------------------------------------


def _p29_paced_client(monkeypatch, rpm, *script):
    import groq

    clock = {"t": 500.0, "sleeps": []}
    monkeypatch.setattr(vision_client.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(groq, "Groq", _P28FakeSDK)
    monkeypatch.setattr(vision_client.GroqVisionClient._call.retry, "sleep", lambda s: None)
    _P28FakeSDK.script = list(script)
    _P28FakeSDK.calls = 0
    client = vision_client.GroqVisionClient(model="qwen/qwen3.8-27b", api_key="fake-groq-key-p29",
                                            requests_per_minute=rpm)

    def _sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["t"] += seconds

    client._pace_sleep = _sleep
    return client, clock


def test_p29_paced_groq_run_spaces_photos_and_completes(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"p{i}") for i in range(4)])
    client, clock = _p29_paced_client(monkeypatch, 15)   # 4s interval
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _p28_settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _P28FakeSDK.calls == 4 and results["images"] == _outcomes(classified=4)
    assert clock["sleeps"] == [4.0, 4.0, 4.0]  # frozen clock: a full gap before photos 2-4 only
    # a rerun sends nothing, so it never waits
    clock["sleeps"].clear()
    run_pipeline.run_weeks(_week_folders("Week 1"), _p28_settings(tmp_path), client,
                           force=False, sample=None, flagged=_flagged(tmp_path))
    assert _P28FakeSDK.calls == 4 and clock["sleeps"] == []


def test_p29_paced_groq_429_still_stops_the_run_with_partial_report(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"s{i}") for i in range(4)])
    client, clock = _p29_paced_client(monkeypatch, 30, "ok", _p28_err("429"))
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _p28_settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _P28FakeSDK.calls == 2 and clock["sleeps"] == [2.0]
    assert results["stopped_rate_limited"]["kind"] == "ambiguous"
    assert results["weeks_not_attempted"] == ["Week 2"]
    assert "PARTIAL - report.xlsx" in _files(tmp_path / "output" / "Week 1")


def test_p29_paced_groq_breaker_unchanged(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"c{i}") for i in range(4)])
    client, clock = _p29_paced_client(monkeypatch, 60, *[_p28_err("503") for _ in range(20)])
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _p28_settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert results["stopped_unavailable"] == {"week": "Week 1", "consecutive_failures": 2,
                                              "server_errors": 2, "timeouts": 0}
    assert _P28FakeSDK.calls == 10                # 2 photos x 5 attempts, as in P2-8
    assert clock["sleeps"] == [1.0] * 9           # every attempt after the first was paced


def test_p29_dry_run_with_groq_provider_never_paces(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"d{i}") for i in range(3)])
    monkeypatch.setattr(vision_client.time, "monotonic",
                        lambda: pytest.fail("a dry run must not touch the pacing clock"))
    client = vision_client.build_vision_client("groq", "qwen/qwen3.8-27b", dry_run=True, rate_limit=None)
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _p28_settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert results["images"] == _outcomes(dry_run=3)


# ---------------------------------------------------------------------------
# Phase P2-10: a report (or manifest) locked by another program (Excel). Locks are simulated by
# a mocked os.replace and, on Windows, by a REAL open file handle (Windows refuses to replace or
# rename a file another handle has open -- the same failure an open Excel workbook causes).
# tmp_path only; scripted clients; no network, no .env.
# ---------------------------------------------------------------------------

P210_SUFFIX = " - OUT OF DATE - DO NOT USE.txt"


def _p210_lock(monkeypatch, *paths, mode="both"):
    """Simulate a Windows lock on `paths`: mode "both" -- any replace onto or away from them fails
    (what Excel causes); "replace" -- only replacing ONTO them fails (renaming away still works)."""
    locked = {Path(p).resolve() for p in paths}
    real_replace = run_pipeline.os.replace

    def _replace(src, dst):
        onto, away = Path(dst).resolve() in locked, Path(src).resolve() in locked
        if onto or (mode == "both" and away):
            raise PermissionError(13, "The process cannot access the file because it is being "
                                      "used by another process", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(run_pipeline.os, "replace", _replace)


def _p210_markers(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir() if p.name.endswith(P210_SUFFIX))


def _p210_run(tmp_path, client, weeks=("Week 1",), sample=None, settings=None):
    return run_pipeline.run_weeks(_week_folders(*weeks), settings or _settings(tmp_path), client,
                                  force=False, sample=sample, flagged=_flagged(tmp_path))


def test_p210_normal_replacement_succeeds_and_leaves_no_marker(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"n{i}") for i in range(2)])
    out = _seed_old_report(tmp_path)
    results = _p210_run(tmp_path, _ScriptedClient())
    assert results["failed"] == [] and results["outdated_files"] == []
    assert _status_sheet(out / "report.xlsx")["Report status"].startswith("COMPLETE")
    assert (out / "report.xlsx").read_bytes() != OLD_REPORT and _p210_markers(out) == []
    assert run_pipeline._strict_exit_reasons(results) == []


def test_p210_replace_blocked_but_rename_works_retires_old_report_no_marker(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"r{i}") for i in range(2)])
    out = _seed_old_report(tmp_path)
    _p210_lock(monkeypatch, out / "report.xlsx", mode="replace")
    results = _p210_run(tmp_path, _ScriptedClient())
    assert [f["week"] for f in results["failed"]] == ["Week 1"]  # never a silent success
    assert results["failed"][0]["exception_type"] == "ReportGenerationError"
    assert "report.xlsx" not in _files(out)  # nothing current-looking is left
    assert [p.read_bytes() for p in _stale(out, "report.xlsx")] == [OLD_REPORT]
    assert _p210_markers(out) == [] and results["outdated_files"] == []
    assert not list(out.glob("*.tmp"))


def test_p210_locked_report_on_complete_run_keeps_bytes_and_writes_marker(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(f"b{i}") for i in range(2)])
    out = _seed_old_report(tmp_path)
    _p210_lock(monkeypatch, out / "report.xlsx")  # replacement AND stale rename both fail
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        results = _p210_run(tmp_path, _ScriptedClient())
    assert [f["week"] for f in results["failed"]] == ["Week 1"]
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT  # byte-for-byte, under its old name
    assert [p.read_bytes() for p in _stale(out, "manifest.csv")] == [OLD_MANIFEST]  # others still retired
    assert _p210_markers(out) == ["report.xlsx" + P210_SUFFIX]
    assert results["outdated_files"] == [str(out / "report.xlsx")]
    assert not list(out.glob("*.tmp"))
    text = " ".join(_msgs(caplog.records))
    assert "report.xlsx is OUT OF DATE" in text and "do NOT use it" in text
    reasons = run_pipeline._strict_exit_reasons(results)
    assert any("1 week(s) failed" in r for r in reasons)
    assert any("1 report file(s) OUT OF DATE under their normal name" in r for r in reasons)


def test_p210_marker_content_is_explicit(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image("c0")])
    out = _seed_old_report(tmp_path)
    _p210_lock(monkeypatch, out / "report.xlsx")
    _p210_run(tmp_path, _ScriptedClient())
    text = (out / ("report.xlsx" + P210_SUFFIX)).read_text(encoding="utf-8")
    for phrase in ('"report.xlsx" in this folder is OUT OF DATE -- do NOT use it.', "Week: Week 1",
                   "PermissionError", "open in Excel", "left untouched",
                   "close the file", "rerun the same command", "no --force",
                   "removed automatically"):
        assert phrase in text, phrase
    assert not list(out.glob("*.tmp"))


@pytest.mark.skipif(sys.platform != "win32", reason="real file locking semantics are Windows-only")
def test_p210_real_windows_lock_open_handle(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"w{i}") for i in range(2)])
    out = _seed_old_report(tmp_path)
    with (out / "report.xlsx").open("rb"):  # held open, exactly like Excel holding the workbook
        results = _p210_run(tmp_path, _ScriptedClient())
    assert [f["week"] for f in results["failed"]] == ["Week 1"]
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT
    assert _p210_markers(out) == ["report.xlsx" + P210_SUFFIX]
    assert not list(out.glob("*.tmp"))


def test_p210_stop_with_locked_report_still_stops_and_writes_partial(tmp_path, monkeypatch):
    # The quota stop must never be replaced by the rename error: before P2-10 the PermissionError
    # escaped stop_with_partial_report(), the week counted as an ordinary failure, and the NEXT
    # week was processed -- sending more requests after a confirmed daily-quota exhaustion.
    _patch_extract_week(monkeypatch, [_make_image(f"q{i}") for i in range(3)])
    out = _seed_old_report(tmp_path)
    _p210_lock(monkeypatch, out / "report.xlsx")
    client = _P13Client({2: "quota20"})
    results = _p210_run(tmp_path, client, weeks=("Week 1", "Week 2"))
    assert client.calls == 2 and results["stopped_early"]["week"] == "Week 1"
    assert results["weeks_not_attempted"] == ["Week 2"]
    assert [f["exception_type"] for f in results["failed"]] == ["GeminiQuotaExhaustedError"]
    assert _status_column(out / "PARTIAL - report.xlsx") == ["CLASSIFIED", "API_FAILURE", "UNPROCESSED"]
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT
    assert [p.read_bytes() for p in _stale(out, "manifest.csv")] == [OLD_MANIFEST]
    assert _p210_markers(out) == ["report.xlsx" + P210_SUFFIX]
    assert results["quota_stop_details"]["stale_files"] == [_stale(out, "manifest.csv")[0].name]
    assert results["outdated_files"] == [str(out / "report.xlsx")]


def test_p210_later_successful_run_clears_the_marker(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"l{i}") for i in range(2)])
    out = _seed_old_report(tmp_path)
    with monkeypatch.context() as m:
        _p210_lock(m, out / "report.xlsx")
        _p210_run(tmp_path, _ScriptedClient())
    assert _p210_markers(out) == ["report.xlsx" + P210_SUFFIX]
    client = _ScriptedClient()  # Excel closed: the lock is gone
    results = _p210_run(tmp_path, client)
    assert client.calls == 0  # every photo was already saved as ok: no requests repeated
    assert results["failed"] == [] and results["outdated_files"] == []
    assert _status_sheet(out / "report.xlsx")["Report status"].startswith("COMPLETE")
    assert _p210_markers(out) == []
    assert [p.read_bytes() for p in _stale(out, "manifest.csv")] == [OLD_MANIFEST]  # history kept


def test_p210_later_stopped_run_renames_the_old_report_and_clears_the_marker(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"s{i}") for i in range(3)])
    out = _seed_old_report(tmp_path)
    with monkeypatch.context() as m:
        _p210_lock(m, out / "report.xlsx")
        _p210_run(tmp_path, _P13Client({2: "quota20"}))  # stop: photos 2-3 still need requests
    assert _p210_markers(out) == ["report.xlsx" + P210_SUFFIX]
    results = _p210_run(tmp_path, _P13Client({1: "quota20"}))  # lock released, quota stops again
    assert results["stopped_early"]["week"] == "Week 1"
    assert [p.read_bytes() for p in _stale(out, "report.xlsx")] == [OLD_REPORT]
    assert "report.xlsx" not in _files(out) and _p210_markers(out) == []
    assert results["outdated_files"] == []


def test_p210_locked_leftover_partial_after_completion_is_flagged(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"p{i}") for i in range(3)])
    out = tmp_path / "output" / "Week 1"
    _p210_run(tmp_path, _P13Client({2: "quota20"}))
    partial_bytes = (out / "PARTIAL - report.xlsx").read_bytes()
    _p210_lock(monkeypatch, out / "PARTIAL - report.xlsx")
    results = _p210_run(tmp_path, _ScriptedClient())
    assert results["failed"] == []  # the week itself finished: the complete report is current
    assert _status_sheet(out / "report.xlsx")["Report status"].startswith("COMPLETE")
    assert (out / "PARTIAL - report.xlsx").read_bytes() == partial_bytes
    assert _p210_markers(out) == ["PARTIAL - report.xlsx" + P210_SUFFIX]
    assert _stale(out, "PARTIAL - manifest.csv")  # its unlocked sibling was still retired
    assert results["outdated_files"] == [str(out / "PARTIAL - report.xlsx")]
    assert any("OUT OF DATE" in r for r in run_pipeline._strict_exit_reasons(results))


def test_p210_sample_run_never_touches_a_locked_complete_report(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"x{i}") for i in range(3)])
    out = _seed_old_report(tmp_path)
    _p210_lock(monkeypatch, out / "report.xlsx", out / "manifest.csv")
    results = _p210_run(tmp_path, _ScriptedClient(), sample=2)
    assert results["failed"] == [] and results["outdated_files"] == []
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT and _p210_markers(out) == []
    assert len(_manifest_rows_named(out, "SAMPLE - manifest.csv")) == 2


def test_p210_locked_sample_report_gets_its_own_marker(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"y{i}") for i in range(3)])
    out = _seed_old_report(tmp_path)
    _p210_run(tmp_path, _ScriptedClient(), sample=2)
    sample_bytes = (out / "SAMPLE - report.xlsx").read_bytes()
    _p210_lock(monkeypatch, out / "SAMPLE - report.xlsx")
    # a larger sample: photo 3 needs a request, and the quota stops it -> SAMPLE family retired
    results = _p210_run(tmp_path, _P13Client({1: "quota20"}), sample=3)
    assert results["stopped_early"]["week"] == "Week 1"
    assert (out / "SAMPLE - report.xlsx").read_bytes() == sample_bytes
    assert _p210_markers(out) == ["SAMPLE - report.xlsx" + P210_SUFFIX]
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT  # the complete family is untouched


def test_p210_dry_run_with_locked_production_report_is_isolated(tmp_path, monkeypatch):
    _seed_production(tmp_path)
    before = _snapshot(tmp_path)
    _p210_lock(monkeypatch, tmp_path / "output" / "Week 1" / "report.xlsx")
    _run_dry_main(tmp_path, monkeypatch)
    assert _snapshot(tmp_path) == before
    assert _p210_markers(tmp_path / "output" / "Week 1") == []
    assert _p210_markers(tmp_path / "dry_run" / "output" / "Week 1") == []


def test_p210_unrelated_files_untouched(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"u{i}") for i in range(2)])
    out = _seed_old_report(tmp_path)
    unrelated = {"notes.txt": b"n", "other.xlsx - OUT OF DATE - DO NOT USE.txt": b"someone else's",
                 "STALE 20000101_000000 - report.xlsx": b"old stale", "report.xlsx.keep.tmp": b"t"}
    for name, data in unrelated.items():
        (out / name).write_bytes(data)
    with monkeypatch.context() as m:
        _p210_lock(m, out / "report.xlsx")
        _p210_run(tmp_path, _ScriptedClient())
    _p210_run(tmp_path, _ScriptedClient())  # recovery run clears only OUR marker
    for name, data in unrelated.items():
        assert (out / name).read_bytes() == data, name
    assert _p210_markers(out) == ["other.xlsx - OUT OF DATE - DO NOT USE.txt"]


def test_p210_marker_write_failure_never_masks_the_original_problem(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, [_make_image(f"f{i}") for i in range(3)])
    out = _seed_old_report(tmp_path)
    _p210_lock(monkeypatch, out / "report.xlsx", out / ("report.xlsx" + P210_SUFFIX))
    client = _P13Client({2: "quota20"})
    with caplog.at_level(logging.ERROR, logger="pipeline"):
        results = _p210_run(tmp_path, client, weeks=("Week 1", "Week 2"))
    assert client.calls == 2 and results["stopped_early"]["week"] == "Week 1"  # stop preserved
    assert (out / "report.xlsx").read_bytes() == OLD_REPORT
    assert results["outdated_files"] == [str(out / "report.xlsx")]  # still reported
    assert any("warning file could not be written" in m for m in _msgs(caplog.records))
    assert not list(out.glob("*.tmp"))


@pytest.mark.parametrize("strict", [False, True])
def test_p210_main_reports_outdated_files_and_strict_exit(tmp_path, monkeypatch, caplog, strict):
    _patch_extract_week(monkeypatch, [_make_image(f"m{i}") for i in range(2)])
    out = _seed_old_report(tmp_path)
    _p210_lock(monkeypatch, out / "report.xlsx")
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, _ScriptedClient(),
                                   ("--strict-exit",) if strict else ())
    summary = [m for m in _msgs(records, logging.ERROR) if m.startswith("1 report file(s) are OUT OF DATE")]
    assert len(summary) == 1 and "report.xlsx" in summary[0] and P210_SUFFIX.strip() in summary[0]
    if strict:
        assert code == run_pipeline.STRICT_EXIT_CODE
        assert "1 report file(s) OUT OF DATE under their normal name" in _strict_line(records)
    else:
        assert code is None  # default exit behaviour unchanged (a failed week already exits 0)



# --- Phase P2-12: processing.force_reprocess removed; only --force reprocesses -----------------


def _p212_production_code_lines() -> list[tuple[str, str]]:
    """(file, line) for every non-comment line of the production modules."""
    root = Path(run_pipeline.__file__).parent
    files = [root / "run_pipeline.py", *sorted((root / "src").glob("*.py"))]
    return [(f.name, line) for f in files
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_p212_no_production_code_reads_force_reprocess():
    hits = [(name, line) for name, line in _p212_production_code_lines() if "force_reprocess" in line]
    assert hits == []


def _p212_images(n=3):
    return [_make_image(f"p212-{i}") for i in range(n)]


def test_p212_default_rerun_resumes_from_state_without_new_requests(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, _p212_images())
    first = _ScriptedClient()
    _, code = _run_main_argv(tmp_path, monkeypatch, caplog, first)
    assert code is None and first.calls == 3
    again = _ScriptedClient()
    _, code = _run_main_argv(tmp_path, monkeypatch, caplog, again)
    assert code is None and again.calls == 0  # every photo is done in state: nothing re-sent


def test_p212_force_cli_option_still_reclassifies_every_photo(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, _p212_images())
    first = _ScriptedClient()
    _run_main_argv(tmp_path, monkeypatch, caplog, first)
    assert first.calls == 3
    forced = _ScriptedClient()
    _, code = _run_main_argv(tmp_path, monkeypatch, caplog, forced, ["--force"])
    assert code is None and forced.calls == 3  # --force re-sends every AI-assessable photo
    after = _ScriptedClient()
    _run_main_argv(tmp_path, monkeypatch, caplog, after)
    assert after.calls == 0  # --force is per run only; the next plain run resumes from state


def test_p212_force_flag_reaches_run_weeks_only_from_the_cli(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, _p212_images())
    seen, real_run_weeks = [], run_pipeline.run_weeks

    def recording_run_weeks(weeks, settings, client, force, sample, flagged):
        seen.append(force)
        return real_run_weeks(weeks, settings, client, force, sample, flagged)

    monkeypatch.setattr(run_pipeline, "run_weeks", recording_run_weeks)
    _run_main_argv(tmp_path, monkeypatch, caplog, _ScriptedClient())
    _run_main_argv(tmp_path, monkeypatch, caplog, _ScriptedClient(), ["--force"])
    assert seen == [False, True]


def test_p212_leftover_force_reprocess_true_in_old_settings_does_not_force(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, _p212_images())
    settings = _settings(tmp_path)
    first = _ScriptedClient()
    run_pipeline.run_weeks(_week_folders("Week 1"), settings, first, force=False, sample=None,
                           flagged=_flagged(tmp_path))
    assert first.calls == 3
    old_style = _settings(tmp_path)
    old_style["processing"]["force_reprocess"] = True  # a pre-P2-12 settings file
    again = _ScriptedClient()
    results = run_pipeline.run_weeks(_week_folders("Week 1"), old_style, again, force=False,
                                     sample=None, flagged=_flagged(tmp_path))
    assert again.calls == 0 and results["failed"] == []


def test_p212_dry_run_unchanged_without_force_reprocess(tmp_path, monkeypatch, caplog):
    _patch_extract_week(monkeypatch, _p212_images())
    # the real dry-run client (offline placeholder; no key, no network)
    client = vision_client.build_vision_client("gemini", "gemini-3.8-flash", dry_run=True)
    assert isinstance(client, vision_client.NullVisionClient)
    _, code = _run_main_argv(tmp_path, monkeypatch, caplog, client, ["--dry-run"])
    assert code is None
    assert list((tmp_path / "dry_run").rglob("DRY RUN - report.xlsx"))
    assert not (tmp_path / "output").exists() and not (tmp_path / ".state").exists()
    real = _ScriptedClient()  # a dry run never counts as done: the first live run sends all 3
    _run_main_argv(tmp_path, monkeypatch, caplog, real)
    assert real.calls == 3



# --- M1 / M2 / L2 / L4: report status, permanent provider errors, live-script opt-in, Gemini 429 --
import importlib.util  # noqa: E402
import json  # noqa: E402

M12_FAILURES = "API FAILURE(S) – not all photos classified"
M12_GEMINI_KEY = "fake-gemini-key-m12-SECRET"
M12_GROQ_KEY = "fake-groq-key-p28"  # the p28_groq_client fixture's key


def _m12_rows(*statuses):
    return [SimpleNamespace(classification_status=s) for s in statuses]


# M1 --------------------------------------------------------------------------------------------


def test_m1_complete_without_failures_is_plain_complete():
    rows = dict(run_pipeline._processing_status_rows("Week 1", _m12_rows("CLASSIFIED", "VISUAL_REVIEW"), 2, None))
    assert rows["Report status"] == "COMPLETE"


def test_m1_complete_with_failures_says_so_and_counts_them():
    rows = dict(run_pipeline._processing_status_rows(
        "Week 1", _m12_rows("CLASSIFIED", "API_FAILURE", "API_FAILURE"), 3, None))
    assert rows["Report status"] == f"COMPLETE WITH 2 {M12_FAILURES}"
    assert "API_FAILURE: 2" in rows["Rows by Classification Status"]


def test_m1_stopped_run_is_partial_even_with_failures():
    rows = dict(run_pipeline._processing_status_rows(
        "Week 1", _m12_rows("API_FAILURE", "UNPROCESSED"), 2, None, stop_reason="quota"))
    assert rows["Report status"] == "PARTIAL - processing stopped early; this is NOT a complete report"


def test_m1_sample_statuses_mention_failures_only_when_present():
    clean = dict(run_pipeline._processing_status_rows("Week 1", _m12_rows("CLASSIFIED"), 4, 1))
    assert clean["Report status"] == ("COMPLETE FOR A SAMPLE ONLY - first 1 of 4 photos (--sample); "
                                      "not a full-week report")
    failed = dict(run_pipeline._processing_status_rows("Week 1", _m12_rows("API_FAILURE"), 1, 5))
    assert failed["Report status"].startswith("COMPLETE FOR A SAMPLE RUN - --sample 5 covered all 1 photos")
    assert failed["Report status"].endswith(f"; WITH 1 {M12_FAILURES}")


def test_m1_no_status_claims_this_run_processed_every_photo():
    for sample, stop in ((None, None), (None, "x"), (1, None), (9, None)):
        status = dict(run_pipeline._processing_status_rows("W", _m12_rows("CLASSIFIED"), 1, sample, stop))
        assert "processed by this run" not in status["Report status"]


def test_m1_pipeline_report_with_an_api_failure_is_not_plain_complete(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image(f"m1-{i}") for i in range(3)])
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), _ScriptedClient({2: "err"}),
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    out = tmp_path / "output" / "Week 1"
    assert results["failed"] == [] and results["stopped_provider_error"] is None
    assert _status_sheet(out / "report.xlsx")["Report status"] == f"COMPLETE WITH 1 {M12_FAILURES}"
    header, rows = _report_table(out / "report.xlsx")
    assert header == EXPECTED_HEADERS  # the 11-column Image Register is unchanged
    assert sorted(r["Classification Status"] for r in rows) == ["API_FAILURE", "CLASSIFIED", "CLASSIFIED"]


# M2 -- Gemini (fake google.genai.Client: no real SDK client, no network) ---------------------


class _M12FakeGenai:
    """google.genai.Client stand-in. `script` items: an exception is raised, anything else
    returns a valid structured answer. Shared by every instance (one run builds one client)."""

    script: list = []
    calls = 0

    def __init__(self, **kwargs):
        self.models = SimpleNamespace(generate_content=self._generate)

    @staticmethod
    def _generate(**kwargs):
        _M12FakeGenai.calls += 1
        item = _M12FakeGenai.script.pop(0) if _M12FakeGenai.script else "ok"
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(parsed=json.loads(P28_OK_JSON), usage_metadata=None, text=None)


@pytest.fixture
def m12_gemini_client(monkeypatch):
    from google import genai

    monkeypatch.setattr(genai, "Client", _M12FakeGenai)

    def make(*script):
        _M12FakeGenai.script = list(script)
        _M12FakeGenai.calls = 0
        client = vision_client.GeminiVisionClient(model="gemini-3.8-flash", api_key=M12_GEMINI_KEY)
        client._call.retry.sleep = lambda seconds: None  # no real waits
        return client

    return make


def _m12_gemini_error(code, status, message="error", details=None):
    from google.genai import errors

    body = {"error": {"code": code, "status": status, "message": f"{message} (key={M12_GEMINI_KEY})"}}
    if details is not None:
        body["error"]["details"] = details
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code=code, response_json=body)


def _m12_assert_provider_stop(tmp_path, results, flagged, kind, status_code, exc_type, provider_calls):
    assert provider_calls == 1  # the error costs exactly one request; nothing after it
    assert results["stopped_provider_error"] == {"week": "Week 1", "kind": kind, "status_code": status_code}
    assert results["stopped_early"] is None and results["stopped_rate_limited"] is None
    assert results["stopped_unavailable"] is None
    assert [f["exception_type"] for f in results["failed"]] == [exc_type]
    assert results["weeks_not_attempted"] == ["Week 2"]
    assert results["images"] == _outcomes(api_failure=1, unprocessed=2)
    saved = state.load_state(tmp_path / ".state", "Week 1")
    assert [e["ok"] for e in saved.values()] == [False]  # the failed photo is saved, retryable
    out = tmp_path / "output" / "Week 1"
    assert _status_sheet(out / "PARTIAL - report.xlsx")["Report status"].startswith("PARTIAL")
    assert not (out / "report.xlsx").exists()
    assert not (tmp_path / "output" / "Week 2").exists()
    rows = _read_flagged_rows(flagged.write())
    assert [r["failure_type"] for r in rows] == ["API_FAILURE"]
    assert "REDACTED" in rows[0]["reason"]
    provider = "Gemini" if exc_type.startswith("Gemini") else "Groq"
    assert run_pipeline._strict_exit_reasons(results, provider)[0].startswith(f"{provider} provider error")


@pytest.mark.parametrize("code,status,kind", [(401, "UNAUTHENTICATED", "authentication"),
                                              (403, "PERMISSION_DENIED", "permission"),
                                              (404, "NOT_FOUND", "not_found")])
def test_m2_gemini_401_403_404_stop_the_run_after_one_request(tmp_path, monkeypatch, caplog,
                                                              m12_gemini_client, code, status, kind):
    _patch_extract_week(monkeypatch, [_make_image(f"m2g-{code}-{i}") for i in range(3)])
    flagged = _flagged(tmp_path)
    client = m12_gemini_client(_m12_gemini_error(code, status))
    with caplog.at_level(logging.INFO, logger="pipeline"):
        results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                         force=False, sample=None, flagged=flagged)
    _m12_assert_provider_stop(tmp_path, results, flagged, kind, code, "GeminiPermanentError",
                              _M12FakeGenai.calls)
    assert M12_GEMINI_KEY not in caplog.text  # never logged


@pytest.mark.parametrize("details,status,kind", [
    ([{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}],
     "INVALID_ARGUMENT", "authentication"),
    (None, "FAILED_PRECONDITION", "configuration"),
])
def test_m2_gemini_400_with_confirmed_provider_wide_cause_stops(tmp_path, monkeypatch, m12_gemini_client,
                                                                details, status, kind):
    _patch_extract_week(monkeypatch, [_make_image(f"m2g400-{kind}-{i}") for i in range(3)])
    flagged = _flagged(tmp_path)
    client = m12_gemini_client(_m12_gemini_error(400, status, details=details))
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                     force=False, sample=None, flagged=flagged)
    _m12_assert_provider_stop(tmp_path, results, flagged, kind, 400, "GeminiPermanentError",
                              _M12FakeGenai.calls)


def test_m2_gemini_ordinary_400_stays_a_per_photo_failure(tmp_path, monkeypatch, m12_gemini_client):
    _patch_extract_week(monkeypatch, [_make_image(f"m2g400plain-{i}") for i in range(3)])
    client = m12_gemini_client(_m12_gemini_error(400, "INVALID_ARGUMENT", "Unable to process input image"))
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _M12FakeGenai.calls == 6  # every photo of both weeks was still attempted
    assert results["stopped_provider_error"] is None and results["failed"] == []
    assert results["images"] == _outcomes(classified=5, api_failure=1)
    out = tmp_path / "output" / "Week 1"
    assert _status_sheet(out / "report.xlsx")["Report status"] == f"COMPLETE WITH 1 {M12_FAILURES}"


def test_m2_gemini_permanent_kind_is_conservative():
    from google.genai import errors

    kind = vision_client._gemini_permanent_kind
    assert kind(_m12_gemini_error(400, "INVALID_ARGUMENT")) is None
    assert kind(_m12_gemini_error(400, "INVALID_ARGUMENT", details=[{"@type": "x.ErrorInfo",
                                                                      "reason": "OTHER"}])) is None
    assert kind(_m12_gemini_error(429, "RESOURCE_EXHAUSTED")) is None
    assert kind(_m12_gemini_error(503, "UNAVAILABLE")) is None
    assert kind(RuntimeError("401 Unauthorized")) is None  # never inferred from text
    malformed = errors.ClientError(code=400, response_json={"error": {"code": 400, "details": "oops"}})
    assert kind(malformed) is None  # malformed details never raise


def test_m2_gemini_permanent_error_is_never_retried(m12_gemini_client):
    client = m12_gemini_client(_m12_gemini_error(403, "PERMISSION_DENIED"))
    with pytest.raises(vision_client.GeminiPermanentError) as info:
        client.classify(_fake_jpeg_bytes("m2-retry"))
    assert _M12FakeGenai.calls == 1
    assert info.value.kind == "permission" and info.value.status_code == 403
    assert M12_GEMINI_KEY not in str(info.value) and "REDACTED" in str(info.value)
    assert isinstance(info.value, vision_client.ProviderPermanentError)
    assert not isinstance(info.value, (vision_client.ProviderRateLimitedError,
                                       vision_client.ProviderServiceUnavailableError))


# M2 -- Groq (the P2-8 fake groq SDK) -----------------------------------------------------------


def _m12_groq_error(status):
    import groq
    import httpx

    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    cls = {401: groq.AuthenticationError, 403: groq.PermissionDeniedError, 404: groq.NotFoundError,
           400: groq.BadRequestError}[status]
    return cls(f"Error code: {status} (key={M12_GROQ_KEY})",
               response=httpx.Response(status, request=req), body=None)


@pytest.mark.parametrize("code,kind", [(401, "authentication"), (403, "permission"), (404, "not_found")])
def test_m2_groq_401_403_404_stop_the_run_after_one_request(tmp_path, monkeypatch, caplog, p28_groq_client,
                                                            code, kind):
    _patch_extract_week(monkeypatch, [_make_image(f"m2q-{code}-{i}") for i in range(3)])
    flagged = _flagged(tmp_path)
    client = p28_groq_client(_m12_groq_error(code))
    with caplog.at_level(logging.INFO, logger="pipeline"):
        results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _p28_settings(tmp_path), client,
                                         force=False, sample=None, flagged=flagged)
    _m12_assert_provider_stop(tmp_path, results, flagged, kind, code, "GroqPermanentError",
                              _P28FakeSDK.calls)
    assert M12_GROQ_KEY not in caplog.text
    assert run_pipeline._strict_exit_reasons(results, "Groq")[0].startswith("Groq provider error")


def test_m2_groq_400_stays_a_per_photo_failure(tmp_path, monkeypatch, p28_groq_client):
    _patch_extract_week(monkeypatch, [_make_image(f"m2q400-{i}") for i in range(3)])
    client = p28_groq_client(_m12_groq_error(400))
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _p28_settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _P28FakeSDK.calls == 3 and results["stopped_provider_error"] is None
    assert results["images"] == _outcomes(classified=2, api_failure=1)


def test_m2_groq_permanent_error_is_never_retried(p28_groq_client):
    client = p28_groq_client(_m12_groq_error(401))
    with pytest.raises(vision_client.GroqPermanentError) as info:
        client.classify(_fake_jpeg_bytes("m2q-retry"))
    assert _P28FakeSDK.calls == 1 and info.value.kind == "authentication"
    assert M12_GROQ_KEY not in str(info.value)


def test_m2_permanent_error_rejects_unknown_kind():
    with pytest.raises(ValueError):
        vision_client.ProviderPermanentError("x", "sometimes", 400)


class _M12PermanentClient(_ScriptedClient):
    def classify(self, image_bytes: bytes) -> dict:
        self.calls += 1
        raise vision_client.GeminiPermanentError("401 UNAUTHENTICATED (simulated)", "authentication", 401)


@pytest.mark.parametrize("strict", [False, True])
def test_m2_main_logs_a_clear_stop_and_strict_exit(tmp_path, monkeypatch, caplog, strict):
    _patch_extract_week(monkeypatch, [_make_image(f"m2main-{i}") for i in range(2)])
    client = _M12PermanentClient()
    records, code = _run_main_argv(tmp_path, monkeypatch, caplog, client,
                                   ["--strict-exit"] if strict else [], week_names=("Week 1", "Week 2"))
    assert client.calls == 1
    text = "\n".join(r.getMessage() for r in records)
    assert "Run stopped early: Gemini reported authentication failed" in text
    assert "No further requests or weeks were attempted" in text
    if strict:
        assert code == run_pipeline.STRICT_EXIT_CODE
        assert "Gemini provider error: authentication failed" in _strict_line(records)
    else:
        assert code is None  # default exit behaviour unchanged for an early stop


def test_m2_other_stops_keep_priority_over_permanent_errors():
    assert vision_client._gemini_permanent_kind(_m12_gemini_error(429, "RESOURCE_EXHAUSTED")) is None
    result = classifier.classify_image(_ScriptedClient({1: "quota"}), b"x")
    assert result.quota_exhausted and result.provider_error_kind is None


# L4 -- Gemini 429 through the real client and the whole pipeline (fake SDK responses only) ----


def _m12_per_minute_429():
    return _m12_gemini_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded", details=[
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "23s"}])


def _m12_daily_429():
    return _m12_gemini_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded", details=[
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                         "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "6843s"}])


def test_l4_gemini_daily_429_through_real_client_stops_run(tmp_path, monkeypatch, m12_gemini_client):
    _patch_extract_week(monkeypatch, [_make_image(f"l4d-{i}") for i in range(3)])
    client = m12_gemini_client("ok", _m12_daily_429())
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _M12FakeGenai.calls == 2  # never retried, nothing after the stop
    assert results["stopped_early"] == {"week": "Week 1", "retry_delay_seconds": 6843.0}
    assert results["weeks_not_attempted"] == ["Week 2"]
    assert results["images"] == _outcomes(classified=1, api_failure=1, unprocessed=1)
    assert (tmp_path / "output" / "Week 1" / "PARTIAL - report.xlsx").exists()


def test_l4_gemini_per_minute_429_retries_once_then_stops(tmp_path, monkeypatch, m12_gemini_client):
    _patch_extract_week(monkeypatch, [_make_image(f"l4m-{i}") for i in range(3)])
    client = m12_gemini_client(_m12_per_minute_429(), _m12_per_minute_429())
    results = run_pipeline.run_weeks(_week_folders("Week 1", "Week 2"), _settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _M12FakeGenai.calls == 2  # one bounded retry, then the run stops
    assert results["stopped_rate_limited"] == {"week": "Week 1", "kind": "per_minute",
                                               "retry_delay_seconds": 23.0}
    assert results["weeks_not_attempted"] == ["Week 2"]


def test_l4_gemini_per_minute_429_recovers_after_its_one_retry(tmp_path, monkeypatch, m12_gemini_client):
    _patch_extract_week(monkeypatch, [_make_image(f"l4r-{i}") for i in range(3)])
    client = m12_gemini_client(_m12_per_minute_429())
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _M12FakeGenai.calls == 4  # photo 1: 429 + retry; photos 2-3: one each
    assert results["stopped_rate_limited"] is None and results["images"] == _outcomes(classified=3)


def test_l4_gemini_ambiguous_429_stops_without_retry(tmp_path, monkeypatch, m12_gemini_client):
    _patch_extract_week(monkeypatch, [_make_image(f"l4a-{i}") for i in range(3)])
    client = m12_gemini_client(_m12_gemini_error(429, "RESOURCE_EXHAUSTED", "Resource exhausted"))
    results = run_pipeline.run_weeks(_week_folders("Week 1"), _settings(tmp_path), client,
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert _M12FakeGenai.calls == 1
    assert results["stopped_rate_limited"]["kind"] == "ambiguous"


# L2 -- the manual live script needs an explicit opt-in (imported only; never executed) --------


def _m12_manual_script():
    path = Path(run_pipeline.__file__).parent / "tests" / "manual" / "phase3d_live_comparison.py"
    spec = importlib.util.spec_from_file_location("m12_phase3d_manual", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # __main__ block does not run on import
    return path, module


def test_l2_manual_live_script_refuses_without_explicit_opt_in(capsys):
    _, module = _m12_manual_script()
    with pytest.raises(SystemExit) as info:
        module.require_live_opt_in([])
    assert info.value.code == 2 and "Refusing to run" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        module.require_live_opt_in(["--confirm-live", "yes"])  # only the exact flag counts
    assert module.require_live_opt_in([module.LIVE_OPT_IN_FLAG]) is None


def test_l2_opt_in_check_runs_before_settings_env_or_any_client():
    path, _ = _m12_manual_script()
    main_block = path.read_text(encoding="utf-8").split('if __name__ == "__main__":', 1)[1]
    first_lines = [line.strip() for line in main_block.splitlines() if line.strip()]
    assert first_lines[0].startswith("require_live_opt_in(sys.argv[1:])")
    for later in ("load_settings", "load_provider_environment", "GeminiVisionClient("):
        assert main_block.index("require_live_opt_in") < main_block.index(later)



# --- M3: cross-week reuse of successful results for byte-identical photos ----------------------


def _m3_patch_weeks(monkeypatch, images_by_week: dict):
    """Per-week fake extraction: _week_folders() gives each week source_workbooks=[<week>/fake.xlsx]."""
    def fake_extract(source_workbooks, *a, **kw):
        week = Path(source_workbooks[0]).parent.name
        return extraction.ExtractionResult(images=list(images_by_week[week]), report_date=None, warnings=[])
    monkeypatch.setattr(extraction, "extract_week", fake_extract)


def _m3_settings(tmp_path):
    return _main_settings(tmp_path)  # names provider + model, so provenance is recorded


def _m3_run(tmp_path, client, weeks, settings=None, force=False, sample=None):
    return run_pipeline.run_weeks(_week_folders(*weeks), settings or _m3_settings(tmp_path), client,
                                  force=force, sample=sample, flagged=_flagged(tmp_path))


def _m3_state(tmp_path, week):
    return state.load_state(tmp_path / ".state", week)


def _m3_provenance(tmp_path):
    return run_pipeline._result_provenance(_m3_settings(tmp_path))


def _m3_entry(provenance, location="Main Deck", ok=True):
    entry = {"ok": ok, "activity_identified": "No activity observed", "activity_confidence": "High",
             "ai_suggested_location": location, "location_confidence": "High", "person_visible": "No"}
    if provenance is not None:
        entry["provenance"] = provenance
    return entry


SHARED = [_make_image("m3-shared-a"), _make_image("m3-shared-b")]


def test_m3_identical_photos_in_another_week_reuse_the_result(tmp_path, monkeypatch, caplog):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    client = _ScriptedClient()
    with caplog.at_level(logging.INFO, logger="pipeline"):
        results = _m3_run(tmp_path, client, ("Week 1", "Week 2"))
    assert client.calls == 2  # only Week 1's two photos are sent
    assert results["failed"] == [] and [s["reused"] for s in results["succeeded"]] == [0, 2]
    assert results["images"] == _outcomes(classified=4)
    h = state.image_hash(SHARED[0].image_bytes)
    assert f"reused the saved classification of an identical photo from Week 1 (sha256 {h})" in caplog.text
    # each week keeps its own filenames/serials; the Image Register is unchanged
    header, rows = _report_table(tmp_path / "output" / "Week 2" / "report.xlsx")
    assert header == EXPECTED_HEADERS
    assert [r["AI Suggested Location"] for r in rows] == ["Main Deck", "Main Deck"]
    assert _status_sheet(tmp_path / "output" / "Week 2" / "report.xlsx")["Report status"] == "COMPLETE"


def test_m3_different_images_are_each_sent(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": [_make_image("m3-w1")], "Week 2": [_make_image("m3-w2")]})
    client = _ScriptedClient()
    results = _m3_run(tmp_path, client, ("Week 1", "Week 2"))
    assert client.calls == 2 and [s["reused"] for s in results["succeeded"]] == [0, 0]


def test_m3_only_the_duplicate_is_reused_in_a_mixed_week(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED[:1], "Week 2": [SHARED[0], _make_image("m3-new")]})
    client = _ScriptedClient()
    results = _m3_run(tmp_path, client, ("Week 1", "Week 2"))
    assert client.calls == 2 and [s["reused"] for s in results["succeeded"]] == [0, 1]


def test_m3_reuse_also_works_from_an_earlier_run(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    _m3_run(tmp_path, _ScriptedClient(), ("Week 1",))
    later = _ScriptedClient()
    _m3_run(tmp_path, later, ("Week 2",))  # e.g. --weeks "Week 2" on another day
    assert later.calls == 0


def test_m3_receiving_week_state_is_self_contained(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    _m3_run(tmp_path, _ScriptedClient(), ("Week 1", "Week 2"))
    provenance = _m3_provenance(tmp_path)
    week2 = _m3_state(tmp_path, "Week 2")
    for img in SHARED:
        entry = week2[state.image_hash(img.image_bytes)]
        assert entry == {**_m3_entry(provenance), "reused_from_week": "Week 1"}
    # Week 2 recovers on its own: even without Week 1's state file nothing is re-sent
    state.state_path(tmp_path / ".state", "Week 1").unlink()  # temp test data only
    again = _ScriptedClient()
    _m3_run(tmp_path, again, ("Week 2",))
    assert again.calls == 0


def test_m3_new_live_results_record_their_provenance(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED[:1]})
    _m3_run(tmp_path, _ScriptedClient(), ("Week 1",))
    entry = list(_m3_state(tmp_path, "Week 1").values())[0]
    assert entry["provenance"] == {"provider": "gemini", "model": "gemini-3.8-flash",
                                   "prompt_schema": vision_client.classification_fingerprint("gemini")}


def test_m3_failed_results_are_never_reused(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED[:1], "Week 2": SHARED[:1]})
    client = _ScriptedClient({1: "err"})
    results = _m3_run(tmp_path, client, ("Week 1", "Week 2"))
    assert client.calls == 2  # Week 2 sends its own request
    assert results["images"] == _outcomes(classified=1, api_failure=1)
    assert "reused_from_week" not in list(_m3_state(tmp_path, "Week 2").values())[0]


@pytest.mark.parametrize("mutate", [
    lambda p: None,                                     # legacy entry: no provenance at all
    lambda p: {**p, "model": "gemini-other-model"},     # different model
    lambda p: {**p, "provider": "groq"},                # different provider
    lambda p: {**p, "prompt_schema": "0" * 64},         # different prompt/schema version
])
def test_m3_results_with_other_or_unknown_provenance_are_not_reused(tmp_path, monkeypatch, mutate):
    _m3_patch_weeks(monkeypatch, {"Week 2": SHARED[:1]})
    h = state.image_hash(SHARED[0].image_bytes)
    state.save_state(tmp_path / ".state", "Week 1", {h: _m3_entry(mutate(_m3_provenance(tmp_path)))})
    client = _ScriptedClient()
    _m3_run(tmp_path, client, ("Week 2",))
    assert client.calls == 1


def test_m3_a_changed_prompt_invalidates_reuse(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED[:1], "Week 2": SHARED[:1]})
    _m3_run(tmp_path, _ScriptedClient(), ("Week 1",))
    monkeypatch.setattr(vision_client, "PROMPT", vision_client.PROMPT + "\nA new rule.")
    client = _ScriptedClient()
    _m3_run(tmp_path, client, ("Week 2",))
    assert client.calls == 1


@pytest.mark.parametrize("bad", [{"ok": "true"}, {"ok": None}, {"ai_suggested_location": ""},
                                 {"person_visible": None}, {"activity_confidence": 3}])
def test_m3_incomplete_or_malformed_entries_are_not_reused(tmp_path, monkeypatch, bad):
    _m3_patch_weeks(monkeypatch, {"Week 2": SHARED[:1]})
    h = state.image_hash(SHARED[0].image_bytes)
    state.save_state(tmp_path / ".state", "Week 1", {h: {**_m3_entry(_m3_provenance(tmp_path)), **bad}})
    client = _ScriptedClient()
    _m3_run(tmp_path, client, ("Week 2",))
    assert client.calls == 1


def test_m3_conflicting_results_in_other_weeks_are_ambiguous_and_not_reused(tmp_path, monkeypatch, caplog):
    _m3_patch_weeks(monkeypatch, {"Week 3": SHARED[:1]})
    h, p = state.image_hash(SHARED[0].image_bytes), _m3_provenance(tmp_path)
    state.save_state(tmp_path / ".state", "Week 1", {h: _m3_entry(p, "Main Deck")})
    state.save_state(tmp_path / ".state", "Week 2", {h: _m3_entry(p, "Hatch Coaming")})
    client = _ScriptedClient()
    with caplog.at_level(logging.INFO, logger="pipeline"):
        _m3_run(tmp_path, client, ("Week 3",))
    assert client.calls == 1 and "have DIFFERENT saved results" in caplog.text


def test_m3_unreadable_state_files_are_ignored_not_reused(tmp_path, monkeypatch, caplog):
    _m3_patch_weeks(monkeypatch, {"Week 2": SHARED[:1]})
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "Week_1.json").write_text("{ not json", encoding="utf-8")
    client = _ScriptedClient()
    with caplog.at_level(logging.INFO, logger="pipeline"):
        _m3_run(tmp_path, client, ("Week 2",))
    assert client.calls == 1 and "ignores unreadable state file(s): Week_1.json" in caplog.text


def test_m3_force_bypasses_reuse_and_sends_every_photo(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    _m3_run(tmp_path, _ScriptedClient(), ("Week 1", "Week 2"))
    forced = _ScriptedClient()
    results = _m3_run(tmp_path, forced, ("Week 1", "Week 2"), force=True)
    assert forced.calls == 4 and [s["reused"] for s in results["succeeded"]] == [0, 0]
    week2 = _m3_state(tmp_path, "Week 2")
    assert all("reused_from_week" not in e for e in week2.values())  # fresh results replaced them


def test_m3_force_flag_from_the_cli_bypasses_reuse(tmp_path, monkeypatch, caplog):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    first = _ScriptedClient()
    _run_main_argv(tmp_path, monkeypatch, caplog, first, week_names=("Week 1", "Week 2"))
    assert first.calls == 2
    forced = _ScriptedClient()
    _run_main_argv(tmp_path, monkeypatch, caplog, forced, ["--force"], week_names=("Week 1", "Week 2"))
    assert forced.calls == 4


def test_m3_dry_run_never_reuses_and_never_touches_production_state(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    settings = _m3_settings(tmp_path)
    _m3_run(tmp_path, _ScriptedClient(), ("Week 1",), settings=settings)
    before = {p.name: p.read_bytes() for p in (tmp_path / ".state").iterdir()}
    dry = run_pipeline._dry_run_settings(settings)
    results = run_pipeline.run_weeks(_week_folders("Week 2"), dry, vision_client.NullVisionClient(),
                                     force=False, sample=None, flagged=_flagged(tmp_path))
    assert results["images"] == _outcomes(dry_run=2) and results["succeeded"][0]["reused"] == 0
    assert {p.name: p.read_bytes() for p in (tmp_path / ".state").iterdir()} == before
    dry_state = state.load_state(Path(dry["paths"]["state_dir"]), "Week 2")
    assert all(e["ok"] is False and "provenance" not in e and "reused_from_week" not in e
               for e in dry_state.values())


def test_m3_dry_run_results_are_never_a_reuse_source(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    settings = _m3_settings(tmp_path)
    dry = run_pipeline._dry_run_settings(settings)
    run_pipeline.run_weeks(_week_folders("Week 1"), dry, vision_client.NullVisionClient(),
                           force=False, sample=None, flagged=_flagged(tmp_path))
    client = _ScriptedClient()
    _m3_run(tmp_path, client, ("Week 2",), settings=settings)
    assert client.calls == 2


def test_m3_settings_without_provider_or_model_keep_the_old_behaviour(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    client = _ScriptedClient()
    _m3_run(tmp_path, client, ("Week 1", "Week 2"), settings=_settings(tmp_path))  # no provider/model
    assert client.calls == 4
    entry = list(_m3_state(tmp_path, "Week 2").values())[0]
    assert set(entry) == {"ok", *state.RESULT_FIELDS}  # exactly the pre-M3 state entry shape


def test_m3_quota_plan_counts_unique_outstanding_images(tmp_path, monkeypatch):
    other = _make_image("m3-other")
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": [*SHARED, other], "Week 3": SHARED})
    plan = run_pipeline.quota_plan(_week_folders("Week 1", "Week 2", "Week 3"), _m3_settings(tmp_path))
    assert [(e["ai_total"], e["done"], e["remaining"], e["reused"]) for e in plan] == [
        (2, 0, 2, 0), (3, 0, 1, 2), (2, 0, 0, 2)]
    assert sum(e["remaining"] for e in plan) == 3  # 3 unique images -> 3 requests, not 7


def test_m3_quota_plan_counts_saved_reusable_results_as_no_request(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED, "Week 2": SHARED})
    _m3_run(tmp_path, _ScriptedClient(), ("Week 1",))
    plan = run_pipeline.quota_plan(_week_folders("Week 1", "Week 2"), _m3_settings(tmp_path))
    assert [(e["done"], e["remaining"], e["reused"]) for e in plan] == [(2, 0, 0), (0, 0, 2)]
    lines = "\n".join(run_pipeline._quota_plan_lines(plan, None))
    assert "Week 2: 2 AI-assessable, 0 done, 0 remaining, 2 identical to a photo in another week" in lines


def test_m3_quota_plan_does_not_count_unreusable_results(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 2": SHARED[:1]})
    h = state.image_hash(SHARED[0].image_bytes)
    state.save_state(tmp_path / ".state", "Week 1", {h: _m3_entry(None)})  # legacy, no provenance
    plan = run_pipeline.quota_plan(_week_folders("Week 2"), _m3_settings(tmp_path))
    assert (plan[0]["remaining"], plan[0]["reused"]) == (1, 0)


def test_m3_stop_after_reuse_keeps_the_existing_stop_behaviour(tmp_path, monkeypatch):
    _m3_patch_weeks(monkeypatch, {"Week 1": SHARED[:1], "Week 2": [SHARED[0], _make_image("m3-q")],
                                  "Week 3": [_make_image("m3-never")]})
    client = _ScriptedClient({2: "quota"})
    results = _m3_run(tmp_path, client, ("Week 1", "Week 2", "Week 3"))
    assert client.calls == 2  # Week 1 photo, then Week 2's new photo hits the quota
    assert results["stopped_early"]["week"] == "Week 2" and results["weeks_not_attempted"] == ["Week 3"]
    week2 = _m3_state(tmp_path, "Week 2")
    assert sorted(e["ok"] for e in week2.values()) == [False, True]  # reused saved; failed retryable


# ---------------------------------------------------------------------------
# Pre-AI audit (2026-11-16): input validation and "nothing processed" reporting.
# ---------------------------------------------------------------------------


def _audit_main(monkeypatch, settings, argv):
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", *argv])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    run_pipeline.main()


@pytest.mark.parametrize("argv", [["--dry-run"], ["--quota-plan"], [], ["--strict-exit"]])
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_audit_bad_project_root_is_a_clean_configuration_error(tmp_path, monkeypatch, capsys,
                                                               env_load_attempts, argv, kind):
    settings = _main_settings(tmp_path)
    root = tmp_path / "no-such-folder"
    if kind == "file":
        root = tmp_path / "root.txt"
        root.write_text("x")
    settings["paths"]["project_root"] = str(root)

    def _must_not_run(*a, **kw):
        raise AssertionError("nothing may run on a configuration error")

    monkeypatch.setattr(run_pipeline, "run_weeks", _must_not_run)
    monkeypatch.setattr(run_pipeline, "quota_plan", _must_not_run)
    monkeypatch.setattr(run_pipeline.vision_client, "build_vision_client", _must_not_run)
    with pytest.raises(SystemExit) as exc_info:
        _audit_main(monkeypatch, settings, argv)
    err = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert err.startswith("Configuration error: paths.project_root ") and str(root) in err
    assert "no API requests were made" in err and "Traceback" not in err
    assert env_load_attempts == []  # .env is never even attempted
    assert not (tmp_path / "logs").exists() and not (tmp_path / "dry_run").exists()


def test_audit_empty_project_root_dry_run_warns_and_strict_exit_fails(tmp_path, monkeypatch, caplog):
    settings = _main_settings(tmp_path)
    (tmp_path / "root").mkdir()
    settings["paths"]["project_root"] = str(tmp_path / "root")
    with caplog.at_level(logging.INFO, logger="pipeline"):
        _audit_main(monkeypatch, settings, ["--dry-run"])  # default mode: still exit 0
    assert "No week was processed" in caplog.text and str(tmp_path / "root") in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="pipeline"):
        with pytest.raises(SystemExit) as exc_info:
            _audit_main(monkeypatch, settings, ["--dry-run", "--strict-exit"])
    assert exc_info.value.code == run_pipeline.STRICT_EXIT_CODE
    assert "no week was processed" in caplog.text


def test_audit_week_folders_without_workbooks_count_as_nothing_processed(tmp_path, monkeypatch, caplog):
    settings = _main_settings(tmp_path)
    (tmp_path / "root" / "Week 1").mkdir(parents=True)
    settings["paths"]["project_root"] = str(tmp_path / "root")
    with caplog.at_level(logging.INFO, logger="pipeline"):
        with pytest.raises(SystemExit) as exc_info:
            _audit_main(monkeypatch, settings, ["--dry-run", "--strict-exit", "--weeks", "Week 1"])
    assert exc_info.value.code == run_pipeline.STRICT_EXIT_CODE
    assert "among the --weeks selected" in caplog.text


def test_audit_no_week_processed_reason_only_when_nothing_ran():
    assert run_pipeline._strict_exit_reasons({}) == []  # older/stubbed shape unchanged
    nothing = {"succeeded": [], "failed": [], "images": _outcomes()}
    assert run_pipeline._strict_exit_reasons(nothing) == [
        "no week was processed (no week folder with source workbooks found)"]
    failed = {**nothing, "failed": [{"week": "Week 1"}]}
    assert run_pipeline._strict_exit_reasons(failed) == ["1 week(s) failed: Week 1"]
    ran = {**nothing, "succeeded": [_fake_summary("Week 1")]}
    assert run_pipeline._strict_exit_reasons(ran) == []


def test_audit_corrupt_own_state_fails_only_that_week_and_names_the_file(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image("a1")])
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    (state_dir / "Week_1.json").write_text("{ not json", encoding="utf-8")
    weeks = _week_folders("Week 1", "Week 2")
    results = run_pipeline.run_weeks(weeks, _settings(tmp_path), _StubClient(), force=False,
                                     sample=None, flagged=_flagged(tmp_path))
    [failure] = results["failed"]
    assert failure["week"] == "Week 1" and failure["exception_type"] == "StateFileError"
    assert "Week_1.json" in failure["error"] and "is not valid JSON" in failure["error"]
    assert (state_dir / "Week_1.json").read_text(encoding="utf-8") == "{ not json"  # untouched
    assert [s["week"] for s in results["succeeded"]] == ["Week 2"]  # isolation unchanged


def test_audit_quota_plan_names_the_corrupt_state_file(tmp_path, monkeypatch):
    _patch_extract_week(monkeypatch, [_make_image("q1")])
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "Week_1.json").write_text("[1]", encoding="utf-8")
    [entry] = run_pipeline.quota_plan(_week_folders("Week 1"), _settings(tmp_path))
    assert entry["error"].startswith("StateFileError: state file ") and "Week_1.json" in entry["error"]
