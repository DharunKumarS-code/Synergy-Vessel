"""SV-08 -- physical viewpoint mapping across inspection weeks.

A *viewpoint* is one physical inspection view (for example "Top View Main Deck, view 2"). Each historical record
(one photograph appearance) can be a *member* of at most one active viewpoint. The mapping is additional metadata in
its own file, ``<viewpoint_dir>/viewpoints.json``; historical workbooks, reports, registers and photographs are never
changed, and every member keeps its original record ID and Official Location text.

Evidence and states
-------------------
* ``method`` -- how a member was proposed:
    ``same_serial_within_layout``  same workbook serial and Official Location inside one stable workbook layout era
    ``identical_file``              byte-identical file (SHA-256) to the previous member: a repeated photograph,
                                    never a new inspection photograph
    ``proposed_cross_layout``       position-based continuation across a workbook layout change (proposal only)
    ``reviewed``                    placed by an administrator's explicit decision
* ``status`` -- ``proposed`` (automatic, no red flags), ``needs_review`` (automatic but flagged), ``approved``
  (administrator decision), ``rejected`` (administrator decision; kept for history, not shown for comparison).

Only *approved* members are ever compared by the AI (see ``ViewpointService.pair_status``); the generator never
approves anything. Every administrator decision is appended to the file's ``history`` and to the audit log.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..models import InspectionRecord
from .inspection_store import normalise

SCHEMA_VERSION = 1
GENERATOR_VERSION = "sv08-1"
MAPPING_FILE = "viewpoints.json"
REPORT_FILE = "viewpoints_report.md"

METHODS = ("same_serial_within_layout", "identical_file", "proposed_cross_layout", "reviewed")
STATUSES = ("proposed", "needs_review", "approved", "rejected")
ACTIVE = ("proposed", "needs_review", "approved")
# Two consecutive weeks belong to the same layout era when at least this share of workbook serials keeps its
# Official Location (Weeks 36 -> 37: 95 of 96; Weeks 37 -> 38: almost none).
ERA_SIMILARITY = 0.8
VIEWPOINT_ID_RE = re.compile(r"^VP-[A-Z0-9][A-Z0-9-]{0,60}$")
RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
MAX_NOTES = 500
MAX_NAME = 120

FLAG_TEXT = {
    "cross_layout": "Crosses a workbook layout change: the serial numbers were renumbered, so the position-based "
                    "proposal must be confirmed by looking at the photographs.",
    "location_count_changed": "The number of photographs under this Official Location changed from the previous week.",
    "label_inconsistency": "The same workbook serial carries a different Official Location in another week of the "
                           "same layout.",
    "identical_file_different_label": "The same photograph file also appears under a different Official Location.",
    "multiple_in_week": "More than one photograph of this viewpoint in the same week.",
    "no_serial": "The record has no workbook serial number, so it cannot be matched by position.",
    "inventory_changed": "The photograph file no longer matches the fingerprint recorded in the mapping.",
    "joined": "Added by an administrator joining two viewpoints; confirm it belongs here.",
    "split": "Moved by an administrator splitting a viewpoint; confirm it belongs here.",
}


class ViewpointError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _serial(r: InspectionRecord) -> Optional[int]:
    return r.serial_number if r.serial_number is not None else r.register_serial_number


def _sha(r: InspectionRecord) -> Optional[str]:
    return r.extracted_photograph.sha256 if r.extracted_photograph and r.extracted_photograph.sha256 else None


def _slug(text: str) -> str:
    words = re.findall(r"[A-Z0-9]+", (text or "").upper())
    return "-".join(words)[:28].strip("-") or "LOCATION"


def _title(text: str) -> str:
    t = " ".join((text or "").split())
    return t[:1].upper() + t[1:].lower() if t.isupper() else t


# =========================================================================== generator (offline, deterministic)
@dataclass
class _Segment:
    era: int
    key: tuple
    location: str            # original Official Location text (latest member)
    hint: Optional[str]      # AI suggestion, used only to keep Not Labelled photographs apart when proposing
    members: list[InspectionRecord] = field(default_factory=list)

    def week_set(self) -> set[int]:
        return {r.week_number for r in self.members}


def detect_eras(records: list[InspectionRecord]) -> tuple[list[list[int]], dict]:
    """Group the weeks into workbook layout eras: consecutive weeks stay in one era while most serials keep their
    Official Location. Returns (eras, similarity per transition)."""
    weeks = sorted({r.week_number for r in records if r.week_number is not None})
    layout: dict[int, dict[int, str]] = {w: {} for w in weeks}
    for r in records:
        s = _serial(r)
        if r.week_number is not None and r.source == "json_report" and s is not None:
            layout[r.week_number][s] = normalise(r.official_location)
    eras: list[list[int]] = []
    sims: dict[str, float] = {}
    for w in weeks:
        if eras:
            prev = eras[-1][-1]
            a, b = layout[prev], layout[w]
            same = sum(1 for s in set(a) & set(b) if a[s] == b[s])
            sim = same / max(len(a), len(b), 1)
            sims[f"{prev}->{w}"] = round(sim, 3)
            if sim >= ERA_SIMILARITY:
                eras[-1].append(w)
                continue
        eras.append([w])
    return eras, sims


def generate(records: Iterable[InspectionRecord], placeholder: str, *, created_at: Optional[str] = None) -> dict:
    """Propose a viewpoint mapping for every record. Nothing is approved; flagged members are needs_review."""
    recs = sorted([r for r in records], key=lambda r: (r.week_number or 0, _serial(r) or 0, r.record_id))
    dated = [r for r in recs if r.week_number is not None]
    ph = normalise(placeholder)
    eras, sims = detect_eras(dated)
    era_of = {w: i for i, ws in enumerate(eras) for w in ws}

    # ---- segments: (era, serial, Official Location) -- a serial is only meaningful inside one layout era
    segments: dict[tuple, _Segment] = {}
    for r in dated:
        loc = normalise(r.official_location)
        s = _serial(r)
        key = (era_of[r.week_number], s, loc) if s is not None else (era_of[r.week_number], "rec", r.record_id)
        seg = segments.get(key)
        if seg is None:
            seg = segments[key] = _Segment(era_of[r.week_number], key, r.official_location or "", None)
        seg.members.append(r)
        seg.location = r.official_location or seg.location
        if loc == ph and r.ai_suggested_location:
            seg.hint = r.ai_suggested_location

    # ---- flags and findings
    flags: dict[str, set[str]] = defaultdict(set)
    findings: list[dict] = []
    by_week_loc: dict[tuple[int, str], list[InspectionRecord]] = defaultdict(list)
    for r in dated:
        by_week_loc[(r.week_number, normalise(r.official_location))].append(r)
    for r in recs:
        if r.week_number is None:
            findings.append({"type": "undated_week", "record_ids": [r.record_id],
                             "message": "The record has no week number and is left unassigned."})
        elif _serial(r) is None:
            flags[r.record_id].add("no_serial")
    # label inconsistency: one serial, several Official Locations inside one era
    for i, ws in enumerate(eras):
        per_serial: dict[int, set[str]] = defaultdict(set)
        for r in dated:
            if era_of[r.week_number] == i and r.source == "json_report" and _serial(r) is not None:
                per_serial[_serial(r)].add(normalise(r.official_location))
        for s, locs in sorted(per_serial.items()):
            if len(locs) > 1:
                ids = [r.record_id for r in dated if era_of[r.week_number] == i and r.source == "json_report"
                       and _serial(r) == s]
                for rid in ids:
                    flags[rid].add("label_inconsistency")
                labels = sorted({(r.week_number, r.official_location) for r in dated if r.record_id in set(ids)})
                findings.append({"type": "label_inconsistency", "record_ids": ids,
                                 "message": f"Serial #{s} changes Official Location inside layout era {i + 1} "
                                            f"(weeks {ws[0]}-{ws[-1]}): " + "; ".join(
                                     f"W{w} {loc}" for w, loc in labels[:1] + labels[-1:])})
    # location count changed between consecutive weeks of one era
    for ws in eras:
        for a, b in zip(ws, ws[1:]):
            for loc in sorted({k[1] for k in by_week_loc if k[0] in (a, b)}):
                na, nb = len(by_week_loc.get((a, loc), [])), len(by_week_loc.get((b, loc), [])),
                if na != nb:
                    for r in by_week_loc.get((b, loc), []):
                        flags[r.record_id].add("location_count_changed")
                    findings.append({"type": "location_count_changed",
                                     "record_ids": [r.record_id for r in by_week_loc.get((b, loc), [])],
                                     "message": f"'{loc}' has {na} photograph(s) in week {a} and {nb} in week {b}."})
    # identical file under different Official Locations
    locs_of_sha: dict[str, set[str]] = defaultdict(set)
    for r in dated:
        if _sha(r):
            locs_of_sha[_sha(r)].add(normalise(r.official_location))
    multi = {h for h, locs in locs_of_sha.items() if len(locs) > 1}
    for h in sorted(multi):
        ids = [r.record_id for r in dated if _sha(r) == h]
        for rid in ids:
            flags[rid].add("identical_file_different_label")
        findings.append({"type": "identical_file_different_label", "record_ids": ids, "sha256": h,
                         "message": "The same photograph file appears under different Official Locations: "
                                    + ", ".join(sorted({f"{r.record_id} ({r.official_location})" for r in dated
                                                        if _sha(r) == h}))})

    # ---- chains: within an era a segment is one chain; across eras, chains continue by position (proposal)
    def subkey(seg: _Segment) -> tuple:
        loc = normalise(seg.location)
        return (loc, normalise(seg.hint)) if loc == ph else (loc, "")

    chains: list[list[_Segment]] = []
    open_chains: dict[int, list[list[_Segment]]] = {}
    for i, ws in enumerate(eras):
        era_segs = sorted([s for s in segments.values() if s.era == i],
                          key=lambda s: (min(s.week_set()), _serial(s.members[0]) or 0, s.members[0].record_id))
        joined: set[int] = set()
        if i > 0:
            last, first = eras[i - 1][-1], ws[0]
            ends = defaultdict(list)     # previous era: chains whose last segment has a member in the last week
            for ch in open_chains.get(i - 1, []):
                seg = ch[-1]
                if last in seg.week_set() and seg.key[1] != "rec":
                    ends[subkey(seg)].append(ch)
            starts = defaultdict(list)
            for seg in era_segs:
                if first in seg.week_set() and seg.key[1] != "rec":
                    starts[subkey(seg)].append(seg)
            for sk in sorted(set(ends) | set(starts)):
                e = sorted(ends.get(sk, []), key=lambda ch: min(_serial(r) or 0 for r in ch[-1].members if r.week_number == last))
                s = sorted(starts.get(sk, []), key=lambda seg: min(_serial(r) or 0 for r in seg.members if r.week_number == first))
                if e and s and len(e) == len(s):
                    for ch, seg in zip(e, s):
                        ch.append(seg)
                        joined.add(id(seg))
                elif e or s:
                    findings.append({"type": "cross_layout_unmatched",
                                     "record_ids": [r.record_id for seg in s for r in seg.members if r.week_number == first]
                                                   + [r.record_id for ch in e for r in ch[-1].members if r.week_number == last],
                                     "message": f"'{sk[0]}'{' (' + sk[1] + ')' if sk[1] else ''}: {len(e)} view(s) in week "
                                                f"{last} but {len(s)} in week {first}; not continued automatically."})
        open_chains[i] = []
        for ch in open_chains.get(i - 1, []):
            if any(id(seg) in joined for seg in ch) and ch[-1].era == i:
                open_chains[i].append(ch)
        for seg in era_segs:
            if id(seg) not in joined:
                ch = [seg]
                chains.append(ch)
                open_chains[i].append(ch)

    # ---- viewpoints
    used_ids: Counter = Counter()
    viewpoints = []
    chains.sort(key=lambda ch: (normalise(ch[-1].location), min(ch[0].week_set()), _serial(ch[0].members[0]) or 0))
    era_label = {i: f"layout era {i + 1} (weeks {ws[0]}-{ws[-1]})" for i, ws in enumerate(eras)}
    for ch in chains:
        loc = ch[-1].location or placeholder
        slug = _slug(loc)
        used_ids[slug] += 1
        n = used_ids[slug]
        vid = f"VP-{slug}-{n:02d}"
        hint = next((s.hint for s in reversed(ch) if s.hint), None)
        name = f"{_title(loc)} - view {n}" + (f" (AI hint: {hint})" if normalise(loc) == ph and hint else "")
        members, prev = [], None
        for si, seg in enumerate(ch):
            for k, r in enumerate(sorted(seg.members, key=lambda r: (r.week_number, _serial(r) or 0, r.record_id))):
                f = set(flags.get(r.record_id, ()))
                ev = [f"{era_label[seg.era]}, serial #{_serial(r)}, Official Location '{r.official_location}'"
                      if _serial(r) is not None else "record without a workbook serial"]
                if prev is not None and _sha(prev) and _sha(prev) == _sha(r):
                    method = "identical_file"
                    ev.append(f"byte-identical file to {prev.record_id} (week {prev.week_number}): repeated photograph")
                elif si > 0 and k == 0:
                    method = "proposed_cross_layout"
                    f.add("cross_layout")
                    p_last = [x for x in ch[si - 1].members if x.week_number == eras[ch[si - 1].era][-1]]
                    ev.append(f"position-based continuation of {p_last[0].record_id if p_last else 'the previous layout'} "
                              f"across the workbook layout change ({era_label[ch[si - 1].era]} -> {era_label[seg.era]})")
                else:
                    method = "same_serial_within_layout"
                if prev is not None and prev.week_number == r.week_number:
                    f.add("multiple_in_week")
                members.append(_member(r, method, ev, sorted(f)))
                prev = r
        viewpoints.append({
            "viewpoint_id": vid, "display_name": name[:MAX_NAME], "official_location": loc,
            "created_at": created_at or _now(), "created_by": "generator", "members": members,
        })

    weeks = sorted({r.week_number for r in dated})
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 1,
        "generated": {
            "at": created_at or _now(), "generator_version": GENERATOR_VERSION,
            "inventory": {"records": len(recs), "unique_files": len({_sha(r) for r in recs if _sha(r)}), "weeks": weeks},
            "eras": [{"era": i + 1, "weeks": ws} for i, ws in enumerate(eras)],
            "era_similarity": sims,
            "rules": [
                "Serial numbers are matching evidence only inside one workbook layout era.",
                "Byte-identical files are repeated photographs (method identical_file), never new inspections.",
                "Matches across a layout change are proposals (needs_review) until an administrator confirms them.",
                "Not Labelled photographs stay Not Labelled; the AI suggestion is only used to keep them apart.",
                "Nothing is approved automatically; only approved members are compared by the AI.",
            ],
        },
        "location_groups": [_deck_group(dated, ph)],
        "viewpoints": viewpoints,
        "findings": findings,
        "history": [],
    }


def _member(r: InspectionRecord, method: str, evidence: list[str], flags: list[str]) -> dict:
    return {
        "record_id": r.record_id, "week": r.week_number, "serial": _serial(r), "source": r.source,
        "official_location": r.official_location, "sha256": _sha(r), "method": method, "evidence": evidence,
        "flags": flags, "status": "needs_review" if flags else "proposed",
        "reviewed_by": None, "reviewed_at": None, "notes": None,
    }


# Display-only "Main Deck Areas" navigation group: an explicit, reviewed allowlist of Official Location names
# (decided by the project owner, 2026-11-25). Compared after normalise() (case and spacing only); nothing else is
# matched, so Bridge Deck, Funnel Deck, Boat Deck, Rescue Boat / Garbage Storage Deck, Forecastle Deck Area, Poop Deck,
# Monkey Island, Gangway Entrance and the cargo holds are never included. Not Labelled is never included.
MAIN_DECK_GROUP_ID = "main-deck-areas"
MAIN_DECK_AREAS: tuple[str, ...] = (
    "PORT AND STARBOARD MAIN DECK FACING FORWARD VIEW",
    "TOP VIEW MAIN DECK",
    "Cross deck Fwd of # 1",
    "Cross deck Between # 1&2",
    "Cross deck Between # 2&3",
    "Cross deck Between # 3&4",
    "Cross deck Between # 4&5",
    "Cross deck between # 5&6",
    "Cross deck between # 6&7",
)


def _main_deck_allowed(location: Optional[str]) -> bool:
    return bool(location) and normalise(location) in {normalise(x) for x in MAIN_DECK_AREAS}


def _deck_group(records: list[InspectionRecord], ph: str) -> dict:
    """Display-only navigation group. It never merges locations or viewpoints and never creates a location."""
    locs = sorted({r.official_location for r in records
                   if r.official_location and normalise(r.official_location) != ph
                   and _main_deck_allowed(r.official_location)}, key=str.casefold)
    return {"group_id": MAIN_DECK_GROUP_ID, "display_name": "Main Deck Areas", "locations": locs,
            "basis": "Reviewed list of Main Deck Official Locations (display-only grouping; "
                     "each location and viewpoint stays separate)."}


# =========================================================================== validation
@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_mapping(data: Any, by_id: Optional[dict] = None) -> Validation:
    """Structural checks, plus checks against the inventory when ``by_id`` (record_id -> record) is given."""
    v = Validation()
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        v.errors.append(f"schema_version must be {SCHEMA_VERSION}")
        return v
    if not isinstance(data.get("revision"), int) or data["revision"] < 1:
        v.errors.append("revision must be a positive integer")
    vps = data.get("viewpoints")
    if not isinstance(vps, list):
        v.errors.append("viewpoints must be a list")
        return v
    seen_vp, active_of = set(), {}
    for vp in vps:
        if not isinstance(vp, dict) or not isinstance(vp.get("viewpoint_id"), str) or not VIEWPOINT_ID_RE.match(vp["viewpoint_id"]):
            v.errors.append(f"invalid viewpoint id: {str(vp.get('viewpoint_id') if isinstance(vp, dict) else vp)[:80]}")
            continue
        vid = vp["viewpoint_id"]
        if vid in seen_vp:
            v.errors.append(f"duplicate viewpoint id {vid}")
        seen_vp.add(vid)
        if not isinstance(vp.get("display_name"), str) or not vp["display_name"].strip():
            v.errors.append(f"{vid}: display_name is required")
        for m in vp.get("members") or []:
            rid = m.get("record_id") if isinstance(m, dict) else None
            if not isinstance(rid, str) or not RECORD_ID_RE.match(rid):
                v.errors.append(f"{vid}: invalid member record id")
                continue
            if m.get("status") not in STATUSES:
                v.errors.append(f"{vid}/{rid}: invalid status {m.get('status')!r}")
            if m.get("method") not in METHODS:
                v.errors.append(f"{vid}/{rid}: invalid method {m.get('method')!r}")
            for key in ("evidence", "flags"):
                if not isinstance(m.get(key, []), list):
                    v.errors.append(f"{vid}/{rid}: {key} must be a list")
            if m.get("status") in ACTIVE:
                if rid in active_of:
                    v.errors.append(f"record {rid} is an active member of both {active_of[rid]} and {vid}")
                active_of[rid] = vid
            if by_id is not None:
                rec = by_id.get(rid)
                if rec is None:
                    v.errors.append(f"{vid}: record {rid} is not in the inspection inventory")
                elif m.get("sha256") and _sha(rec) and m["sha256"] != _sha(rec):
                    v.warnings.append(f"{vid}/{rid}: photograph fingerprint changed since mapping")
                elif rec.week_number is not None and m.get("week") != rec.week_number:
                    v.errors.append(f"{vid}/{rid}: week {m.get('week')} does not match the record (week {rec.week_number})")
    text = json.dumps(data, ensure_ascii=False)
    if re.search(r"[A-Za-z]:\\\\|[A-Za-z]:/|\\\\\\\\", text):
        v.errors.append("the mapping must not contain absolute file-system paths")
    if not isinstance(data.get("history", []), list):
        v.errors.append("history must be a list")
    return v


# =========================================================================== store + service
class ViewpointStore:
    """The mapping file. Rewritten atomically (temp file + fsync + replace); every write bumps ``revision``."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / MAPPING_FILE
        self.lock = threading.RLock()

    def read(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, data: dict) -> None:
        from ..imports.store import write_atomic
        self.dir.mkdir(parents=True, exist_ok=True)
        write_atomic(self.dir, MAPPING_FILE, (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


def has_decisions(data: Optional[dict]) -> bool:
    if not data:
        return False
    if data.get("history"):
        return True
    return any(m.get("status") in ("approved", "rejected") or m.get("reviewed_by")
               for vp in data.get("viewpoints", []) for m in vp.get("members", []))


@dataclass
class Mapping:
    available: bool
    data: Optional[dict] = None
    problem: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    member_of: dict[str, tuple[dict, dict]] = field(default_factory=dict)   # record_id -> (viewpoint, member)

    @property
    def revision(self) -> Optional[int]:
        return self.data.get("revision") if self.data else None


class ViewpointService:
    def __init__(self, directory: Path, store_getter: Callable[[], Any]):
        self.files = ViewpointStore(directory)
        self._store = store_getter
        self._cache: tuple[Any, Any, Mapping] | None = None

    # ------------------------------------------------------------------ loading
    def mapping(self) -> Mapping:
        """The current mapping, validated against the loaded inventory (cached per file version and inventory)."""
        store = self._store()
        try:
            stat = self.files.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if self._cache and self._cache[0] == stamp and self._cache[1] is store:
            return self._cache[2]
        if stamp is None:
            m = Mapping(False, problem="No viewpoint mapping has been generated yet. An administrator can create it "
                                       "with: python -m app.cli generate-viewpoints")
        else:
            try:
                data = self.files.read()
                val = validate_mapping(data, store.by_id)
            except (OSError, ValueError) as exc:
                data, val = None, Validation(errors=[f"unreadable: {type(exc).__name__}"])
            if not val.ok:
                m = Mapping(False, problem="The viewpoint mapping file is invalid (" + "; ".join(val.errors[:3])
                                           + "). Restore it from a backup.", warnings=val.warnings)
            else:
                m = Mapping(True, data=data, warnings=val.warnings)
                for vp in data["viewpoints"]:
                    for mem in vp.get("members", []):
                        if mem.get("status") in ACTIVE:
                            m.member_of[mem["record_id"]] = (vp, mem)
        self._cache = (stamp, store, m)
        return m

    # ------------------------------------------------------------------ queries
    def _photo(self, rec: InspectionRecord, mem: dict, prev_sha: Optional[str]) -> dict:
        sha = _sha(rec)
        return {**rec.model_dump(mode="json"),
                "viewpoint_member": {"status": mem["status"], "method": mem["method"], "flags": mem.get("flags", []),
                                     "flag_messages": [FLAG_TEXT.get(f, f) for f in mem.get("flags", [])],
                                     "evidence": mem.get("evidence", []),
                                     "repeat_of_previous_week": bool(sha and prev_sha and sha == prev_sha)}}

    def _weeks(self, vessel_id: Optional[str]) -> list[dict]:
        out: dict[int, dict] = {}
        for r in self._store().records:
            if r.week_number is None or (vessel_id and r.vessel_id != vessel_id):
                continue
            e = out.setdefault(r.week_number, {"week": r.week_number, "week_label": r.week, "inspection_date": None})
            if r.inspection_date and not e["inspection_date"]:
                e["inspection_date"] = r.inspection_date.isoformat()
        return [out[w] for w in sorted(out)]

    def viewpoint_view(self, vp: dict, weeks: list[dict]) -> dict:
        store = self._store()
        per_week: dict[int, list[dict]] = defaultdict(list)
        counts = Counter()
        for mem in vp.get("members", []):
            counts[mem["status"]] += 1
            if mem["status"] in ACTIVE and mem["record_id"] in store.by_id:
                per_week[mem["week"]].append(mem)
        out_weeks, prev_sha = [], None
        for w in weeks:
            mems = sorted(per_week.get(w["week"], []), key=lambda m: (m.get("serial") or 0, m["record_id"]))
            photos = [self._photo(store.by_id[m["record_id"]], m, prev_sha) for m in mems]
            out_weeks.append({**w, "photos": photos, "available": bool(photos)})
            if mems:
                prev_sha = _sha(store.by_id[mems[0]["record_id"]])
        return {"viewpoint_id": vp["viewpoint_id"], "display_name": vp["display_name"],
                "official_location": vp.get("official_location"),
                "counts": {s: counts.get(s, 0) for s in STATUSES}, "weeks": out_weeks}

    def for_location(self, location: str, vessel_id: Optional[str] = None) -> dict:
        m = self.mapping()
        weeks = self._weeks(vessel_id)
        want = normalise(location)
        store = self._store()
        result = {"location": location, "mapping": {"available": m.available, "revision": m.revision, "problem": m.problem},
                  "weeks": weeks, "viewpoints": [], "unlinked": []}
        if not m.available:
            return result
        for vp in m.data["viewpoints"]:
            if normalise(vp.get("official_location")) != want:
                continue
            members = [x for x in vp.get("members", []) if x["record_id"] in store.by_id]
            if vessel_id and not any(store.by_id[x["record_id"]].vessel_id == vessel_id for x in members):
                continue
            view = self.viewpoint_view(vp, weeks)
            if any(w["photos"] for w in view["weeks"]):
                result["viewpoints"].append(view)
        result["unlinked"] = [r.model_dump(mode="json") for r in store.records
                              if normalise(r.official_location) == want and r.record_id not in m.member_of
                              and (not vessel_id or r.vessel_id == vessel_id)]
        return result

    def groups(self, vessel_id: Optional[str] = None) -> dict:
        m = self.mapping()
        if not m.available:
            return {"mapping": {"available": False, "problem": m.problem}, "groups": []}
        counts = Counter(normalise(vp.get("official_location")) for vp in m.data["viewpoints"])
        groups = []
        for g in m.data.get("location_groups", []):
            locs, basis = g.get("locations", []), g.get("basis")
            if g.get("group_id") == MAIN_DECK_GROUP_ID:
                # A mapping generated before the reviewed allowlist (broad "deck" word match) is corrected on read;
                # the mapping file itself is not rewritten.
                locs = [loc for loc in locs if _main_deck_allowed(loc)]
                basis = _deck_group([], "")["basis"]
            groups.append({"group_id": g["group_id"], "display_name": g["display_name"], "basis": basis,
                           "locations": [{"location": loc, "viewpoint_count": counts.get(normalise(loc), 0)}
                                         for loc in locs]})
        return {"mapping": {"available": True, "revision": m.revision}, "groups": groups}

    def pair_status(self, earlier_id: str, later_id: str) -> dict:
        """How two records relate through the mapping. ``approved`` is the only status that allows AI analysis."""
        m = self.mapping()
        if not m.available:
            return {"status": "mapping_unavailable", "viewpoint_id": None,
                    "message": m.problem or "The viewpoint mapping is not available."}
        a, b = m.member_of.get(earlier_id), m.member_of.get(later_id)
        if a is None or b is None:
            return {"status": "unmapped", "viewpoint_id": None,
                    "message": "At least one photograph is not linked to a viewpoint yet."}
        if a[0]["viewpoint_id"] != b[0]["viewpoint_id"]:
            return {"status": "different_viewpoints", "viewpoint_id": None,
                    "earlier_viewpoint_id": a[0]["viewpoint_id"], "later_viewpoint_id": b[0]["viewpoint_id"],
                    "message": "The photographs belong to different physical viewpoints."}
        vid = a[0]["viewpoint_id"]
        if a[1].get("sha256") and a[1].get("sha256") == b[1].get("sha256"):
            return {"status": "not_retaken", "viewpoint_id": vid,
                    "message": "Not retaken - the same photograph file was reused."}
        if a[1]["status"] == "approved" and b[1]["status"] == "approved":
            return {"status": "approved", "viewpoint_id": vid, "message": "Both photographs are approved for this viewpoint."}
        return {"status": "requires_review", "viewpoint_id": vid,
                "message": "Viewpoint mapping requires admin review before these photographs can be analysed."}

    def lookup(self, record_id: str) -> dict:
        m = self.mapping()
        store = self._store()
        rec = store.by_id.get(record_id)
        hit = m.member_of.get(record_id) if m.available else None
        return {"record_id": record_id, "known_record": rec is not None,
                "mapping": {"available": m.available, "revision": m.revision, "problem": m.problem},
                "viewpoint_id": hit[0]["viewpoint_id"] if hit else None,
                "official_location": hit[0].get("official_location") if hit else None,
                "record": rec.model_dump(mode="json") if rec else None}

    # ------------------------------------------------------------------ administration
    def review(self, location: Optional[str] = None, week: Optional[int] = None, status: Optional[str] = None) -> dict:
        m = self.mapping()
        if not m.available:
            raise ViewpointError(409, "mapping_unavailable", m.problem or "The viewpoint mapping is not available.")
        store = self._store()
        want = normalise(location) if location else None
        totals, matches, vps = Counter(), [], []
        for vp in m.data["viewpoints"]:
            for mem in vp.get("members", []):
                totals[mem["status"]] += 1
            locs = {normalise(vp.get("official_location"))} | {normalise(x.get("official_location")) for x in vp.get("members", [])}
            if want and want not in locs:
                continue
            active = sorted([x for x in vp.get("members", []) if x["status"] in ACTIVE],
                            key=lambda x: (x["week"], x.get("serial") or 0, x["record_id"]))
            members = [self._admin_member(x, store) for x in sorted(vp.get("members", []), key=lambda x: (x["week"], x.get("serial") or 0))]
            vp_view = {"viewpoint_id": vp["viewpoint_id"], "display_name": vp["display_name"],
                       "official_location": vp.get("official_location"), "members": members,
                       "counts": dict(Counter(x["status"] for x in vp.get("members", [])))}
            if week is None or any(x["week"] == week for x in vp.get("members", [])):
                if status is None or any(x["status"] == status for x in vp.get("members", [])):
                    vps.append(vp_view)
            for e, l in zip(active, active[1:]):
                combined = ("approved" if e["status"] == l["status"] == "approved"
                            else "needs_review" if "needs_review" in (e["status"], l["status"]) else "proposed")
                if week is not None and week not in (e["week"], l["week"]):
                    continue
                if status is not None and combined != status:
                    continue
                matches.append({"viewpoint_id": vp["viewpoint_id"], "display_name": vp["display_name"],
                                "official_location": vp.get("official_location"), "status": combined,
                                "repeated_photograph": bool(e.get("sha256") and e.get("sha256") == l.get("sha256")),
                                "earlier": self._admin_member(e, store), "later": self._admin_member(l, store)})
        unlinked = [r.record_id for r in store.records if r.record_id not in m.member_of]
        return {"revision": m.revision, "summary": {"viewpoints": len(m.data["viewpoints"]),
                                                    "members": {s: totals.get(s, 0) for s in STATUSES},
                                                    "unlinked_records": len(unlinked), "findings": len(m.data.get("findings", []))},
                "matches": matches, "viewpoints": vps, "unlinked": unlinked,
                "findings": m.data.get("findings", []), "generated": m.data.get("generated", {}),
                "warnings": m.warnings, "flag_text": FLAG_TEXT}

    @staticmethod
    def _admin_member(mem: dict, store) -> dict:
        rec = store.by_id.get(mem["record_id"])
        return {**{k: mem.get(k) for k in ("record_id", "week", "serial", "official_location", "method", "evidence",
                                           "flags", "status", "reviewed_at", "notes")},
                "sha256": mem.get("sha256"),
                "reviewed_by": (mem.get("reviewed_by") or {}).get("username") if mem.get("reviewed_by") else None,
                "inspection_date": rec.inspection_date.isoformat() if rec and rec.inspection_date else None,
                "image_id": rec.image_id if rec else None, "image_url": rec.image_url if rec else None,
                "ai_suggested_location": rec.ai_suggested_location if rec else None}

    def history(self, viewpoint_id: Optional[str] = None, limit: int = 100) -> dict:
        m = self.mapping()
        if not m.available:
            raise ViewpointError(409, "mapping_unavailable", m.problem or "The viewpoint mapping is not available.")
        items = [h for h in m.data.get("history", []) if viewpoint_id is None or viewpoint_id in h.get("viewpoint_ids", [])]
        return {"revision": m.revision, "items": list(reversed(items))[:limit]}

    def decide(self, action: str, body: dict, actor: dict) -> dict:
        """Apply one administrator decision. Optimistic concurrency through ``revision``; the whole file is
        rewritten atomically and the decision is appended to ``history`` with the previous and new states."""
        with self.files.lock:
            m = self.mapping()
            if not m.available:
                raise ViewpointError(409, "mapping_unavailable", m.problem or "The viewpoint mapping is not available.")
            if body.get("revision") != m.revision:
                raise ViewpointError(409, "mapping_changed", "The viewpoint mapping was changed by someone else. "
                                                             "Reload and check the latest state before deciding.")
            data = copy.deepcopy(m.data)
            store = self._store()
            vps = {vp["viewpoint_id"]: vp for vp in data["viewpoints"]}
            notes = (body.get("notes") or "").strip()[:MAX_NOTES] or None
            vid = body.get("viewpoint_id")
            vp = vps.get(vid)
            if vp is None:
                raise ViewpointError(404, "unknown_viewpoint", "The viewpoint was not found.")
            ids = [r for r in (body.get("record_ids") or []) if isinstance(r, str)]
            changes: list[dict] = []
            touched = [vid]
            who = {"username": actor.get("username"), "role": actor.get("role")}
            at = _now()

            def members(want_ids: list[str], vpx: dict) -> list[dict]:
                found = {x["record_id"]: x for x in vpx.get("members", [])}
                missing = [r for r in want_ids if r not in found]
                if not want_ids or missing:
                    raise ViewpointError(422, "unknown_member", "Choose photographs that belong to this viewpoint.")
                return [found[r] for r in want_ids]

            def need_reason(mems: list[dict]) -> None:
                if any(x["status"] == "approved" for x in mems) and not notes:
                    raise ViewpointError(422, "reason_required", "Changing an approved mapping needs a reason in the notes.")

            def mark(mem: dict, status: str, method: Optional[str] = None, flag: Optional[str] = None,
                     where: Optional[str] = None) -> None:
                changes.append({"record_id": mem["record_id"], "previous": {"status": mem["status"], "viewpoint_id": vid},
                                "new": {"status": status, "viewpoint_id": where or vid}})
                mem["status"] = status
                if method:
                    mem["method"] = method
                if flag and flag not in mem.setdefault("flags", []):
                    mem["flags"].append(flag)
                mem["reviewed_by"], mem["reviewed_at"], mem["notes"] = who, at, notes

            if action == "approve":
                mems = members(ids, vp)
                if any(x["status"] == "rejected" for x in mems):
                    raise ViewpointError(409, "rejected_member", "Reinstate a rejected photograph before approving it.")
                for x in mems:
                    if x["status"] != "approved":
                        mark(x, "approved")
            elif action == "approve_all_proposed":
                if any(x["status"] == "needs_review" for x in vp["members"]):
                    raise ViewpointError(409, "flagged_members", "Decide the flagged (needs review) photographs of this "
                                                                 "viewpoint individually first.")
                for x in vp["members"]:
                    if x["status"] == "proposed":
                        mark(x, "approved")
            elif action == "reject":
                mems = members(ids, vp)
                need_reason(mems)
                for x in mems:
                    if x["status"] != "rejected":
                        mark(x, "rejected")
            elif action == "reinstate":
                for x in members(ids, vp):
                    if x["status"] == "rejected":
                        if x["record_id"] in {r for v2 in data["viewpoints"] for r in
                                              (y["record_id"] for y in v2.get("members", []) if y["status"] in ACTIVE)}:
                            raise ViewpointError(409, "already_linked", "This photograph is now linked to another viewpoint.")
                        mark(x, "needs_review")
            elif action == "move":
                target = vps.get(body.get("target_viewpoint_id"))
                if target is None or target is vp:
                    raise ViewpointError(422, "invalid_target", "Choose a different, existing viewpoint.")
                if not notes:
                    raise ViewpointError(422, "reason_required", "Moving a photograph to another viewpoint needs a reason.")
                for x in members(ids, vp):
                    if x["status"] == "rejected":
                        raise ViewpointError(409, "rejected_member", "Reinstate a rejected photograph before moving it.")
                    new = {**copy.deepcopy(x), "method": "reviewed"}
                    new["evidence"] = list(x.get("evidence", [])) + [f"moved from {vid} by an administrator"]
                    mark(x, "rejected", where=target["viewpoint_id"])
                    x["notes"] = f"moved to {target['viewpoint_id']}: {notes}"
                    new.update({"status": "approved", "reviewed_by": who, "reviewed_at": at, "notes": notes})
                    target["members"].append(new)
                touched.append(target["viewpoint_id"])
            elif action == "split":
                at_week = body.get("at_week")
                if not isinstance(at_week, int):
                    raise ViewpointError(422, "invalid_week", "Choose the first week of the new viewpoint.")
                moving = [x for x in vp["members"] if x["week"] >= at_week and x["status"] in ACTIVE]
                staying = [x for x in vp["members"] if x["week"] < at_week and x["status"] in ACTIVE]
                if not moving or not staying:
                    raise ViewpointError(422, "invalid_week", "The split week must leave photographs on both sides.")
                need_reason(moving)
                new_id = self._new_id(vid, vps)
                new_vp = {"viewpoint_id": new_id, "display_name": (body.get("display_name") or f"{vp['display_name']} (from week {at_week})")[:MAX_NAME],
                          "official_location": vp.get("official_location"), "created_at": at, "created_by": who["username"],
                          "members": []}
                for x in moving:
                    status = "needs_review" if x["status"] == "approved" else x["status"]
                    new = copy.deepcopy(x)
                    mark(x, "rejected", where=new_id)
                    x["notes"] = f"split into {new_id}" + (f": {notes}" if notes else "")
                    new.update({"status": status, "flags": sorted(set(new.get("flags", [])) | ({"split"} if status == "needs_review" else set()))})
                    new_vp["members"].append(new)
                data["viewpoints"].append(new_vp)
                touched.append(new_id)
            elif action == "join":
                other = vps.get(body.get("target_viewpoint_id"))
                if other is None or other is vp:
                    raise ViewpointError(422, "invalid_target", "Choose a different, existing viewpoint to join.")
                if not notes:
                    raise ViewpointError(422, "reason_required", "Joining two viewpoints needs a reason.")
                for x in [y for y in other["members"] if y["status"] in ACTIVE]:
                    new = copy.deepcopy(x)
                    mark(x, "rejected", where=vid)
                    x["notes"] = f"joined into {vid}: {notes}"
                    new.update({"status": "needs_review", "flags": sorted(set(new.get("flags", [])) | {"joined"}),
                                "reviewed_by": None, "reviewed_at": None, "notes": None})
                    vp["members"].append(new)
                touched.append(other["viewpoint_id"])
            elif action == "rename":
                name = " ".join(str(body.get("display_name") or "").split())[:MAX_NAME]
                if not name:
                    raise ViewpointError(422, "invalid_name", "Enter a display name.")
                changes.append({"previous": {"display_name": vp["display_name"]}, "new": {"display_name": name}})
                vp["display_name"] = name
            else:
                raise ViewpointError(422, "unknown_action", "Unknown decision.")
            if not changes:
                raise ViewpointError(409, "nothing_changed", "Nothing to change: the photographs already have that state.")

            data["revision"] = m.revision + 1
            entry = {"seq": len(data.get("history", [])) + 1, "at": at, "by": who, "action": action,
                     "viewpoint_ids": touched, "record_ids": ids, "changes": changes, "notes": notes}
            data.setdefault("history", []).append(entry)
            val = validate_mapping(data, store.by_id)
            if not val.ok:
                raise ViewpointError(409, "invalid_result", "The decision would make the mapping inconsistent: "
                                     + "; ".join(val.errors[:2]))
            self.files.write(data)
            self._cache = None
            return {"revision": data["revision"], "history_entry": entry}

    @staticmethod
    def _new_id(vid: str, vps: dict) -> str:
        base = re.sub(r"-S\d+$", "", vid)
        n = 1
        while f"{base}-S{n}" in vps:
            n += 1
        return f"{base}-S{n}"


# =========================================================================== report
def summarise(data: dict) -> dict:
    statuses = Counter(m["status"] for vp in data["viewpoints"] for m in vp["members"])
    methods = Counter(m["method"] for vp in data["viewpoints"] for m in vp["members"] if m["status"] in ACTIVE)
    flags = Counter(f for vp in data["viewpoints"] for m in vp["members"] if m["status"] in ACTIVE for f in m.get("flags", []))
    linked = {m["record_id"] for vp in data["viewpoints"] for m in vp["members"] if m["status"] in ACTIVE}
    repeated = sum(1 for vp in data["viewpoints"] for m in vp["members"]
                   if m["status"] in ACTIVE and m["method"] == "identical_file")
    return {"viewpoints": len(data["viewpoints"]), "members": dict(statuses), "methods": dict(methods),
            "flags": dict(flags), "linked_records": len(linked), "repeated_photographs": repeated,
            "findings": dict(Counter(f["type"] for f in data.get("findings", [])))}


def render_report(data: dict, total_records: int) -> str:
    s = summarise(data)
    g = data.get("generated", {})
    lines = ["# Viewpoint mapping report", "",
             f"Generated {g.get('at')} by {g.get('generator_version')} (mapping revision {data.get('revision')}).", "",
             "## Inventory", "",
             f"* Records: {g.get('inventory', {}).get('records')} | unique photograph files: "
             f"{g.get('inventory', {}).get('unique_files')} | weeks: {', '.join(map(str, g.get('inventory', {}).get('weeks', [])))}",
             f"* Records linked to a viewpoint: {s['linked_records']} of {total_records}; unresolved: {total_records - s['linked_records']}",
             "", "## Workbook layout eras", ""]
    lines += [f"* Era {e['era']}: weeks {', '.join(map(str, e['weeks']))}" for e in g.get("eras", [])]
    lines += ["", "Serial-number similarity between consecutive weeks: "
              + ", ".join(f"{k} {v:.2f}" for k, v in g.get("era_similarity", {}).items()), "",
              "## Mapping states", "",
              f"* Viewpoints: {s['viewpoints']}",
              f"* Members by status: " + ", ".join(f"{k} {v}" for k, v in sorted(s["members"].items())),
              f"* Members by method: " + ", ".join(f"{k} {v}" for k, v in sorted(s["methods"].items())),
              f"* Repeated photographs (identical file to the previous week): {s['repeated_photographs']}",
              f"* Flags: " + (", ".join(f"{k} {v}" for k, v in sorted(s["flags"].items())) or "none"),
              "", "## Findings that need an administrator", ""]
    for f in data.get("findings", []):
        lines.append(f"* **{f['type']}** - {f['message']}")
    lines += ["", "## Viewpoints", "", "| Viewpoint | Name | Weeks | Proposed | Needs review | Approved |", "|---|---|---|---|---|---|"]
    for vp in data["viewpoints"]:
        c = Counter(m["status"] for m in vp["members"])
        weeks = sorted({m["week"] for m in vp["members"] if m["status"] in ACTIVE})
        lines.append(f"| {vp['viewpoint_id']} | {vp['display_name']} | {weeks[0] if weeks else '-'}-{weeks[-1] if weeks else '-'} "
                     f"({len(weeks)}) | {c.get('proposed', 0)} | {c.get('needs_review', 0)} | {c.get('approved', 0)} |")
    lines += ["", "Nothing in this mapping is approved automatically. The AI compares only photographs that an "
              "administrator approved as the same viewpoint.", ""]
    return "\n".join(lines)
