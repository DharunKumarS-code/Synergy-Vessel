"""Append-only, hash-chained JSONL audit log.

Each line is one event::

    {"seq": n, "at": ISO-8601, "event": "...", "actor": {...}|null, "target": str|null,
     "details": {...}, "prev_hash": <hash of entry n-1 or GENESIS>, "hash": sha256(canonical entry without "hash")}

Modifying, removing or reordering a line breaks the chain. Truncating the newest lines is detected
through ``audit.head.json`` (last seq + hash, rewritten atomically after every append). Writes are
serialised by a process lock and appended with flush + fsync; lines are never rewritten.

Values whose keys look sensitive (password, token, secret, key, authorization, cookie) are never
written -- they are replaced by "[redacted]" -- so callers cannot leak credentials by mistake.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

GENESIS = "0" * 64
LOG_NAME = "audit.jsonl"
HEAD_NAME = "audit.head.json"
SENSITIVE_KEY = re.compile(r"pass(word)?|token|secret|api[_-]?key|authori[sz]ation|cookie|credential|bearer", re.I)


def _canonical(entry: dict) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if SENSITIVE_KEY.search(str(k)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class VerifyResult:
    valid: bool
    entries: int
    problems: list[str] = field(default_factory=list)
    last_hash: str = GENESIS

    def as_dict(self) -> dict:
        return {"valid": self.valid, "entries": self.entries, "problems": self.problems[:50], "last_hash": self.last_hash}


class AuditLog:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / LOG_NAME
        self.head_path = self.dir / HEAD_NAME
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ write
    def _tail(self) -> tuple[int, str]:
        """(seq, hash) of the last line actually in the log."""
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with open(self.path, "rb") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        e = json.loads(last)
        return int(e["seq"]), str(e["hash"])

    def record(self, event: str, actor: Optional[dict] = None, target: Optional[str] = None,
               details: Optional[dict] = None, *, at: Optional[dt.datetime] = None) -> dict:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            seq, prev = self._tail()
            entry = {
                "seq": seq + 1,
                "at": (at or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"),
                "event": event,
                "actor": scrub(actor) if actor else None,
                "target": None if target is None else str(target)[:200],
                "details": scrub(details or {}),
                "prev_hash": prev,
            }
            entry["hash"] = entry_hash(entry)
            with open(self.path, "ab") as fh:
                fh.write(_canonical(entry) + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            _atomic_write(self.head_path, json.dumps({"seq": entry["seq"], "hash": entry["hash"]}).encode())
            return entry

    # ------------------------------------------------------------------ read
    def entries(self, limit: int = 200, event: Optional[str] = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if event is None or e.get("event") == event:
                    out.append(e)
        return list(reversed(out))[:limit]

    def verify(self) -> VerifyResult:
        problems: list[str] = []
        prev, expected_seq, count = GENESIS, 1, 0
        if self.path.exists():
            with open(self.path, "rb") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        problems.append(f"line {lineno}: not valid JSON")
                        continue
                    count += 1
                    if e.get("seq") != expected_seq:
                        problems.append(f"line {lineno}: sequence {e.get('seq')} where {expected_seq} was expected "
                                        "(an entry was removed, inserted or reordered)")
                    if e.get("prev_hash") != prev:
                        problems.append(f"line {lineno}: does not chain to the previous entry")
                    if e.get("hash") != entry_hash(e):
                        problems.append(f"line {lineno}: content does not match its hash (entry was modified)")
                    prev = str(e.get("hash"))
                    expected_seq = (e.get("seq") if isinstance(e.get("seq"), int) else expected_seq) + 1
        head = None
        if self.head_path.exists():
            try:
                head = json.loads(self.head_path.read_text(encoding="utf-8"))
            except ValueError:
                problems.append("the audit head record is unreadable")
        if head and (head.get("seq") != count or head.get("hash") != prev):
            problems.append(f"the log ends at entry {count} but the head record expects entry {head.get('seq')} "
                            "(entries were removed from the end, or the log was replaced)")
        if head is None and count and not problems:
            problems.append("the audit head record is missing")
        return VerifyResult(valid=not problems, entries=count, problems=problems, last_hash=prev)
