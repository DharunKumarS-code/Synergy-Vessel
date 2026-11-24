"""Separate, append-friendly store for condition analyses.

Layout: ``<analysis_dir>/<analysis_id>.json`` -- one file per analysis, written atomically
(temporary file in the same folder -> flush + fsync -> ``os.replace``), so an interrupted write
leaves either the previous complete file or no file, never a partial one. Leftover temporary
files and unreadable files are reported by ``status()`` and skipped, never deleted.

This store holds ONLY dashboard condition analyses. It never touches inspection JSON reports,
registers, photographs, Official/AI locations, or automation state.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Iterable, Optional

from pydantic import ValidationError

from .records import AnalysisRecord

TMP_SUFFIX = ".tmp"


class AnalysisStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._lock = threading.RLock()
        self._by_id: dict[str, AnalysisRecord] = {}
        self._unreadable: list[str] = []
        self._leftover_tmp: list[str] = []
        self._loaded = False
        # Increases on every load and save (including reviews); caches of derived data key on it.
        self.version = 0

    # ------------------------------------------------------------------ loading
    def load(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._unreadable.clear()
            self._leftover_tmp.clear()
            if self.directory.is_dir():
                for p in sorted(self.directory.iterdir()):
                    if p.name.endswith(TMP_SUFFIX):
                        self._leftover_tmp.append(p.name)
                        continue
                    if p.suffix != ".json":
                        continue
                    try:
                        rec = AnalysisRecord.model_validate_json(p.read_text(encoding="utf-8"))
                        if rec.analysis_id != p.stem:
                            raise ValueError("file name does not match analysis_id")
                        self._by_id[rec.analysis_id] = rec
                    except (OSError, ValueError, ValidationError):
                        self._unreadable.append(p.name)
            self._loaded = True
            self.version += 1

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------ writing
    def _write(self, rec: AnalysisRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.directory / f"{rec.analysis_id}.json"
        tmp = self.directory / f"{rec.analysis_id}.{uuid.uuid4().hex[:8]}{TMP_SUFFIX}"
        data = rec.model_dump_json(indent=2)
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()  # only our own temporary file, after a failed replace

    def save(self, rec: AnalysisRecord) -> AnalysisRecord:
        with self._lock:
            self._ensure()
            self._write(rec)
            self._by_id[rec.analysis_id] = rec
            self.version += 1
            return rec

    # ------------------------------------------------------------------ queries
    def get(self, analysis_id: str) -> Optional[AnalysisRecord]:
        with self._lock:
            self._ensure()
            return self._by_id.get(analysis_id)

    def all(self) -> list[AnalysisRecord]:
        with self._lock:
            self._ensure()
            return sorted(self._by_id.values(), key=lambda r: r.created_at, reverse=True)

    def latest_for_key(self, dedup_key: str) -> Optional[AnalysisRecord]:
        return next((r for r in self.all() if r.dedup_key == dedup_key), None)

    def find(self, **filters) -> Iterable[AnalysisRecord]:
        return [r for r in self.all() if r.matches(**filters)]

    def status(self) -> dict:
        with self._lock:
            self._ensure()
            return {
                "stored_analyses": len(self._by_id),
                "unreadable_files": len(self._unreadable),
                "interrupted_writes": len(self._leftover_tmp),
            }

