"""run_pipeline --from-registers: register rows as AI-pipeline input.

Synthetic projects only: source workbooks are generated in tmp_path and their registers are
written by the real extraction-only code (weekly_image_report). The vision client is a local
stub -- no real API, no .env, no network."""

import csv
import hashlib
import logging
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_pipeline  # noqa: E402
from src import discovery, logging_utils, state, vision_client  # noqa: E402
from src import weekly_image_report as wr  # noqa: E402
from test_image_extraction import banner, hashes, make_workbook, png_bytes  # noqa: E402
from test_register_input import BLUE, GREEN, RED, YELLOW, build_week30  # noqa: E402

PURPLE = png_bytes((120, 0, 120))
REPORT30, MANIFEST30 = "AI Classification - Week_30.xlsx", "AI Classification - Week_30 - manifest.csv"
REPORT31 = "AI Classification - Week_31.xlsx"

OK = {"person_visible": "No", "activity_identified": "No activity observed",
      "activity_confidence": "High", "ai_suggested_location": "Main Deck", "location_confidence": "High"}


class RecordingClient(vision_client.VisionClient):
    """Records the bytes of every request; `script` maps a 1-indexed call to a failure."""

    def __init__(self, script=None):
        self.sent: list[bytes] = []
        self._script = script or {}

    @property
    def calls(self):
        return len(self.sent)

    def classify(self, image_bytes):
        self.sent.append(image_bytes)
        kind = self._script.get(self.calls)
        if kind == "quota":
            raise vision_client.GeminiQuotaExhaustedError("quota exhausted (simulated)", 60.0)
        if kind == "err":
            raise RuntimeError("simulated failure")
        return dict(OK)


def build_project(root: Path) -> Path:
    """Week 30 (see test_register_input.build_week30) + week 31: W31-0001 RED (no heading; the
    same photo as W30-0001), W31-0002 PURPLE (no heading), W31-0003 GREEN ("HATCH COAMING")."""
    build_week30(root)
    make_workbook(root / "week 31" / "Deck Photos -09 AUG 2026 Part 1 (1).xlsx", [
        ("AUG 09 2026", banner("09 AUG 2026"), [("B5", RED), ("B6", PURPLE), ("B12", GREEN)],
         [(10, "HATCH COAMING")]),
    ])
    wr.generate_weekly_reports(root)
    return root


@pytest.fixture
def proj(tmp_path):
    root = build_project(tmp_path / "project")
    auto = tmp_path / "automation"
    settings = {
        "provider": "gemini", "model": "gemini-test-model",
        "processing": {"heading_min_merge_width": 10},
        "paths": {"project_root": str(root), "automation_root": str(auto),
                  "state_dir": str(auto / ".state"), "logs_dir": str(auto / "logs")},
        "report": {"extracted_images_dirname": "Extracted Images",
                   "output_filename": "Final Official vs AI Location Report.xlsx",
                   "image_cell_max_width_px": 420, "image_cell_max_height_px": 320},
    }
    return root, auto, settings


def weeks(root, *names):
    found = discovery.find_week_folders(root)
    return [w for w in found if not names or w.name in names]


def run(root, auto, settings, client, names=(), force=False, sample=None):
    return run_pipeline.run_weeks(weeks(root, *names), settings, client, force, sample,
                                  logging_utils.FlaggedItemsWriter(auto / "logs"), from_registers=True)


def read_report(path):
    wb = openpyxl.load_workbook(path)
    try:
        ws = wb["Image Register"]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        status = {r[0]: r[1] for r in wb["Processing Status"].iter_rows(min_row=2, values_only=True)}
        return rows[0], rows[1:], len(ws._images), status
    finally:
        wb.close()


