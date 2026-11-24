"""Import preview / commit / history. All writes go to the separate import store, atomically."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from ..config import Settings
from ..services.inspection_store import InspectionStore, normalise, parse_report_date
from ..services.json_loader import discover_weeks
from .parser import FORMAT_DECK, ParsedRow, ParsedWorkbook, WorkbookRejected, parse_workbook
from .store import (ACTIVE, STATUS, WITHDRAWN, ImportLock, LockBusy, read_state, write_state_bytes)
from .store import LOCATIONS, read_decisions, write_atomic
from ..location_intel.rules import HIGH, KnownLocations, _numbers, key, match
from ..location_intel.service import AI_CONFIDENCE_CAP, AUTO_METHODS, LocationIntelligence, effective_location
from ..location_intel.rules import band as confidence_band
from .synonyms import SynonymFailure, SynonymStore
from .store import (ATTEMPTS, IMAGES, IMPORT_ID_RE, MANIFEST, SOURCE, STAGING_PREFIX, iter_committed, staging_dirs,
                    write_file)

SCHEMA_VERSION = 1


class ImportFailure(Exception):
    def __init__(self, status: int, code: str, message: str, guidance: str = ""):
        super().__init__(message)
        self.status, self.code, self.message, self.guidance = status, code, message, guidance


def identity(excel: Optional[str], worksheet: Optional[str], report_date: Optional[str],
             serial: Optional[int], sha: Optional[str]) -> tuple:
    """What makes an inspection record the *same record*: its source workbook, worksheet, report date,
    register serial number and photograph. The same photograph under a different identity is a
    legitimate repeated appearance, not a duplicate."""
    d = parse_report_date(report_date)
    return (normalise(excel), normalise(worksheet), d.isoformat() if d else normalise(report_date), serial, sha)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ImportService:
    def __init__(self, settings: Settings, get_store: Callable[[], InspectionStore],
                 reload: Callable[[], None]):
        self.settings = settings
        self.dir = settings.import_dir
        self._get_store = get_store
        self._reload = reload
        self._lock = threading.Lock()
        self._register_shas: Optional[dict[str, str]] = None
        self.location_intel = LocationIntelligence(settings)   # Phase 9 (own AI configuration)
        self.synonyms = SynonymStore(settings.import_dir)       # Phase 10.3 (admin-managed only)

    # ------------------------------------------------------------ duplicate context
    def _production_registers(self) -> dict[str, str]:
        if self._register_shas is None:
            out = {}
            for week in discover_weeks(self.settings):
                # Phase 8.5: the week's register AND its original source workbooks (Deck Photos), read-only
                sources = {week.register_path} if week.register_path and week.register_path.is_file() else set()
                if week.folder.is_dir():
                    sources |= {f for f in week.folder.glob("*.xlsx") if f.is_file() and not f.name.startswith("~$")}
                for f in sorted(sources):
                    out[hashlib.sha256(f.read_bytes()).hexdigest()] = f"{week.folder.name}/{f.name}"
            self._register_shas = out
        return self._register_shas

    def _classify(self, wb: ParsedWorkbook) -> None:
        """Mark duplicates and repeated appearances against the current dataset (production + imports)."""
        store = self._get_store()
        if wb.sha256 in self._production_registers():
            raise WorkbookRejected("duplicate_workbook",
                                   f"This workbook is identical to the existing workbook '{self._production_registers()[wb.sha256]}'.",
                                   "Its records are already in the dataset. Nothing needs to be imported.")
        for manifest, _ in iter_committed(self.dir):
            if manifest.get("source_sha256") == wb.sha256:
                state = read_state(self.dir / manifest["import_id"])[0]
                raise WorkbookRejected("duplicate_workbook",
                                       f"This workbook was already imported as {manifest['import_id']} on {manifest['created_at'][:10]}"
                                       + (" (currently withdrawn)." if state == WITHDRAWN else "."),
                                       "Reinstate that import instead of importing it again." if state == WITHDRAWN else
                                       "Open that import in the history below instead of importing it again.")
        for folder in self._unreadable_imports():
            src = folder / SOURCE
            if src.is_file() and hashlib.sha256(src.read_bytes()).hexdigest() == wb.sha256:
                raise WorkbookRejected("duplicate_workbook",
                                       f"This workbook already exists as import {folder.name}, whose manifest cannot be read.",
                                       "Restore that import's manifest from a backup instead of importing the workbook again.")
        existing: dict[tuple, str] = {}
        by_sha: dict[str, list[str]] = {}
        for r in store.records:
            sha = r.extracted_photograph.sha256 if r.extracted_photograph else None
            if not sha:
                continue
            serial = r.register_serial_number if r.register_serial_number is not None else r.serial_number
            existing.setdefault(identity(r.original_excel_filename, r.worksheet_name, r.report_date, serial, sha), r.record_id)
            by_sha.setdefault(sha, []).append(r.record_id)
        # Phase 8.5: Deck Photos rows get serials from their position in the uploaded file, which need not match the
        # week-wide register serials. The same photograph on the same worksheet is therefore matched by count.
        by_sheet_sha: dict[tuple, list[str]] = {}
        if wb.source_format == FORMAT_DECK:
            for r in store.records:
                sha = r.extracted_photograph.sha256 if r.extracted_photograph else None
                if sha:
                    by_sheet_sha.setdefault((normalise(r.worksheet_name), sha), []).append(r.record_id)
        sheet_seen: dict[tuple, int] = {}
        seen_key: dict[tuple, int] = {}
        seen_sha: dict[str, int] = {}
        for row in wb.rows:
            if row.status != "ready":
                continue
            if wb.source_format == FORMAT_DECK:
                k = (normalise(row.worksheet_name), row.sha256)
                sheet_seen[k] = sheet_seen.get(k, 0) + 1
                matches = by_sheet_sha.get(k, [])
                if sheet_seen[k] <= len(matches):
                    row.status = "duplicate"
                    row.notes.append(f"Already in the dataset as record {matches[sheet_seen[k] - 1]} "
                                     "(same worksheet and photograph).")
                    continue
            key = identity(row.original_excel_filename, row.worksheet_name, row.report_date, row.serial_number, row.sha256)
            if key in existing:
                row.status = "duplicate"
                row.notes.append(f"Already in the dataset as record {existing[key]} (same workbook, worksheet, date, serial and photograph).")
                continue
            if key in seen_key:
                row.status = "duplicate"
                row.notes.append(f"Repeats row {seen_key[key]} of this workbook exactly.")
                continue
            seen_key[key] = row.row_number
            others = by_sha.get(row.sha256, [])
            if others:
                row.notes.append(f"Repeated appearance: this photograph is also used by {len(others)} existing record(s) "
                                 f"(e.g. {others[0]}). It is kept as a separate appearance.")
            elif row.sha256 in seen_sha:
                row.notes.append(f"Repeated appearance: the same photograph is also in row {seen_sha[row.sha256]} of this workbook.")
            seen_sha.setdefault(row.sha256, row.row_number)

    @staticmethod
    def _summary(wb: ParsedWorkbook) -> dict:
        count = lambda s: sum(1 for r in wb.rows if r.status == s)  # noqa: E731
        ready = count("ready")
        return {
            "total_rows": len(wb.rows),
            "records": sum(1 for r in wb.rows if r.status != "skipped"),
            "ready": ready,
            "duplicates": count("duplicate"),
            "blank_rows": count("skipped"),
            "skipped": count("duplicate") + count("skipped"),
            "failed": count("invalid"),
            "repeated_appearances": sum(1 for r in wb.rows if r.status == "ready" and any(n.startswith("Repeated appearance") for n in r.notes)),
            "unique_photographs": len({r.sha256 for r in wb.rows if r.status == "ready"}),
            "warnings": sum(1 for r in wb.rows if r.warnings),
        }

    @staticmethod
    def _row_dict(r: ParsedRow, record_id: Optional[str] = None) -> dict:
        d = {k: v for k, v in r.__dict__.items()}
        d["inspection_date"] = r.inspection_date.isoformat() if r.inspection_date else None
        d["record_id"] = record_id
        return d

    def _known_locations(self) -> KnownLocations:
        """Trusted locations only (Phase 10.2) -- see trusted_location_names() -- plus the administrator-managed
        synonyms of those locations (Phase 10.3)."""
        return KnownLocations(trusted_location_names(self._get_store().records), self.settings.unlabelled_placeholder,
                              managed=self.synonyms.mapping())

    # ------------------------------------------------------------ Phase 10.3: managed synonyms (admin API)
    def _synonym_call(self, fn, *args):
        try:
            return fn(*args)
        except SynonymFailure as exc:
            raise ImportFailure(exc.status, exc.code, exc.message, exc.guidance) from None

    def list_synonyms(self) -> dict:
        return self.synonyms.listing(self._known_locations())

    def create_synonym(self, synonym, canonical, actor: dict) -> dict:
        return self._synonym_call(self.synonyms.create, synonym, canonical, self._known_locations(), actor)

    def update_synonym(self, synonym_id: str, synonym, canonical, actor: dict) -> dict:
        return self._synonym_call(self.synonyms.update, synonym_id, synonym, canonical, self._known_locations(), actor)

    def delete_synonym(self, synonym_id: str) -> dict:
        return self._synonym_call(self.synonyms.delete, synonym_id)

    def _parse(self, data: bytes, filename: str) -> ParsedWorkbook:
        wb = parse_workbook(data, filename)
        self._classify(wb)
        # Phase 9: standardise locations. Never fails the import: AI problems fall back to rules + review.
        wb.location_summary = self.location_intel.annotate(wb.rows, self._known_locations(), cache_key=wb.sha256)
        return wb

    # ------------------------------------------------------------ preview
    def preview(self, data: bytes, filename: str) -> dict:
        """Validate and classify in memory. Writes nothing."""
        try:
            wb = self._parse(data, filename)
        except WorkbookRejected as exc:
            raise ImportFailure(400 if exc.code != "duplicate_workbook" else 409, exc.code, exc.message, exc.guidance) from None
        summary = self._summary(wb)
        return {"filename": wb.filename, "fingerprint": wb.sha256, "size_bytes": wb.size_bytes,
                "source_format": wb.source_format, "location_intelligence": wb.location_summary, "summary": summary, "file_warnings": wb.warnings,
                "can_import": summary["ready"] > 0, "rows": [self._row_dict(r) for r in wb.rows]}

    # ------------------------------------------------------------ commit
    def _attempt(self, filename: str, code: str, message: str, summary: Optional[dict] = None) -> None:
        try:
            folder = self.dir / ATTEMPTS
            folder.mkdir(parents=True, exist_ok=True)
            now = _now()
            att = {"attempt_id": f"ATT-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}", "created_at": now.isoformat(),
                   "filename": filename, "status": "failed", "code": code, "message": message, "summary": summary}
            write_file(folder / f"{att['attempt_id']}.json", json.dumps(att, indent=1).encode("utf-8"))
        except OSError:
            pass  # history of failures is best effort; it never affects data

    def _unreadable_imports(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        readable = {m["import_id"] for m, _ in iter_committed(self.dir)}
        return [p for p in self.dir.iterdir() if p.is_dir() and IMPORT_ID_RE.match(p.name) and p.name not in readable]

    def _disk_lock(self) -> ImportLock:
        try:
            return ImportLock(self.dir).acquire()
        except LockBusy:
            raise ImportFailure(409, "import_busy", "Another import operation is in progress.",
                                "Wait a moment and try again.") from None
        except OSError:
            raise ImportFailure(500, "import_failed", "The import folder is not writable. Nothing was changed.",
                                "Check folder permissions and free disk space, then try again.") from None

    def commit(self, data: bytes, filename: str, fingerprint: str, committed_by: Optional[dict] = None) -> dict:
        with self._lock:
            disk_lock = self._disk_lock()  # also excludes other backend processes
            try:
                return self._commit_locked(data, filename, fingerprint, committed_by)
            finally:
                disk_lock.release()

    def _commit_locked(self, data: bytes, filename: str, fingerprint: str, committed_by: Optional[dict]) -> dict:
        if True:  # body kept at its original indentation
            name = filename or "upload.xlsx"
            if hashlib.sha256(data).hexdigest() != (fingerprint or "").lower():
                raise ImportFailure(409, "preview_mismatch", "The uploaded file differs from the file that was previewed.",
                                    "Preview the workbook again, then confirm.")
            try:
                wb = self._parse(data, name)
            except WorkbookRejected as exc:
                self._attempt(name, exc.code, exc.message)
                raise ImportFailure(400 if exc.code != "duplicate_workbook" else 409, exc.code, exc.message, exc.guidance) from None
            summary = self._summary(wb)
            if summary["ready"] == 0:
                self._attempt(wb.filename, "nothing_to_import", "No new valid records.", summary)
                raise ImportFailure(422, "nothing_to_import", "There are no new valid records to import.",
                                    "Every record is a duplicate, blank or invalid. Fix the invalid rows and preview again.")
            now = _now()
            import_id = f"IMP-{now:%Y%m%d}-{uuid.uuid4().hex[:10]}"
            final = self.dir / import_id
            staging = self.dir / f"{STAGING_PREFIX}{import_id}"
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                for old in staging_dirs(self.dir):  # incomplete work from an interrupted import
                    shutil.rmtree(old, ignore_errors=True)
                (staging / IMAGES).mkdir(parents=True)
                write_file(staging / SOURCE, data)
                rows = []
                for r in wb.rows:
                    record_id = None
                    if r.status == "ready":
                        r.status = "imported"
                        record_id = f"{import_id}-R{r.row_number:04d}"
                        target = staging / IMAGES / f"{r.sha256}.{r.image_format}"
                        if not target.exists():
                            write_file(target, wb.photos[r.sha256])
                    rows.append(self._row_dict(r, record_id))
                summary["imported"] = summary.pop("ready")
                manifest = {"schema_version": SCHEMA_VERSION, "import_id": import_id, "created_at": now.isoformat(),
                            "filename": wb.filename, "source_sha256": wb.sha256, "size_bytes": wb.size_bytes,
                            "status": "completed", "summary": summary, "file_warnings": wb.warnings, "rows": rows,
                            "source_format": wb.source_format, "location_intelligence": wb.location_summary,
                            "committed_by": committed_by}
                write_file(staging / MANIFEST, json.dumps(manifest, indent=1).encode("utf-8"))
                os.rename(staging, final)  # the single publishing step
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                self._attempt(wb.filename, "import_failed", "The import could not be written; nothing was imported.", summary)
                raise ImportFailure(500, "import_failed", "The import could not be completed. Nothing was imported.",
                                    "Check free disk space and folder permissions, then try again.") from None
            try:
                self._reload()
            except Exception:
                raise ImportFailure(500, "reload_failed", f"Import {import_id} was saved but the dashboard could not reload.",
                                    "Restart the dashboard backend; the imported records will then appear.") from None
            return self._public(manifest, include_rows=True)

    # ------------------------------------------------------------ history
    @staticmethod
    def _public(m: dict, include_rows: bool = False, state: Optional[tuple] = None) -> dict:
        keys = ("import_id", "created_at", "filename", "source_sha256", "size_bytes", "status", "summary", "file_warnings",
                "source_format", "location_intelligence",
                "committed_by")
        out = {k: m.get(k) for k in keys}
        if state is not None:
            out["status"] = "withdrawn" if state[0] == WITHDRAWN else m.get("status")
            out["state_events"] = state[1]
        if include_rows:
            out["rows"] = m.get("rows", [])
        return out

    def history(self) -> dict:
        problems: list[str] = []
        items = []
        for m, folder in iter_committed(self.dir, problems):
            state = read_state(folder)
            if state[2]:
                problems.append(state[2])
            items.append(self._public(m, state=state))
        problems += [f"import '{p.name}' could not be read and was ignored" for p in self._unreadable_imports()
                     if f"import '{p.name}' could not be read and was ignored" not in problems]
        attempts = []
        folder = self.dir / ATTEMPTS
        if folder.is_dir():
            for p in folder.glob("ATT-*.json"):
                try:
                    attempts.append(json.loads(p.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    problems.append(f"attempt log '{p.name}' could not be read")
        items.sort(key=lambda m: m["created_at"], reverse=True)
        attempts.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        return {"imports": items, "failed_attempts": attempts,
                "interrupted_imports": len(staging_dirs(self.dir)), "problems": problems}

    def get(self, import_id: str) -> dict:
        m, folder = self._find(import_id)
        out = self._public(m, include_rows=True, state=read_state(folder))
        decisions, problem = read_decisions(folder)
        out["location_decisions"] = decisions
        out["location_problems"] = [problem] if problem else []
        for row in out["rows"]:
            if row.get("status") == "imported":
                loc, method, review = effective_location(row, decisions.get(str(row.get("row_number"))))
                row["effective_location"] = {"location": loc, "method": method, "review_status": review}
        return out

    # ------------------------------------------------------------ Phase 10.1: read-only location review queue
    def review_queue(self) -> dict:
        """Pending location decisions across active committed imports, grouped by original Excel text.

        Strictly read-only: no lock, no write, no AI. Uses the same effective review status as the import detail
        (manifest location_intel + locations.json decisions). Withdrawn imports are excluded; an import whose
        decisions file cannot be read is excluded and reported (its pending state cannot be determined)."""
        problems: list[str] = []
        groups: dict[str, dict] = {}
        scanned = withdrawn = skipped = 0
        known = self._known_locations()          # Phase 10.4: current trusted locations + active managed synonyms
        for m, folder in iter_committed(self.dir, problems):
            state = read_state(folder)
            if state[2]:
                problems.append(state[2])
            if state[0] == WITHDRAWN:
                withdrawn += 1
                continue
            decisions, problem = read_decisions(folder)
            if problem:
                problems.append(f"{problem}; import '{m['import_id']}' is not shown in the review queue")
                skipped += 1
                continue
            scanned += 1
            for row in m.get("rows", []):
                if row.get("status") != "imported":
                    continue
                if effective_location(row, decisions.get(str(row.get("row_number"))))[2] != "pending":
                    continue
                info = row.get("location_intel") or {}
                text = " ".join(str(row.get("official_location") or "").split())
                g = groups.setdefault(review_group_key(text), {"variants": {}, "occurrences": []})
                g["variants"][text] = g["variants"].get(text, 0) + 1
                g["occurrences"].append(_queue_occurrence(m, row, info))
        out = []
        for gk, g in groups.items():
            occ = sorted(g["occurrences"], key=lambda o: (o["import_id"], o["row_number"]))
            variants = sorted(g["variants"].items(), key=lambda kv: (-kv[1], kv[0]))
            out.append({
                "group_key": gk,
                "display_text": variants[0][0] or None,                   # most frequent original spelling
                "variants": [{"text": t or None, "count": n} for t, n in variants],
                "pending_count": len(occ),
                "import_count": len({o["import_id"] for o in occ}),
                "import_ids": sorted({o["import_id"] for o in occ}),
                "rule_methods": _tally(o["rule_method"] for o in occ),
                "rule_candidates": _tally(o["rule_candidate"] for o in occ),
                "candidates": sorted({c for o in occ for c in o["candidates"]}),
                "ai_suggestions": _tally(o["ai_suggestion"] for o in occ),
                "review_reasons": _tally(o["review_reason"] for o in occ),
                "current_match": _current_match(variants[0][0], occ, known),
                "occurrences": occ,
            })
        out.sort(key=lambda g: (-g["pending_count"], g["group_key"]))
        return {"groups": out, "group_count": len(out), "total_pending": sum(g["pending_count"] for g in out),
                "imports_scanned": scanned, "withdrawn_excluded": withdrawn, "unreadable_excluded": skipped,
                "problems": problems, "read_only": True}

    # ------------------------------------------------------------ Phase 10.5: whole-group decisions
    def _group_plan(self, group_key: str, action: str, location: Optional[str], known: KnownLocations) -> dict:
        """Which rows of one review group a group action would decide, and why the others are excluded. Read-only.
        Eligibility is always computed here from the files on disk -- never taken from the client."""
        if action not in GROUP_ACTIONS:
            raise ImportFailure(400, "invalid_action", "Unknown group decision.", "Use accept, manual, keep_original or reject.")
        gk = review_group_key(group_key)
        if not gk or len(gk) > 200:
            raise ImportFailure(400, "invalid_group", "Choose a review group.")
        target = via = entered = None
        new_location = False
        if action == "manual":
            entered = " ".join("".join(ch for ch in (location or "") if ch.isprintable()).split())
            if not 2 <= len(entered) <= 120:
                raise ImportFailure(400, "invalid_location", "Enter a location of 2-120 characters.", "")
            existing, via = known.resolve_trusted(entered)                  # Phase 10.4 resolver
            target, new_location, via = existing or entered, existing is None, via or "new_location"
            if _numbers(key(gk)) != _numbers(key(target)):
                raise ImportFailure(422, "number_mismatch", f"The numbers in '{target}' do not match this group's text.",
                                    "A group decision must keep the location numbers; decide such rows one at a time.")
        elif action == "accept" and location is not None:
            target, via = known.resolve_trusted(location)
            if target is None:
                raise ImportFailure(409, "untrusted_suggestion", f"'{location}' is not a trusted location.",
                                    "Only a current, trusted suggestion can be accepted for a group.")
        current = None
        eligible, excluded, suggestions = [], [], {}
        for m, folder in iter_committed(self.dir):
            rows = [r for r in m.get("rows", []) if r.get("status") == "imported"
                    and review_group_key(r.get("official_location")) == gk]
            if not rows:
                continue
            withdrawn = read_state(folder)[0] == WITHDRAWN
            decisions, problem = read_decisions(folder)
            for r in rows:
                ref = {"import_id": m["import_id"], "row_number": r.get("row_number")}
                if withdrawn or problem:
                    excluded.append({**ref, "reason": "import_withdrawn" if withdrawn else "decisions_unreadable"})
                    continue
                if effective_location(r, decisions.get(str(ref["row_number"])))[2] != "pending":
                    excluded.append({**ref, "reason": "not_pending"})
                    continue
                info = r.get("location_intel") or {}
                if action == "reject" and not _stored_options(info):
                    excluded.append({**ref, "reason": "no_suggestion"})
                    continue
                if action == "accept":
                    if current is None:
                        current = match(r.get("official_location"), known)  # one group -> one rule key -> one match
                    found = _current_suggestions(info, current, known)
                    if not found:
                        excluded.append({**ref, "reason": "no_trusted_suggestion"})
                        continue
                    if len(found) > 1:
                        excluded.append({**ref, "reason": "conflicting_suggestions"})
                        continue
                    only = next(iter(found))
                    suggestions[only] = suggestions.get(only, 0) + 1
                    if target is None:                                       # first look: only list the suggestions
                        continue
                    if only != target:
                        excluded.append({**ref, "reason": "different_suggestion"})
                        continue
                eligible.append({**ref, "_row": r, "_folder": folder, "_info": info})
        if not eligible and not excluded and not suggestions:
            raise ImportFailure(404, "unknown_group", "No imported record belongs to that review group.",
                                "Reload the review queue.")
        if action == "accept" and target is not None and current is not None and current.normalized_location == target \
                and current.method == "managed_synonym":
            via = "managed_synonym"
        return {"group_key": gk, "action": action, "location": target, "entered_location": entered,
                "resolved_via": via, "new_location": new_location,
                "suggestions": [{"location": k, "count": n} for k, n in sorted(suggestions.items(), key=lambda kv: (-kv[1], kv[0]))],
                "eligible_count": len(eligible), "_eligible": eligible,
                "eligible_rows": [{"import_id": e["import_id"], "row_number": e["row_number"]} for e in eligible],
                "excluded": sorted(excluded, key=lambda x: (x["import_id"], x["row_number"])),
                "excluded_reasons": [{"reason": t["value"], "text": EXCLUSION_TEXT[t["value"]], "count": t["count"]}
                                     for t in _tally(x["reason"] for x in excluded)]}

    @staticmethod
    def _public_plan(plan: dict) -> dict:
        return {k: v for k, v in plan.items() if not k.startswith("_")}

    def preview_group_decision(self, group_key: str, action: str, location: Optional[str] = None) -> dict:
        """Dry run for the confirmation step: nothing is locked or written, nothing is audited."""
        return {**self._public_plan(self._group_plan(group_key, action, location, self._known_locations())), "dry_run": True}

    def decide_group(self, group_key: str, action: str, location: Optional[str], expected_count: int,
                     actor: Optional[dict] = None) -> dict:
        """Apply one decision to every eligible pending row of a review group -- all or nothing. Same lock, same
        locations.json format and same reload rollback as decide_location(); every row gets its own decision + event."""
        if action == "accept" and not location:
            raise ImportFailure(400, "choose_suggestion", "Name the suggestion to accept.", "Preview the group first.")
        with self._lock:
            disk_lock = self._disk_lock()
            try:
                plan = self._group_plan(group_key, action, location, self._known_locations())   # re-checked under the lock
                if plan["eligible_count"] == 0:
                    raise ImportFailure(409, "nothing_to_decide", "No record of this group can take that decision.",
                                        "See the excluded records and their reasons.")
                if plan["eligible_count"] != expected_count:
                    raise ImportFailure(409, "group_changed",
                                        f"The group changed since it was previewed ({plan['eligible_count']} eligible "
                                        f"record(s) now, {expected_count} confirmed). Nothing was changed.",
                                        "Review the group again and confirm the new count.")
                group_id = f"GRP-{uuid.uuid4().hex[:12]}"
                at = _now().isoformat(timespec="seconds")
                meta = {"id": group_id, "key": plan["group_key"], "size": plan["eligible_count"]}
                by_folder: dict[Path, list[dict]] = {}
                for e in plan["_eligible"]:
                    by_folder.setdefault(e["_folder"], []).append(e)
                writes = []
                for folder, items in sorted(by_folder.items()):
                    decisions, _ = read_decisions(folder)
                    path = folder / LOCATIONS
                    previous = path.read_bytes() if path.exists() else None
                    history = []
                    if previous:
                        try:
                            history = json.loads(previous).get("events", [])
                        except ValueError:
                            history = []
                    events = []
                    for e in items:
                        d = _group_row_decision(action, plan, e, actor, at, meta)
                        decisions = {**decisions, str(e["row_number"]): d}
                        events.append({**d, "row_number": e["row_number"]})
                    new = json.dumps({"import_id": items[0]["import_id"], "decisions": decisions, "events": history + events},
                                     indent=1).encode("utf-8")
                    writes.append((folder, previous, new))
                written: list[tuple[Path, Optional[bytes]]] = []
                try:
                    for folder, previous, new in writes:
                        write_atomic(folder, LOCATIONS, new)
                        written.append((folder, previous))
                    self._reload()
                except Exception as exc:
                    rollback_ok = True
                    for folder, previous in reversed(written):           # undo every import already written
                        try:
                            write_atomic(folder, LOCATIONS, previous)
                        except Exception:
                            rollback_ok = False
                    try:
                        self._reload()
                    except Exception:
                        rollback_ok = False
                    code = "rollback_failed" if not rollback_ok else \
                        "decision_write_failed" if isinstance(exc, OSError) else "decision_failed"
                    raise ImportFailure(500, code,
                                        "The group decision could not be completed, so every change was undone." if rollback_ok
                                        else "The group decision failed and the previous decisions could not be fully restored.",
                                        "Nothing was changed; try again." if rollback_ok else
                                        "Restart the backend and run the import integrity check.") from None
                out = self._public_plan(plan)
                out.update({"applied": True, "rolled_back": False, "group_decision_id": group_id,
                            "decided_rows": [{"import_id": e["import_id"], "row_number": e["row_number"],
                                              "original_location": e["_row"].get("official_location")}
                                             for e in plan["_eligible"]]})
                return out
            finally:
                disk_lock.release()

    def _find(self, import_id: str) -> tuple[dict, Path]:
        if not IMPORT_ID_RE.match(import_id or ""):
            raise ImportFailure(400, "invalid_import_id", "Invalid import identifier.")
        for m, folder in iter_committed(self.dir):
            if m["import_id"] == import_id:
                return m, folder
        raise ImportFailure(404, "unknown_import", "Import not found.")

    # ------------------------------------------------------------ Phase 8.4: integrity, withdrawal, reinstatement
    def verify(self, import_id: str) -> dict:
        """Re-hash the stored workbook and every imported photograph against the manifest (read-only)."""
        m, folder = self._find(import_id)
        problems: list[str] = []
        src = folder / SOURCE
        if not src.is_file():
            problems.append("the stored source workbook is missing")
        elif hashlib.sha256(src.read_bytes()).hexdigest() != m.get("source_sha256"):
            problems.append("the stored source workbook does not match its SHA-256")
        expected: dict[str, str] = {}
        for row in m.get("rows", []):
            if row.get("status") == "imported":
                if not row.get("sha256") or not row.get("record_id"):
                    problems.append(f"row {row.get('row_number')} is marked imported but has no photograph hash or record id")
                    continue
                expected[f"{row['sha256']}.{row.get('image_format') or 'jpg'}"] = row["sha256"]
        checked = 0
        for name, sha in sorted(expected.items()):
            path = folder / IMAGES / name
            if not path.is_file():
                problems.append(f"photograph {name} is missing")
                continue
            checked += 1
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                problems.append(f"photograph {name} does not match its SHA-256")
        img_dir = folder / IMAGES
        if img_dir.is_dir():
            problems += [f"unexpected file in images/: {p.name}" for p in img_dir.iterdir() if p.name not in expected]
        imported = sum(1 for r in m.get("rows", []) if r.get("status") == "imported")
        if (m.get("summary") or {}).get("imported") != imported:
            problems.append("the manifest summary does not match its rows")
        state, _, state_problem = read_state(folder)
        if state_problem:
            problems.append(state_problem)
        if read_decisions(folder)[1]:
            problems.append(read_decisions(folder)[1])
        return {"import_id": import_id, "valid": not problems, "state": state, "photographs_checked": checked,
                "records": imported, "problems": problems}

    def _identities(self, m: dict) -> set[tuple]:
        return {identity(r.get("original_excel_filename"), r.get("worksheet_name"), r.get("report_date"),
                         r.get("serial_number"), r.get("sha256"))
                for r in m.get("rows", []) if r.get("status") == "imported"}

    def _change_state(self, import_id: str, target: str, *, reason: str, confirm: str, actor: Optional[dict],
                      affected_analyses: Optional[list[str]] = None) -> dict:
        reason = " ".join((reason or "").split())
        if (confirm or "").strip() != import_id:
            raise ImportFailure(400, "confirmation_mismatch", "The confirmation does not match the import identifier.",
                                "Type the import identifier exactly to confirm.")
        if not 5 <= len(reason) <= 500:
            raise ImportFailure(400, "reason_required", "A reason of 5-500 characters is required.",
                                "Explain why this import is being " + ("withdrawn." if target == WITHDRAWN else "reinstated."))
        with self._lock:
            disk_lock = self._disk_lock()
            try:
                m, folder = self._find(import_id)
                state, events, problem = read_state(folder)
                if problem:
                    raise ImportFailure(409, "state_unreadable", problem + ".", "Restore status.json from a backup first.")
                if state == target:
                    raise ImportFailure(409, "already_" + target, f"Import {import_id} is already {target}.", "")
                if target == ACTIVE:
                    current = {identity(r.original_excel_filename, r.worksheet_name, r.report_date,
                                        r.register_serial_number if r.register_serial_number is not None else r.serial_number,
                                        r.extracted_photograph.sha256 if r.extracted_photograph else None): r.record_id
                               for r in self._get_store().records}
                    clashes = sorted(current[k] for k in self._identities(m) if k in current)
                    if clashes:
                        raise ImportFailure(409, "reinstate_conflict",
                                            f"{len(clashes)} record(s) of this import now exist elsewhere (e.g. {clashes[0]}).",
                                            "Withdraw the overlapping import first, so no record appears twice.")
                status_path = folder / STATUS
                previous = status_path.read_bytes() if status_path.exists() else None
                event = {"action": "withdrawn" if target == WITHDRAWN else "reinstated", "at": _now().isoformat(timespec="seconds"),
                         "by": actor, "reason": reason}
                if affected_analyses is not None:
                    event["affected_analyses"] = affected_analyses
                new = json.dumps({"import_id": import_id, "state": target, "events": events + [event]}, indent=1).encode("utf-8")
                try:
                    write_state_bytes(folder, new)
                except OSError:
                    raise ImportFailure(500, "state_write_failed", "The change could not be saved. Nothing was changed.",
                                        "Check folder permissions and free disk space.") from None
                try:
                    self._reload()
                except Exception:
                    # Roll back: restore the previous status exactly, then reload the previous dataset.
                    rollback_ok = True
                    try:
                        write_state_bytes(folder, previous)
                        self._reload()
                    except Exception:
                        rollback_ok = False
                    raise ImportFailure(500, "state_change_failed" if rollback_ok else "rollback_failed",
                                        "The dataset could not be reloaded, so the change was undone." if rollback_ok else
                                        "The dataset could not be reloaded and the previous state could not be fully restored.",
                                        "Nothing was changed; try again." if rollback_ok else
                                        "Restart the backend and run the import integrity check.") from None
                return {**self._public(m, state=read_state(folder)), "event": event}
            finally:
                disk_lock.release()

    # ------------------------------------------------------------ Phase 9: location review decisions
    def decide_location(self, import_id: str, row_number: int, action: str, location: Optional[str] = None,
                        actor: Optional[dict] = None, confirm_new_location: bool = False) -> dict:
        """Record an administrator's decision for one imported row's location. Stored in locations.json in the
        import's own folder (manifest unchanged), under the import lock, with reload rollback on failure.
        A 'manual' location must resolve to a trusted location (or active synonym); text that does not is refused
        unless ``confirm_new_location`` is True -- a new location is never created implicitly."""
        if action not in ("accept", "reject", "keep_original", "manual"):
            raise ImportFailure(400, "invalid_action", "Unknown location decision.", "Use accept, reject, keep_original or manual.")
        with self._lock:
            disk_lock = self._disk_lock()
            try:
                m, folder = self._find(import_id)
                if read_state(folder)[0] == WITHDRAWN:
                    raise ImportFailure(409, "import_withdrawn", f"Import {import_id} is withdrawn.",
                                        "Reinstate the import before reviewing its locations.")
                row = next((r for r in m.get("rows", []) if r.get("row_number") == row_number and r.get("status") == "imported"), None)
                if row is None:
                    raise ImportFailure(404, "unknown_row", f"Row {row_number} is not an imported record of {import_id}.", "")
                decisions, problem = read_decisions(folder)
                if problem:
                    raise ImportFailure(409, "decisions_unreadable", problem + ".", "Restore locations.json from a backup first.")
                info = row.get("location_intel") or {}
                rule_candidate = info.get("normalized_location") \
                    if info.get("normalized_location") != info.get("original_location") else None
                options = [x for x in dict.fromkeys((info.get("ai_suggestion"), rule_candidate)) if x]
                suggestion = options[0] if options else None
                known = self._known_locations()
                decision = {"action": action, "at": _now().isoformat(timespec="seconds"), "by": actor,
                            "original_location": row.get("official_location")}
                if action in ("accept", "reject"):
                    if not suggestion:
                        raise ImportFailure(409, "no_suggestion", "There is no suggestion to accept or reject for this row.",
                                            "Keep the original location or enter one manually.")
                    # Phase 9.4: when the AI and the rules disagree, the reviewer must say which one is accepted --
                    # the AI suggestion never silently wins over the rule candidate.
                    if action == "accept" and location is not None:
                        if location not in options:
                            raise ImportFailure(400, "not_a_suggestion", "Only a suggested location can be accepted.",
                                                "Use 'Enter location' for any other location.")
                        suggestion = location
                    elif action == "accept" and len(options) > 1:
                        raise ImportFailure(409, "choose_suggestion",
                                            "The AI suggestion and the rule candidate disagree; choose which one to accept.",
                                            "Accept one of them, keep the original or enter a location.")
                    decision["suggestion"] = suggestion
                    if len(options) > 1:
                        decision["disagreement"] = {"rule_candidate": rule_candidate, "ai_suggestion": info.get("ai_suggestion")}
                    if action == "accept":
                        resolved, via = known.resolve_trusted(suggestion)          # Phase 10.4: synonym-aware
                        decision["location"] = resolved or suggestion
                        decision["resolved_via"] = via or "as_suggested"
                elif action == "manual":
                    text = " ".join("".join(ch for ch in (location or "") if ch.isprintable()).split())
                    if not 2 <= len(text) <= 120:
                        raise ImportFailure(400, "invalid_location", "Enter a location of 2-120 characters.", "")
                    existing, via = known.resolve_trusted(text)                   # Phase 10.4: synonym-aware
                    if existing is None and not confirm_new_location:
                        raise ImportFailure(422, "untrusted_location", f"'{text}' is not a trusted location.",
                                            "Check the spelling or choose an existing location. To add it as a new "
                                            "location, confirm that explicitly (or use the group decision preview).")
                    decision["location"] = existing or text
                    decision["new_location"] = existing is None
                    decision["entered_location"] = text                            # what the administrator typed
                    decision["resolved_via"] = via or "new_location"
                path = folder / LOCATIONS
                previous = path.read_bytes() if path.exists() else None
                history = []
                if previous:
                    try:
                        history = json.loads(previous).get("events", [])
                    except ValueError:
                        history = []
                decisions = {**decisions, str(row_number): decision}
                new = json.dumps({"import_id": import_id, "decisions": decisions,
                                  "events": history + [{**decision, "row_number": row_number}]}, indent=1).encode("utf-8")
                try:
                    write_atomic(folder, LOCATIONS, new)
                except OSError:
                    raise ImportFailure(500, "decision_write_failed", "The decision could not be saved. Nothing was changed.",
                                        "Check folder permissions and free disk space.") from None
                try:
                    self._reload()
                except Exception:
                    rollback_ok = True
                    try:
                        write_atomic(folder, LOCATIONS, previous)
                        self._reload()
                    except Exception:
                        rollback_ok = False
                    raise ImportFailure(500, "decision_failed" if rollback_ok else "rollback_failed",
                                        "The dataset could not be reloaded, so the decision was undone." if rollback_ok else
                                        "The dataset could not be reloaded and the previous decisions could not be fully restored.",
                                        "Nothing was changed; try again." if rollback_ok else
                                        "Restart the backend and run the import integrity check.") from None
                loc, method, review = effective_location(row, decision)
                return {"import_id": import_id, "row_number": row_number, "decision": decision,
                        "effective_location": {"location": loc, "method": method, "review_status": review}}
            finally:
                disk_lock.release()

    def withdraw(self, import_id: str, *, reason: str, confirm: str, actor: Optional[dict] = None,
                 affected_analyses: Optional[list[str]] = None) -> dict:
        """Reversible rollback: the import's records leave the dataset; its files are kept unchanged."""
        return self._change_state(import_id, WITHDRAWN, reason=reason, confirm=confirm, actor=actor,
                                  affected_analyses=affected_analyses)

    def reinstate(self, import_id: str, *, reason: str, confirm: str, actor: Optional[dict] = None) -> dict:
        return self._change_state(import_id, ACTIVE, reason=reason, confirm=confirm, actor=actor)


