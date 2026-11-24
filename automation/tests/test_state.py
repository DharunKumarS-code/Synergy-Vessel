"""Tests for the content-hash state ledger, including the Phase M "ok" success/failure field
that lets a failed API request be automatically retried without --force (see state.py and
run_pipeline.process_week's "ok": result.error is None write), and the Phase O atomic-write
guarantee (temp file + os.replace()) in save_state().
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src import state


def test_image_hash_is_deterministic_and_distinguishes_content():
    a = state.image_hash(b"photo-one")
    b = state.image_hash(b"photo-one")
    c = state.image_hash(b"photo-two")
    assert a == b
    assert a != c


def test_unseen_hash_needs_processing():
    assert state.needs_processing({}, "hash-x", force=False) is True


def test_successful_entry_is_skipped_on_rerun():
    week_state = {"hash-x": {"ok": True, "activity_identified": "No activity observed",
                              "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                              "location_confidence": "High", "person_visible": "No"}}
    assert state.needs_processing(week_state, "hash-x", force=False) is False


def test_failed_entry_is_retried_without_force():
    week_state = {"hash-x": {"ok": False, "activity_identified": "Needs Review",
                              "activity_confidence": "Needs Review", "ai_suggested_location": "Needs Review",
                              "location_confidence": "Needs Review", "person_visible": "Uncertain"}}
    assert state.needs_processing(week_state, "hash-x", force=False) is True


def test_ordinary_needs_review_without_api_error_is_treated_as_completed():
    # a legitimate, successful API call whose answer happens to be "Needs Review" (e.g. an
    # unrecognized location, or an ambiguous person) is still a completed request -- ok=True --
    # and must NOT be reprocessed just because its confidence/activity values are "Needs Review".
    week_state = {"hash-x": {"ok": True, "activity_identified": "Needs Review",
                              "activity_confidence": "Needs Review", "ai_suggested_location": "Needs Review",
                              "location_confidence": "Needs Review", "person_visible": "Uncertain"}}
    assert state.needs_processing(week_state, "hash-x", force=False) is False


def test_legacy_entry_without_ok_field_remains_compatible():
    # a state file saved before this field existed has no "ok" key at all -- must be treated
    # exactly as it always was (successful, not reprocessed), never silently invalidated.
    week_state = {"hash-x": {"activity_identified": "No activity observed", "activity_confidence": "High",
                              "ai_suggested_location": "Main Deck", "location_confidence": "High",
                              "person_visible": "No"}}
    assert state.needs_processing(week_state, "hash-x", force=False) is False


def test_force_always_reprocesses_regardless_of_ok():
    week_state = {"hash-x": {"ok": True, "activity_identified": "No activity observed",
                              "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                              "location_confidence": "High", "person_visible": "No"}}
    assert state.needs_processing(week_state, "hash-x", force=True) is True


def test_duplicate_content_hash_does_not_trigger_duplicate_processing():
    # two images with identical bytes hash identically -- once one is recorded successful, the
    # same hash is never flagged as needing processing again, regardless of how many images in
    # the week happen to share it (this is the existing dedup guarantee; confirmed unaffected by
    # the "ok" field).
    content_hash = state.image_hash(b"identical-photo-bytes")
    week_state = {}
    assert state.needs_processing(week_state, content_hash, force=False) is True
    week_state[content_hash] = {"ok": True, "activity_identified": "No activity observed",
                                 "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                                 "location_confidence": "High", "person_visible": "No"}
    assert state.needs_processing(week_state, content_hash, force=False) is False
    assert state.needs_processing(week_state, content_hash, force=False) is False  # repeated check, same result


def test_failed_entry_becomes_successful_after_later_reprocessing():
    week_state = {"hash-x": {"ok": False, "activity_identified": "Needs Review",
                              "activity_confidence": "Needs Review", "ai_suggested_location": "Needs Review",
                              "location_confidence": "Needs Review", "person_visible": "Uncertain"}}
    assert state.needs_processing(week_state, "hash-x", force=False) is True

    # simulate run_pipeline.process_week() overwriting the entry after a successful reprocessing
    week_state["hash-x"] = {"ok": True, "activity_identified": "No activity observed",
                             "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                             "location_confidence": "High", "person_visible": "No"}
    assert state.needs_processing(week_state, "hash-x", force=False) is False


def test_save_and_load_state_round_trip_preserves_ok_field(tmp_path):
    week_state = {"hash-x": {"ok": False, "activity_identified": "Needs Review",
                              "activity_confidence": "Needs Review", "ai_suggested_location": "Needs Review",
                              "location_confidence": "Needs Review", "person_visible": "Uncertain"}}
    state.save_state(tmp_path, "Week 99", week_state)
    loaded = state.load_state(tmp_path, "Week 99")
    assert loaded == week_state
    assert state.needs_processing(loaded, "hash-x", force=False) is True


# ---------------------------------------------------------------------------
# Phase O: atomic writes (temp file + os.replace()).
# ---------------------------------------------------------------------------


def test_no_leftover_temp_files_after_a_successful_save(tmp_path):
    state.save_state(tmp_path, "Week 99", {"hash-x": {"ok": True}})
    names = [p.name for p in tmp_path.iterdir()]
    assert names == ["Week_99.json"]  # no stray ".tmp" file left behind


def test_existing_state_survives_a_simulated_replacement_failure(tmp_path, monkeypatch):
    original = {"hash-x": {"ok": True, "activity_identified": "No activity observed",
                            "activity_confidence": "High", "ai_suggested_location": "Main Deck",
                            "location_confidence": "High", "person_visible": "No"}}
    state.save_state(tmp_path, "Week 99", original)

    def _boom(*args, **kwargs):
        raise OSError("simulated os.replace failure")

    monkeypatch.setattr(state.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated os.replace failure"):
        state.save_state(tmp_path, "Week 99", {"hash-y": {"ok": True}})

    # the original file must be completely untouched -- os.replace() never ran, and the
    # write happened only to the (now-deleted) temp file, never to the real path.
    assert state.load_state(tmp_path, "Week 99") == original


def test_temp_file_is_cleaned_up_after_a_simulated_replacement_failure(tmp_path, monkeypatch):
    state.save_state(tmp_path, "Week 99", {"hash-x": {"ok": True}})

    monkeypatch.setattr(state.os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        state.save_state(tmp_path, "Week 99", {"hash-y": {"ok": True}})

    leftover_tmp_files = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftover_tmp_files == []


def test_save_state_is_a_real_atomic_replace_not_an_in_place_write(tmp_path, monkeypatch):
    # confirm the implementation actually goes through a separate temp file and os.replace(),
    # not just path.write_text() dressed up differently -- guards against a future edit
    # silently reverting to the non-atomic approach this phase was meant to fix.
    calls = []
    real_replace = state.os.replace

    def _tracking_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(state.os, "replace", _tracking_replace)
    state.save_state(tmp_path, "Week 99", {"hash-x": {"ok": True}})

    assert len(calls) == 1
    src, dst = calls[0]
    assert src != dst
    assert dst == str(tmp_path / "Week_99.json")
    assert ".tmp" in src


# --- Pre-AI audit (2026-11-16): state-file robustness ---------------------------------------


def test_audit_save_state_removes_temp_file_on_keyboard_interrupt(tmp_path, monkeypatch):
    state.save_state(tmp_path, "Week 99", {"old": {"ok": True}})
    before = (tmp_path / "Week_99.json").read_bytes()

    def _interrupt(fd):
        raise KeyboardInterrupt()

    monkeypatch.setattr(state.os, "fsync", _interrupt)
    with pytest.raises(KeyboardInterrupt):  # propagates unchanged, never swallowed
        state.save_state(tmp_path, "Week 99", {"new": {"ok": True}})
    assert [p.name for p in tmp_path.iterdir()] == ["Week_99.json"]  # no stray .tmp file
    assert (tmp_path / "Week_99.json").read_bytes() == before  # previous state untouched


@pytest.mark.parametrize("text, fragment", [
    ("{ not json", "is not valid JSON"),
    ("", "is not valid JSON"),
    ("[1, 2]", "must contain a JSON object, got list"),
    ('"text"', "must contain a JSON object, got str"),
    ("null", "must contain a JSON object, got NoneType"),
])
def test_audit_corrupt_state_file_is_a_clear_error_and_left_untouched(tmp_path, text, fragment):
    path = tmp_path / "Week_99.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(state.StateFileError) as exc_info:
        state.load_state(tmp_path, "Week 99")
    message = str(exc_info.value)
    assert fragment in message and str(path) in message and "was not modified" in message
    assert isinstance(exc_info.value, ValueError)  # existing `except ValueError` still applies
    assert path.read_text(encoding="utf-8") == text


def test_audit_non_utf8_state_file_is_a_clear_error(tmp_path):
    (tmp_path / "Week_99.json").write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(state.StateFileError, match="is not valid JSON"):
        state.load_state(tmp_path, "Week 99")


def test_audit_valid_state_file_loads_unchanged(tmp_path):
    state.save_state(tmp_path, "Week 99", {"h": {"ok": True, "activity_identified": "x"}})
    assert state.load_state(tmp_path, "Week 99") == {"h": {"ok": True, "activity_identified": "x"}}
    assert state.load_state(tmp_path, "Week 1") == {}  # missing file still means empty state