def read_manifest(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def col(header, rows, name):
    i = header.index(name)
    return [r[i] for r in rows]


# --- image IDs, all appearances, official location, unique requests ---------------------------


def test_every_register_row_kept_with_image_ids_and_one_request_per_unique_photo(proj):
    root, auto, settings = proj
    client = RecordingClient()
    results = run(root, auto, settings, client, ("Week 30",))
    assert client.sent == [RED, YELLOW]  # 3 blank rows, 2 unique photos -> 2 requests
    header, rows, n_images, status = read_report(auto / "output" / "Week 30" / REPORT30)
    assert header == (run_pipeline.report_builder.HEADERS + ["Result Source"]
                      + run_pipeline.report_builder.REVIEW_HEADERS)
    assert col(header, rows, "Serial Number") == [1, 2, 3, 4, 5]
    assert col(header, rows, "Image Filename") == [f"W30-000{i}.png" for i in range(1, 6)]
    assert n_images == 5  # every appearance embedded
    assert col(header, rows, "Result Source") == [
        "AI request (this run)", "AI request (this run)", "Official Location (no AI request)",
        "Official Location (no AI request)", "Same photo as W30-0001 (no AI request)"]
    assert col(header, rows, "Classification Status") == ["CLASSIFIED"] * 5
    assert col(header, rows, "AI Suggested Location") == ["Main Deck", "Main Deck", None, None, "Main Deck"]
    assert "Week_30_Image_Register.xlsx (5 rows, every photo SHA-256 verified" in status["Input"]
    assert status["Report status"] == "COMPLETE" and status["Photos in this report"] == "5 of 5 extracted"
    [summary] = results["succeeded"]
    assert summary["classified"] == 2 and summary["duplicates"] == 1 and summary["total"] == 5


def test_official_location_preserved_and_blank_rows_assessed(proj):
    root, auto, settings = proj
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    header, rows, _, _ = read_report(auto / "output" / "Week 30" / REPORT30)
    assert col(header, rows, "Official Location") == [None, None, "MAIN DECK", "MAIN DECK", None]
    assert col(header, rows, "Activity Identified") == [
        "No activity observed", "No activity observed", "Not Assessed (Official Location)",
        "Not Assessed (Official Location)", "No activity observed"]
    assert col(header, rows, "Report Date") == ["8/2/2026"] * 5  # register text, verbatim


def test_manifest_carries_image_id_hash_and_result_source(proj):
    root, auto, settings = proj
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    m = read_manifest(auto / "output" / "Week 30" / MANIFEST30)
    assert list(m[0]) == ["image_id", "serial_number", "image_filename", "report_date",
                          "original_excel_filename", "worksheet_name", "official_location",
                          "ai_suggested_location", "activity_identified", "location_confidence",
                          "classification_status", "result_source", "sha256"]
    assert [r["image_id"] for r in m] == [f"W30-000{i}" for i in range(1, 6)]
    assert [r["sha256"] for r in m] == [hashlib.sha256(b).hexdigest() for b in (RED, YELLOW, GREEN, BLUE, RED)]
    assert m[4]["result_source"] == "Same photo as W30-0001 (no AI request)"
    assert m[2]["official_location"] == "MAIN DECK" and m[0]["official_location"] == ""


def test_state_format_unchanged_one_entry_per_unique_photo(proj):
    root, auto, settings = proj
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    week_state = state.load_state(auto / ".state", "Week 30")
    assert set(week_state) == {hashlib.sha256(RED).hexdigest(), hashlib.sha256(YELLOW).hexdigest()}
    for entry in week_state.values():
        assert set(entry) == {"ok", *state.RESULT_FIELDS, "provenance"} and entry["ok"] is True


def test_duplicate_of_a_failed_request_copies_the_failure_and_is_not_sent(proj):
    root, auto, settings = proj
    client = RecordingClient({1: "err"})
    results = run(root, auto, settings, client, ("Week 30",))
    assert client.sent == [RED, YELLOW]  # W30-0005 (RED again) is never sent
    header, rows, _, status = read_report(auto / "output" / "Week 30" / REPORT30)
    assert col(header, rows, "Classification Status") == [
        "API_FAILURE", "CLASSIFIED", "CLASSIFIED", "CLASSIFIED", "API_FAILURE"]
    assert status["Report status"].startswith("COMPLETE WITH 2 API FAILURE(S)")
    assert results["images"]["api_failure"] == 2  # per appearance
    # rerun: the failed photo is retried once, and its repeat follows it
    retry = RecordingClient()
    run(root, auto, settings, retry, ("Week 30",))
    assert retry.sent == [RED]
    header, rows, _, _ = read_report(auto / "output" / "Week 30" / REPORT30)
    assert col(header, rows, "Classification Status") == ["CLASSIFIED"] * 5
    assert col(header, rows, "Result Source")[1] == "Saved result (earlier run)"


def test_force_still_sends_each_unique_photo_once(proj):
    root, auto, settings = proj
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    forced = RecordingClient()
    run(root, auto, settings, forced, ("Week 30",), force=True)
    assert forced.sent == [RED, YELLOW]


# --- cross-week reuse and quota planning ------------------------------------------------------


def test_cross_week_reuse_within_one_run_and_across_runs(proj):
    root, auto, settings = proj
    client = RecordingClient()
    run(root, auto, settings, client)
    assert client.sent == [RED, YELLOW, PURPLE]  # week 31's RED reuses Week 30's result
    header, rows, _, _ = read_report(auto / "output" / "week 31" / REPORT31)
    assert col(header, rows, "Image Filename") == ["W31-0001.png", "W31-0002.png", "W31-0003.png"]
    assert col(header, rows, "Result Source") == [
        "Reused from Week 30 (identical photo, no AI request)", "AI request (this run)",
        "Official Location (no AI request)"]
    w31 = state.load_state(auto / ".state", "week 31")
    assert w31[hashlib.sha256(RED).hexdigest()]["reused_from_week"] == "Week 30"
    again = RecordingClient()
    run(root, auto, settings, again)
    assert again.sent == []  # everything is saved now
    header, rows, _, _ = read_report(auto / "output" / "week 31" / REPORT31)
    assert col(header, rows, "Result Source")[0] == "Reused from Week 30 (identical photo, no AI request)"


def test_quota_plan_counts_unique_requests(proj, capsys):
    root, auto, settings = proj
    plan = run_pipeline.quota_plan(weeks(root), settings, from_registers=True)
    assert [(e["week"], e["ai_total"], e["done"], e["remaining"], e["reused"], e["duplicates"])
            for e in plan] == [("Week 30", 3, 0, 2, 0, 1), ("week 31", 2, 0, 1, 1, 0)]
    lines = "\n".join(run_pipeline._quota_plan_lines(plan, None))
    assert "Week 30: 3 AI-assessable, 0 done, 2 remaining, 1 repeat appearance(s)" in lines
    assert "3 still need a Gemini request" in lines
    run(root, auto, settings, RecordingClient())
    plan = run_pipeline.quota_plan(weeks(root), settings, from_registers=True)
    assert sum(e["remaining"] for e in plan) == 0 and [e["done"] for e in plan] == [2, 2]


def test_quota_plan_reports_refused_register(proj):
    root, auto, settings = proj
    (root / "Week 30" / "Extracted Images - New" / "W30-0001.png").write_bytes(BLUE)
    plan = run_pipeline.quota_plan(weeks(root), settings, from_registers=True)
    assert plan[0]["error"].startswith("RegisterInputError: Week 30: register is not complete")
    assert plan[1]["error"] is None


def test_quota_plan_cli(proj, monkeypatch, capsys):
    root, auto, settings = proj
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--quota-plan", "--from-registers"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)
    run_pipeline.main()
    out = capsys.readouterr().out
    assert "Week 30: 3 AI-assessable, 0 done, 2 remaining" in out
    assert not auto.exists()  # read-only: nothing written at all


# --- partial processing, output naming, existing report protection ----------------------------


def seed_production_files(auto: Path) -> dict:
    """Files that look like existing production output for Week 30 (legacy mode)."""
    out = auto / "output" / "Week 30"
    (out / "Extracted Images").mkdir(parents=True)
    files = {out / "Final Official vs AI Location Report.xlsx": b"production report",
             out / "manifest.csv": b"production manifest",
             out / "PARTIAL - Final Official vs AI Location Report.xlsx": b"legacy partial",
             out / "SAMPLE - manifest.csv": b"legacy sample",
             out / "Extracted Images" / "2026-08-02_001.jpg": b"legacy photo"}
    for p, data in files.items():
        p.write_bytes(data)
    return files


def test_separate_output_names_and_existing_reports_untouched(proj):
    root, auto, settings = proj
    files = seed_production_files(auto)
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    out = auto / "output" / "Week 30"
    assert {p.name for p in out.iterdir()} == {p.name for p in files if p.parent == out} | {
        "Extracted Images", REPORT30, MANIFEST30}
    for p, data in files.items():
        assert p.read_bytes() == data
    assert [p.name for p in (out / "Extracted Images").iterdir()] == ["2026-08-02_001.jpg"]


def test_no_photo_files_are_written_or_copied(proj):
    root, auto, settings = proj
    run(root, auto, settings, RecordingClient())
    written = sorted(p.relative_to(auto / "output").as_posix() for p in (auto / "output").rglob("*"))
    assert written == ["Week 30", f"Week 30/{MANIFEST30}", f"Week 30/{REPORT30}",
                       "week 31", "week 31/AI Classification - Week_31 - manifest.csv",
                       f"week 31/{REPORT31}"]


def test_early_stop_writes_partial_register_report_and_retires_only_its_own_family(proj):
    root, auto, settings = proj
    files = seed_production_files(auto)
    run(root, auto, settings, RecordingClient(), ("Week 30",))  # an earlier complete AI report
    # reset state so the next run must send again, then hit the quota on its 2nd request
    (auto / ".state" / "Week_30.json").unlink()
    client = RecordingClient({2: "quota"})
    results = run(root, auto, settings, client)
    assert client.sent == [RED, YELLOW] and results["stopped_early"]["week"] == "Week 30"
    assert results["weeks_not_attempted"] == ["week 31"]
    out = auto / "output" / "Week 30"
    header, rows, n_images, status = read_report(out / ("PARTIAL - " + REPORT30))
    assert n_images == 5 and status["Report status"].startswith("PARTIAL")
    assert col(header, rows, "Classification Status") == [
        "CLASSIFIED", "API_FAILURE", "CLASSIFIED", "CLASSIFIED", "CLASSIFIED"]
    assert col(header, rows, "Result Source")[4] == "Same photo as W30-0001 (no AI request)"
    assert (out / ("PARTIAL - " + MANIFEST30)).is_file()
    stale = sorted(p.name for p in out.iterdir() if p.name.startswith("STALE "))
    assert len(stale) == 2 and all(REPORT30.split(".")[0] in n for n in stale)
    assert not (out / REPORT30).exists()
    for p, data in files.items():  # production / legacy files untouched
        assert p.read_bytes() == data
    assert not (auto / "output" / "week 31").exists()


def test_stop_before_a_repeat_marks_the_repeat_like_its_original(proj):
    root, auto, settings = proj
    client = RecordingClient({1: "quota"})
    results = run(root, auto, settings, client, ("Week 30",))
    assert client.sent == [RED]
    header, rows, _, _ = read_report(auto / "output" / "Week 30" / ("PARTIAL - " + REPORT30))
    assert col(header, rows, "Classification Status") == [
        "API_FAILURE", "UNPROCESSED", "CLASSIFIED", "CLASSIFIED", "API_FAILURE"]
    assert results["images"]["unprocessed"] == 1


def test_completed_run_supersedes_its_own_partial_and_sample_only(proj):
    root, auto, settings = proj
    files = seed_production_files(auto)
    run(root, auto, settings, RecordingClient(), ("Week 30",), sample=1)
    out = auto / "output" / "Week 30"
    assert (out / ("SAMPLE - " + REPORT30)).is_file()
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    names = {p.name for p in out.iterdir()}
    assert REPORT30 in names and ("SAMPLE - " + REPORT30) not in names
    assert any(n.startswith("STALE ") and n.endswith("SAMPLE - " + REPORT30) for n in names)
    for p, data in files.items():
        assert p.read_bytes() == data


def test_refused_register_fails_only_that_week_and_sends_nothing_for_it(proj):
    root, auto, settings = proj
    (root / "Week 30" / "Extracted Images - New" / "W30-0002.png").write_bytes(GREEN)
    client = RecordingClient()
    results = run(root, auto, settings, client)
    [failure] = results["failed"]
    assert failure["week"] == "Week 30" and failure["exception_type"] == "RegisterInputError"
    assert client.sent == [RED, PURPLE]  # week 31 only
    assert not (auto / "output" / "Week 30" / REPORT30).exists()
    assert [s["week"] for s in results["succeeded"]] == ["week 31"]


def test_legacy_mode_unchanged_and_shares_the_same_state(proj):
    """The default (extraction) mode still writes its own family, and sees results saved by a
    register-based run as done (same SHA-256 keys)."""
    root, auto, settings = proj
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    legacy = RecordingClient()
    run_pipeline.run_weeks(weeks(root, "Week 30"), settings, legacy, False, None,
                           logging_utils.FlaggedItemsWriter(auto / "logs"))
    assert legacy.sent == []
    out = auto / "output" / "Week 30"
    assert (out / "Final Official vs AI Location Report.xlsx").is_file() and (out / "manifest.csv").is_file()
    header, rows, _, _ = read_report(out / "Final Official vs AI Location Report.xlsx")
    assert header == run_pipeline.report_builder.HEADERS and len(rows) == 4  # deduplicated, as before
    assert (out / REPORT30).is_file()  # untouched by the legacy run


# --- dry run, CLI, original file integrity ----------------------------------------------------


def test_dry_run_from_registers_is_isolated(proj, monkeypatch, caplog, env_load_attempts):
    root, auto, settings = proj
    before = hashes(root)
    monkeypatch.setattr(sys, "argv", ["run_pipeline.py", "--dry-run", "--from-registers"])
    monkeypatch.setattr(run_pipeline.config, "load_settings", lambda: settings)

    def _forbidden(*a, **kw):
        raise AssertionError("a dry run must never build a real client")

    monkeypatch.setattr(vision_client, "GeminiVisionClient", _forbidden)
    with caplog.at_level(logging.INFO, logger="pipeline"):
        run_pipeline.main()
    assert sorted(p.name for p in auto.iterdir()) == ["dry_run"]  # real state/output/logs untouched
    out = auto / "dry_run" / "output" / "Week 30"
    header, rows, _, _ = read_report(out / ("DRY RUN - " + REPORT30))
    assert col(header, rows, "Classification Status") == ["DRY_RUN", "DRY_RUN", "CLASSIFIED", "CLASSIFIED", "DRY_RUN"]
    assert (out / ("DRY RUN - " + MANIFEST30)).is_file()
    dry_state = state.load_state(auto / "dry_run" / ".state", "Week 30")
    assert all(e["ok"] is False for e in dry_state.values())
    assert env_load_attempts == [] and hashes(root) == before


def test_full_run_leaves_every_source_register_and_photo_byte_identical(proj):
    root, auto, settings = proj
    before = hashes(root)  # source workbooks, registers, Extracted Images - New
    run(root, auto, settings, RecordingClient({2: "err"}))
    run(root, auto, settings, RecordingClient(), force=True)
    run(root, auto, settings, RecordingClient({1: "quota"}), force=True)
    assert hashes(root) == before


# --- Activity Confidence / Remarks columns (register mode only) -------------------------------

class LowConfidenceClient(RecordingClient):
    def classify(self, image_bytes):
        super().classify(image_bytes)
        return {**OK, "location_confidence": "Low"}


def _review_cols(auto):
    header, rows, _, _ = read_report(auto / "output" / "Week 30" / REPORT30)
    assert header[-3:] == ["Result Source", "Activity Confidence", "Remarks"]
    return [dict(zip(header, r)) for r in rows]


def test_activity_confidence_from_result_and_blank_remarks_when_classified(proj):
    root, auto, settings = proj
    run(root, auto, settings, RecordingClient(), ("Week 30",))
    rows = _review_cols(auto)
    ai = [r for r in rows if r["Result Source"] != run_pipeline.SOURCE_OFFICIAL]
    labelled = [r for r in rows if r["Result Source"] == run_pipeline.SOURCE_OFFICIAL]
    assert ai and labelled
    assert {r["Activity Confidence"] for r in ai} == {"High"}          # copied, never invented
    assert all(r["Classification Status"] == "CLASSIFIED" and not r["Remarks"] for r in ai)
    assert all(r["Activity Confidence"] is None and not r["Remarks"] for r in labelled)


def test_remarks_reuse_existing_review_and_failure_reasons(proj):
    root, auto, settings = proj
    run(root, auto, settings, LowConfidenceClient(), ("Week 30",))
    review = [r for r in _review_cols(auto) if r["Classification Status"] == "VISUAL_REVIEW"]
    assert review and {r["Remarks"] for r in review} == {
        "Low/Needs-Review confidence on activity or location"}

    root2 = build_project(auto.parent / "project2")
    run(root2, auto, settings, RecordingClient({1: "err"}), ("Week 30",), force=True)
    rows = _review_cols(auto)
    failed = [r for r in rows if r["Classification Status"] == "API_FAILURE"]
    assert failed and all("simulated failure" in r["Remarks"] for r in failed)
    assert all(r["Activity Confidence"] == "Needs Review" for r in failed)


def test_remarks_helper_never_invents_text():
    res = run_pipeline.classifier.ClassificationResult(
        activity_identified="x", activity_confidence="High", ai_suggested_location="Main Deck",
        location_confidence="High", person_visible="No")
    assert run_pipeline._remarks(None, "CLASSIFIED") == ""
    assert run_pipeline._remarks(res, "CLASSIFIED") == ""
    assert run_pipeline._remarks(res, run_pipeline.DRY_RUN_STATUS) == run_pipeline.DRY_RUN_REASON
