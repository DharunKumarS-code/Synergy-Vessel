"""Location / date / vessel queries over the in-memory store.

Official Location is the primary key for location matching. AI Suggested
Location is only consulted when a caller explicitly asks for it, and it never
replaces the Official Location value on a record.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Iterable, Literal, Optional

from ..models import DateEntry, InspectionRecord, Location, Vessel
from .inspection_store import InspectionStore, normalise

MatchMode = Literal["exact", "contains"]


def matched_fields(
    r: InspectionRecord,
    want: str,
    *,
    match: MatchMode = "exact",
    include_ai_location: bool = False,
    placeholder: Optional[str] = None,
) -> list[str]:
    """Names of the location fields of ``r`` that match the normalised query ``want``.

    A placeholder Official Location (e.g. 'Not Labelled') is not a real location, so it
    only matches when searched for exactly, never as a partial ('contains') hit.
    """
    fields = [("official_location", r.official_location)]
    if include_ai_location:
        fields.append(("ai_suggested_location", r.ai_suggested_location))
    out = []
    for name, value in fields:
        v = normalise(value)
        if not v:
            continue
        if match == "exact":
            hit = v == want
        else:
            hit = want in v and not (placeholder and name == "official_location" and v == placeholder and v != want)
        if hit:
            out.append(name)
    return out


def filter_records(
    records: Iterable[InspectionRecord],
    *,
    vessel_id: Optional[str] = None,
    location: Optional[str] = None,
    match: MatchMode = "exact",
    include_ai_location: bool = False,
    date: Optional[dt.date] = None,
    week: Optional[str] = None,
    placeholder: Optional[str] = None,
) -> list[InspectionRecord]:
    want = normalise(location) if location is not None else None
    want_week = normalise(week) if week is not None else None
    ph = normalise(placeholder) if placeholder else None
    out = []
    for r in records:
        if vessel_id is not None and r.vessel_id != vessel_id:
            continue
        if date is not None and r.inspection_date != date:
            continue
        if want_week is not None and want_week not in (normalise(r.week), normalise(r.week_folder)):
            continue
        if want is not None and not matched_fields(
            r, want, match=match, include_ai_location=include_ai_location, placeholder=ph
        ):
            continue
        out.append(r)
    return out


def list_vessels(store: InspectionStore) -> list[Vessel]:
    groups: dict[str, list[InspectionRecord]] = defaultdict(list)
    for r in store.records:
        groups[r.vessel_id].append(r)
    out = []
    for vid, recs in sorted(groups.items()):
        dates = [r.inspection_date for r in recs if r.inspection_date]
        weeks = sorted({(r.week_number or 0, r.week) for r in recs})
        out.append(Vessel(
            vessel_id=vid,
            vessel_name=recs[0].vessel_name,
            name_source="original_excel_filename (text before 'Deck Photos')",
            record_count=len(recs),
            weeks=[w for _, w in weeks],
            first_inspection_date=min(dates) if dates else None,
            last_inspection_date=max(dates) if dates else None,
        ))
    return out


def list_locations(store: InspectionStore, vessel_id: Optional[str] = None,
                   include_placeholder: bool = True) -> list[Location]:
    placeholder = normalise(store.settings.unlabelled_placeholder)
    groups: dict[str, list[InspectionRecord]] = defaultdict(list)
    for r in store.records:
        if vessel_id is not None and r.vessel_id != vessel_id:
            continue
        key = normalise(r.official_location)
        if not key:
            continue
        groups[key].append(r)
    out = []
    for key, recs in groups.items():
        is_ph = key == placeholder
        if is_ph and not include_placeholder:
            continue
        variants = Counter(r.official_location for r in recs)
        out.append(Location(
            location=variants.most_common(1)[0][0],
            variants=sorted(variants),
            is_placeholder=is_ph,
            record_count=len(recs),
            date_count=len({r.inspection_date for r in recs if r.inspection_date}),
            vessel_ids=sorted({r.vessel_id for r in recs}),
        ))
    out.sort(key=lambda l: (l.is_placeholder, l.location.casefold()))
    return out


def location_exists(store: InspectionStore, location: str, include_ai_location: bool = False) -> bool:
    return bool(filter_records(store.records, location=location, include_ai_location=include_ai_location))


def dates_for(records: list[InspectionRecord]) -> tuple[list[DateEntry], int]:
    by_date: dict[dt.date, list[InspectionRecord]] = defaultdict(list)
    undated = 0
    for r in records:
        if r.inspection_date is None:
            undated += 1
        else:
            by_date[r.inspection_date].append(r)
    entries = [
        DateEntry(
            date=d,
            weeks=sorted({r.week for r in recs}),
            record_count=len(recs),
            resolved_image_count=sum(r.image_status == "resolved" for r in recs),
        )
        for d, recs in sorted(by_date.items())
    ]
    return entries, undated