# ============================================================================ Phase 10.1: review queue helpers
def review_group_key(text: Optional[str]) -> str:
    """Conservative grouping key: case and whitespace only. Numbers, punctuation, abbreviations and word order are
    kept, so "Cargo Hold 1" and "Cargo Hold 2" (or "Stbd" and "Starboard") are never merged."""
    return " ".join(str(text or "").split()).casefold()


def _tally(values) -> list[dict]:
    counts: dict[str, int] = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    return [{"value": v, "count": n} for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _current_match(text: str, occurrences: list[dict], known: KnownLocations) -> Optional[dict]:
    """Phase 10.4 (read-only): what the rules would find for this text NOW, against the current trusted locations and
    active managed synonyms -- only when it names a location and differs from what was stored at import time for at
    least one pending row. Nothing is applied: every row still needs an explicit decision."""
    rr = match(text, known)
    if not rr.normalized_location:
        return None
    if all((o["rule_method"], o["rule_candidate"]) == (rr.method, rr.normalized_location) for o in occurrences):
        return None
    return {"location": rr.normalized_location, "method": rr.method, "band": rr.band,
            "would_auto_apply": rr.method in AUTO_METHODS and rr.confidence >= HIGH, "explanation": rr.explanation}


def _queue_occurrence(m: dict, row: dict, info: dict) -> dict:
    """One pending row: where it comes from plus the review fields already stored in the manifest. AI confidence is
    only ever exposed as a capped band (low/medium), never as a raw number."""
    rule_candidate = info.get("rule_candidate")
    if rule_candidate is None and info.get("rule_method") in ("partial", "spelling"):      # manifests before Phase 9.4
        rule_candidate = info.get("normalized_location")
    ai = info.get("ai_suggestion")
    conf = info.get("ai_confidence")
    ai_band = info.get("ai_confidence_band") or (confidence_band(min(float(conf), AI_CONFIDENCE_CAP))
                                                 if ai and isinstance(conf, (int, float)) else None)
    return {
        "import_id": m["import_id"], "import_filename": m.get("filename"), "import_created_at": m.get("created_at"),
        "row_number": row.get("row_number"), "record_id": row.get("record_id"),
        "original_location": " ".join(str(row.get("official_location") or "").split()) or None,
        "worksheet_name": row.get("worksheet_name"), "report_date": row.get("report_date"),
        "inspection_date": row.get("inspection_date"), "image_filename": row.get("image_filename"),
        "source_cell": row.get("source_cell"),
        "rule_method": info.get("rule_method"), "rule_candidate": rule_candidate,
        "candidates": list(info.get("candidates") or []),
        "ai_suggestion": ai, "ai_confidence_band": ai_band if ai else None,
        "ai_agreement": info.get("ai_agreement"),
        "ai_reason": info.get("ai_reason") if ai or info.get("ai_alternatives") else None,
        "ai_provider": info.get("ai_provider"), "ai_alternatives": list(info.get("ai_alternatives") or []),
        "ai_ambiguous": bool(info.get("ai_ambiguous")), "ai_low_confidence": bool(info.get("ai_low_confidence")),
        "review_reason": info.get("review_reason"),
        "review_status": "pending",
    }


# ============================================================================ Phase 10.2: trusted location sources
# Review states of an Excel-imported record whose location a human approved. Every other imported record is untrusted
# for automatic matching: "pending" / "rejected" / "kept_original" / no review (pre-Phase 9) still carry raw Excel text,
# and "auto_applied" / "not_required" values were themselves matched to an existing trusted name, so they can add no
# legitimate new name (excluding them also stops a wrongly applied name from becoming trusted again).
TRUSTED_IMPORT_REVIEWS = frozenset({"accepted", "manual"})


def trusted_location_names(records):
    """Location names allowed to drive automatic matching: the historical dataset (registers / JSON reports) plus
    imported records whose location was approved by a reviewer. Withdrawn imports are not loaded at all."""
    for r in records:
        if r.source != "excel_import" or r.location_review in TRUSTED_IMPORT_REVIEWS:
            yield r.official_location


# ============================================================================ Phase 10.5: whole-group decisions
GROUP_ACTIONS = ("accept", "manual", "keep_original", "reject")
EXCLUSION_TEXT = {
    "import_withdrawn": "The import is withdrawn.",
    "decisions_unreadable": "The import's location review file cannot be read.",
    "not_pending": "Already decided or matched automatically.",
    "no_suggestion": "There is no suggestion to reject.",
    "no_trusted_suggestion": "No current suggestion is a trusted location.",
    "conflicting_suggestions": "Different trusted locations are suggested; decide it on its own.",
    "different_suggestion": "Its current suggestion is a different location.",
}


def _stored_options(info: dict) -> list[str]:
    """The suggestions decide_location() offers for one row (AI suggestion, rule candidate)."""
    rule_candidate = info.get("normalized_location") if info.get("normalized_location") != info.get("original_location") else None
    return [x for x in dict.fromkeys((info.get("ai_suggestion"), rule_candidate)) if x]


def _current_suggestions(info: dict, current, known: KnownLocations) -> set:
    """Trusted locations suggested for a row TODAY: the current rule match plus every stored suggestion that still
    resolves to a trusted location / active synonym. Stale (no longer trusted) suggestions are dropped, never applied."""
    found = {current.normalized_location} if current is not None and current.normalized_location else set()
    for s in _stored_options(info):
        resolved, _ = known.resolve_trusted(s)
        if resolved:
            found.add(resolved)
    return found


def _group_row_decision(action: str, plan: dict, e: dict, actor, at: str, meta: dict) -> dict:
    """One row's decision in exactly the shape decide_location() writes, plus the group action it came from."""
    d = {"action": action, "at": at, "by": actor, "original_location": e["_row"].get("official_location"), "group": meta}
    if action == "accept":
        d.update({"suggestion": plan["location"], "location": plan["location"], "resolved_via": plan["resolved_via"]})
    elif action == "reject":
        d["suggestion"] = _stored_options(e["_info"])[0]
    elif action == "manual":
        d.update({"location": plan["location"], "new_location": plan["new_location"],
                  "entered_location": plan["entered_location"], "resolved_via": plan["resolved_via"]})
    return d
