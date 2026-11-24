from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def image_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def state_path(state_dir: Path, week_name: str) -> Path:
    safe_name = week_name.strip().replace(" ", "_")
    return state_dir / f"{safe_name}.json"


class StateFileError(ValueError):
    """A week's state file exists but is not a valid JSON object. Names the file; the file is
    never modified (returning {} instead would let the next save overwrite every saved result).
    Subclasses ValueError, so existing `except ValueError` handling still applies."""


def load_state(state_dir: Path, week_name: str) -> dict[str, Any]:
    path = state_path(state_dir, week_name)
    if not path.exists():
        return {}
    advice = ("The file was not modified. Restore it from a backup before rerunning this week "
              "(moving it aside makes every photo in this week be sent again).")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # json.JSONDecodeError or UnicodeDecodeError
        raise StateFileError(f"state file {path} is not valid JSON "
                             f"({type(exc).__name__}: {exc}). {advice}") from exc
    if not isinstance(data, dict):
        raise StateFileError(f"state file {path} must contain a JSON object, got "
                             f"{type(data).__name__}. {advice}")
    return data


def save_state(state_dir: Path, week_name: str, state: dict[str, Any]) -> None:
    """Atomic write: serialize to a temp file in the same directory, flush+fsync it, then
    os.replace() it over the real path. os.replace() is atomic on both POSIX and Windows when
    source and destination are on the same filesystem (unlike os.rename(), which raises on
    Windows if the destination already exists) -- a crash at any point before that final
    replace leaves the original file completely untouched, never partially overwritten. The
    temp file lives in state_dir itself (not a system temp dir) specifically so it's always on
    the same filesystem as the target, which os.replace()'s atomicity guarantee requires.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(state_dir, week_name)
    data = json.dumps(state, indent=2)

    fd, tmp_name = tempfile.mkstemp(dir=str(state_dir), prefix=f"{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # BaseException (same as report_builder / the manifest writer): a Ctrl+C mid-write also
        # removes our own temp file. The previous state file is untouched either way.
        try:
            tmp_path.unlink(missing_ok=True)  # never leave a stray .tmp file behind on failure
        except OSError:
            pass  # never let clean-up mask the original error
        raise


def needs_processing(state: dict[str, Any], content_hash: str, force: bool) -> bool:
    """True if this image has never been (successfully) classified, or --force was passed.

    An entry saved before the "ok" field existed has no "ok" key -- .get(..., True) treats it
    as successful, preserving old state files' original behavior exactly (never silently
    reprocessed just because of this change). A failed attempt (ok=False, e.g. a quota-exhausted
    or errored API call recorded by run_pipeline.process_week) is NOT treated as done -- it is
    retried automatically on the next run, without needing --force.
    """
    if force:
        return True
    if content_hash not in state:
        return True
    return not state[content_hash].get("ok", True)


# --- M3: cross-week reuse of successful classifications for byte-identical photos ------------

# The classification fields a state entry carries (classifier.ClassificationResult's saved fields).
RESULT_FIELDS = ("activity_identified", "activity_confidence", "ai_suggested_location",
                 "location_confidence", "person_visible")


def week_key(week_name: str) -> str:
    """The state-file stem for a week (the same mapping state_path() uses)."""
    return week_name.strip().replace(" ", "_")


def is_reusable_entry(entry: Any, provenance: dict | None) -> bool:
    """M3: True only for an entry that is safe to copy into another week: an explicit successful
    result ("ok" is exactly True -- a legacy entry with no "ok" key does not qualify), every
    classification field present as a non-empty string, and a recorded provenance (provider,
    model, prompt/schema fingerprint) identical to the current one. Entries without provenance
    (written before M3) are never reused, because which model/prompt produced them is unknown."""
    if provenance is None or not isinstance(entry, dict) or entry.get("ok") is not True:
        return False
    if entry.get("provenance") != provenance:
        return False
    return all(isinstance(entry.get(f), str) and entry.get(f).strip() for f in RESULT_FIELDS)


class CrossWeekResults:
    """M3: reusable successful results from every week's state file in one state directory,
    keyed by SHA-256 image hash. Read-only over the files; run_pipeline copies a reused result
    into the receiving week's own state file. lookup() returns None -- send the photo -- when no
    other week has a reusable result, or when other weeks' reusable results disagree
    (ambiguous: never pick one silently)."""

    def __init__(self, provenance: dict):
        self.provenance = provenance
        self._by_hash: dict[str, dict[str, dict]] = {}  # hash -> {week_key: entry}
        self.unreadable: list[str] = []  # state files that could not be parsed (never reused; logged)

    @classmethod
    def from_state_dir(cls, state_dir: Path, provenance: dict) -> "CrossWeekResults":
        results = cls(provenance)
        if not state_dir.is_dir():
            return results
        for path in sorted(state_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                results.unreadable.append(path.name)
                continue
            if not isinstance(data, dict):
                results.unreadable.append(path.name)
                continue
            for content_hash, entry in data.items():
                results._add(path.stem, content_hash, entry)
        return results

    def _add(self, key: str, content_hash: str, entry: Any) -> None:
        if is_reusable_entry(entry, self.provenance):
            self._by_hash.setdefault(content_hash, {})[key] = dict(entry)

    def add(self, week_name: str, content_hash: str, entry: dict) -> None:
        """Register a result this run just saved, so later weeks in the same run can reuse it."""
        self._add(week_key(week_name), content_hash, entry)

    def lookup(self, content_hash: str, receiving_week: str) -> tuple[str, dict] | None:
        """(source week key, entry) of a reusable result for this hash from ANOTHER week, or
        None when there is none or the candidates disagree."""
        own = week_key(receiving_week)
        candidates = {k: e for k, e in self._by_hash.get(content_hash, {}).items() if k != own}
        if not candidates:
            return None
        if len({tuple(e[f] for f in RESULT_FIELDS) for e in candidates.values()}) != 1:
            return None
        source = sorted(candidates)[0]
        return source, candidates[source]

    def is_conflicting(self, content_hash: str, receiving_week: str) -> bool:
        own = week_key(receiving_week)
        candidates = [e for k, e in self._by_hash.get(content_hash, {}).items() if k != own]
        return len({tuple(e[f] for f in RESULT_FIELDS) for e in candidates}) > 1
