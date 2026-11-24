"""On-disk layout of committed imports (append-only) and their loading.

    <import_dir>/
      IMP-YYYYMMDD-xxxxxxxxxx/        one folder per committed import, published by a single rename
        source.xlsx                   byte-exact copy of the uploaded workbook (never modified)
        images/<sha256>.<ext>         each unique photograph once, content-addressed
        manifest.json                 import summary + per-row results (written last)
        status.json                   Phase 8.4: withdrawal / reinstatement history (absent = active)
      attempts/ATT-*.json             failed / rejected commit attempts (no record data)
      .staging-*                      incomplete work; never loaded, removed by the next import
      .import.lock                    Phase 8.4: cross-process lock held while an import changes state

This module deliberately does not import the inspection store (no circular imports).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Iterator, Optional

IMPORT_ID_RE = re.compile(r"^IMP-\d{8}-[0-9a-f]{10}$")
MANIFEST = "manifest.json"
SOURCE = "source.xlsx"
IMAGES = "images"
ATTEMPTS = "attempts"
STAGING_PREFIX = ".staging-"


def write_file(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def iter_committed(import_dir: Path, problems: list[str] | None = None) -> Iterator[tuple[dict, Path]]:
    """Yield (manifest, folder) for every readable committed import, oldest first."""
    if not import_dir.is_dir():
        return
    for folder in sorted(p for p in import_dir.iterdir() if p.is_dir() and IMPORT_ID_RE.match(p.name)):
        try:
            manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
            if manifest.get("import_id") != folder.name or not isinstance(manifest.get("rows"), list):
                raise ValueError("manifest does not match its folder")
        except (OSError, ValueError):
            if problems is not None:
                problems.append(f"import '{folder.name}' could not be read and was ignored")
            continue
        yield manifest, folder


def staging_dirs(import_dir: Path) -> list[Path]:
    if not import_dir.is_dir():
        return []
    return [p for p in import_dir.iterdir() if p.is_dir() and p.name.startswith(STAGING_PREFIX)]


# ============================================================================ Phase 8.4: import state
STATUS = "status.json"
LOCK = ".import.lock"
ACTIVE, WITHDRAWN = "active", "withdrawn"


def read_state(folder: Path) -> tuple[str, list[dict], Optional[str]]:
    """(state, events, problem). No status file -> active. An unreadable status file keeps the import
    active (its records stay visible, as before) and is reported as a problem."""
    path = folder / STATUS
    if not path.exists():
        return ACTIVE, [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state, events = data["state"], data["events"]
        if state not in (ACTIVE, WITHDRAWN) or not isinstance(events, list):
            raise ValueError
        return state, events, None
    except (OSError, ValueError, KeyError, TypeError):
        return ACTIVE, [], f"import '{folder.name}' has an unreadable status file; it is treated as active"


def write_state_bytes(folder: Path, data: Optional[bytes]) -> None:
    """Atomically replace status.json (data=None removes it). Used for changes and for rollback."""
    path = folder / STATUS
    if data is None:
        if path.exists():
            path.unlink()
        return
    tmp = folder / f".{STATUS}.{secrets.token_hex(4)}.tmp"
    try:
        write_file(tmp, data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class LockBusy(RuntimeError):
    pass


class ImportLock:
    """Cross-process lock: an exclusively created lock file. A lock older than ``stale_after`` seconds is
    taken over (its holder crashed -- import operations take seconds). Release only removes our own lock."""

    def __init__(self, import_dir: Path, *, timeout: float = 15.0, stale_after: float = 600.0):
        self.path = Path(import_dir) / LOCK
        self.timeout, self.stale_after = timeout, stale_after
        self.token: Optional[str] = None

    def _try_create(self) -> bool:
        token = secrets.token_hex(8)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "pid": os.getpid(), "host": socket.gethostname(),
                       "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        self.token = token
        return True

    def _break_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if age < self.stale_after:
            return
        grave = self.path.with_name(f"{LOCK}.stale-{secrets.token_hex(4)}")
        try:
            os.rename(self.path, grave)  # only one contender can win this rename
            grave.unlink()
        except OSError:
            pass

    def acquire(self) -> "ImportLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while not self._try_create():
            self._break_if_stale()
            if time.monotonic() >= deadline:
                raise LockBusy("another import operation is in progress")
            time.sleep(0.05)
        return self

    def release(self) -> None:
        if not self.token:
            return
        try:
            if json.loads(self.path.read_text(encoding="utf-8")).get("token") == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass
        self.token = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


# ============================================================================ Phase 9: location review decisions
LOCATIONS = "locations.json"


def read_decisions(folder: Path) -> tuple[dict, Optional[str]]:
    """({row_number(str): decision}, problem). Absent -> no decisions. Unreadable -> ignored and reported, so
    records fall back to their automatic / original locations."""
    path = folder / LOCATIONS
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        decisions = data["decisions"]
        if not isinstance(decisions, dict):
            raise ValueError
        return decisions, None
    except (OSError, ValueError, KeyError, TypeError):
        return {}, f"import '{folder.name}' has an unreadable location review file; its decisions are ignored"


def write_atomic(folder: Path, name: str, data: Optional[bytes]) -> None:
    """Atomically replace folder/name (None removes it)."""
    path = folder / name
    if data is None:
        if path.exists():
            path.unlink()
        return
    tmp = folder / f".{name}.{secrets.token_hex(4)}.tmp"
    try:
        write_file(tmp, data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
