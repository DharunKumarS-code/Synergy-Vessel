"""Phase 10.3 -- administrator-managed location synonyms.

One JSON file, <import_dir>/location_synonyms.json (covered by backups with the rest of the import folder), rewritten
atomically. Synonyms are created ONLY through the admin API: imported Excel text, review decisions and AI suggestions
never create one. Each synonym maps one variation to one trusted canonical location; conflicts are rejected, never
overwritten. The rules re-check every synonym when the known-location list is built (see rules.KnownLocations)."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..location_intel.rules import KnownLocations, _numbers, key
from ..location_intel.service import NON_LOCATION_KEYS
from .store import write_atomic                     # temp file + fsync + os.replace

SYNONYMS_FILE = "location_synonyms.json"
MAX_TEXT = 200


class SynonymFailure(Exception):
    def __init__(self, status: int, code: str, message: str, guidance: str = ""):
        super().__init__(message)
        self.status, self.code, self.message, self.guidance = status, code, message, guidance


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SynonymStore:
    def __init__(self, import_dir: Path):
        self.dir = import_dir
        self.path = import_dir / SYNONYMS_FILE
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ persistence
    def load(self) -> tuple[list[dict], Optional[str]]:
        """(entries, problem). An unreadable file yields no synonyms (fail safe: fewer automatic matches)."""
        if not self.path.exists():
            return [], None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data["synonyms"]
            if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("id"), str)
                                                        and isinstance(e.get("synonym"), str)
                                                        and isinstance(e.get("canonical"), str) for e in entries):
                raise ValueError
            return entries, None
        except (OSError, ValueError, KeyError, TypeError):
            return [], f"{SYNONYMS_FILE} is unreadable; managed synonyms are ignored until it is restored"

    def mapping(self) -> dict[str, str]:
        return {e["synonym"]: e["canonical"] for e in self.load()[0]}

    def _save(self, entries: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        body = {"version": 1, "synonyms": sorted(entries, key=lambda e: (e["canonical"].casefold(), e["synonym"].casefold()))}
        write_atomic(self.dir, SYNONYMS_FILE, json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8"))

    def _writable(self) -> list[dict]:
        entries, problem = self.load()
        if problem:
            raise SynonymFailure(409, "synonyms_unreadable", problem + ".", "Restore the file from a backup first.")
        return entries

    # ------------------------------------------------------------------ validation
    @staticmethod
    def validate(synonym, canonical, known: KnownLocations, entries: list[dict], exclude_id: Optional[str] = None
                 ) -> tuple[str, str]:
        syn, canon_in = _clean(synonym), _clean(canonical)
        if not canon_in:
            raise SynonymFailure(422, "empty_canonical", "Choose the canonical location.")
        if not syn:
            raise SynonymFailure(422, "empty_synonym", "Enter the synonym text.")
        if len(syn) > MAX_TEXT or len(canon_in) > MAX_TEXT:
            raise SynonymFailure(422, "too_long", f"Synonyms and locations are limited to {MAX_TEXT} characters.")
        sk = key(syn)
        if not sk or sk == known.placeholder_key or sk in NON_LOCATION_KEYS:
            raise SynonymFailure(422, "invalid_synonym", f"'{syn}' is not a location and cannot be a synonym.")
        canon = known.resolve(canon_in)
        if canon is None:
            raise SynonymFailure(422, "unknown_canonical", f"'{canon_in}' is not a trusted location.",
                                 "A canonical location must already exist in the dataset or have been approved in review.")
        ck = key(canon)
        others = [e for e in entries if e["id"] != exclude_id]
        if any(key(e["synonym"]) == ck for e in others):
            raise SynonymFailure(409, "circular", f"'{canon}' is itself a managed synonym and cannot be a canonical location.")
        if sk == ck:
            raise SynonymFailure(409, "redundant", f"'{syn}' already matches '{canon}' automatically.",
                                 "Synonyms are only needed for wording the rules do not already recognise.")
        if sk in known.by_key:
            raise SynonymFailure(409, "is_location", f"'{syn}' already matches the existing location '{known.by_key[sk]}'.",
                                 "An existing location cannot be made a synonym of another one.")
        for e in others:
            if key(e["synonym"]) == sk:
                if key(e["canonical"]) == ck:
                    raise SynonymFailure(409, "duplicate", f"'{syn}' is already a synonym of '{e['canonical']}' ('{e['synonym']}').")
                raise SynonymFailure(409, "conflict", f"'{syn}' is already a synonym of '{e['canonical']}'.",
                                     "Edit or delete the existing synonym first; mappings are never overwritten.")
        if sk in known.alias_keys:
            if known.alias_keys[sk] == canon:
                raise SynonymFailure(409, "redundant", f"'{syn}' is already a built-in synonym of '{canon}'.")
            raise SynonymFailure(409, "conflict", f"'{syn}' is already a built-in synonym of '{known.alias_keys[sk]}'.")
        if _numbers(sk) != _numbers(ck):
            raise SynonymFailure(422, "number_mismatch", f"The numbers in '{syn}' do not match '{canon}'.",
                                 "A synonym must carry exactly the same location numbers as its canonical location.")
        return syn, canon

    # ------------------------------------------------------------------ operations
    def listing(self, known: KnownLocations) -> dict:
        entries, problem = self.load()
        out = []
        for e in sorted(entries, key=lambda e: (e["canonical"].casefold(), e["synonym"].casefold(), e["id"])):
            sk = key(e["synonym"])
            active = known.managed_keys.get(sk)
            if active is not None:
                status, reason = "active", None
            elif known.resolve(e["canonical"]) is None:
                status, reason = "inactive", "The canonical location is no longer a trusted location."
            elif sk in known.by_key:
                status, reason = "inactive", f"Now matches the existing location '{known.by_key[sk]}' directly."
            else:
                status, reason = "inactive", "Not usable by the rules."
            out.append({**e, "status": status, "status_reason": reason})
        return {"synonyms": out, "count": len(out), "canonical_locations": known.names,
                "problems": [problem] if problem else []}

    def create(self, synonym, canonical, known: KnownLocations, actor: dict) -> dict:
        with self._lock:
            entries = self._writable()
            syn, canon = self.validate(synonym, canonical, known, entries)
            now = _now()
            entry = {"id": f"SYN-{secrets.token_hex(6)}", "synonym": syn, "canonical": canon,
                     "created_at": now, "created_by": actor, "updated_at": now, "updated_by": actor}
            self._save(entries + [entry])
            return entry

    def update(self, synonym_id: str, synonym, canonical, known: KnownLocations, actor: dict) -> dict:
        with self._lock:
            entries = self._writable()
            current = self._find(entries, synonym_id)
            syn, canon = self.validate(synonym, canonical, known, entries, exclude_id=synonym_id)
            entry = {**current, "synonym": syn, "canonical": canon, "updated_at": _now(), "updated_by": actor}
            self._save([entry if e["id"] == synonym_id else e for e in entries])
            return {"synonym": entry, "previous": current}

    def delete(self, synonym_id: str) -> dict:
        with self._lock:
            entries = self._writable()
            current = self._find(entries, synonym_id)
            self._save([e for e in entries if e["id"] != synonym_id])
            return current

    @staticmethod
    def _find(entries: list[dict], synonym_id: str) -> dict:
        for e in entries:
            if e["id"] == synonym_id:
                return e
        raise SynonymFailure(404, "not_found", "That synonym does not exist (it may have been deleted).")
