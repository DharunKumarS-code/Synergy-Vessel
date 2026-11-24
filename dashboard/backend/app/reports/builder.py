"""Collects report data from existing records and saved analyses (read-only)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from ..ai.records import AnalysisRecord, ReviewStatus
from ..ai.schema import Classification
from ..ai.service import AnalysisError
from ..models import InspectionRecord
from ..services.inspection_store import InspectionStore, normalise
from ..services.location_service import filter_records

MAX_LOCATION_LEN = 200


class ReportError(AnalysisError):
    """User-safe report failure (same JSON shape as AI errors: {detail, code})."""


@dataclass(frozen=True)
class ReportFilters:
    vessel: Optional[str] = None
    week: Optional[str] = None
    date_from: Optional[dt.date] = None
    date_to: Optional[dt.date] = None
    location: Optional[str] = None
    classification: Optional[Classification] = None

    def describe(self) -> list[tuple[str, str]]:
        out = []
        if self.week:
            out.append(("Week", self.week))
        if self.date_from or self.date_to:
            out.append(("Date range", f"{self.date_from or 'start'} to {self.date_to or 'end'}"))
        if self.location:
            out.append(("Location contains", self.location))
        if self.classification:
            out.append(("AI classification", self.classification.value))
        return out


def assessment_status(a: AnalysisRecord) -> tuple[str, str]:
    """(short label, sentence) distinguishing simulated / AI-generated / human-approved results."""
    if a.simulated:
        return "SIMULATED", "Simulated result from the development mock provider. It is not an assessment of these photographs."
    status = a.review.status
    if status is ReviewStatus.APPROVED:
        return "HUMAN-APPROVED", "AI-generated assessment approved by a human reviewer."
    if status is ReviewStatus.REJECTED:
        return "REJECTED", "AI-generated assessment rejected by a human reviewer. Not a finding."
    if status is ReviewStatus.NEEDS_REASSESSMENT:
        return "NEEDS REASSESSMENT", "AI-generated assessment flagged by a reviewer for reassessment. Not verified."
    return "UNVERIFIED", "AI-generated assessment awaiting human review. Not verified."


@dataclass
class ReportData:
    filters: ReportFilters
    vessel_name: str
    generated_at: dt.datetime
    records: list[InspectionRecord]
    analyses: list[AnalysisRecord]
    period: tuple[Optional[dt.date], Optional[dt.date]]
    summary: dict = field(default_factory=dict)


def validate_filters(store: InspectionStore, f: ReportFilters) -> ReportFilters:
    if f.vessel is not None and not any(r.vessel_id == f.vessel for r in store.records):
        raise ReportError(404, "unknown_vessel", "The selected vessel was not found.")
    if f.week is not None:
        w = normalise(f.week)
        if not w or not any(w in (normalise(r.week), normalise(r.week_folder)) for r in store.records):
            raise ReportError(400, "unknown_week", "The selected inspection week was not found.")
    if f.date_from and f.date_to and f.date_from > f.date_to:
        raise ReportError(400, "invalid_date_range", "The start date must be on or before the end date.")
    if f.location is not None:
        if not f.location.strip():
            return ReportFilters(**{**f.__dict__, "location": None})
        if len(f.location) > MAX_LOCATION_LEN:
            raise ReportError(400, "invalid_location", "The location filter is too long.")
    return f


def _in_range(d: Optional[dt.date], f: ReportFilters) -> bool:
    if f.date_from is None and f.date_to is None:
        return True
    if d is None:
        return False
    return (f.date_from is None or d >= f.date_from) and (f.date_to is None or d <= f.date_to)


def build_report(store: InspectionStore, analyses: list[AnalysisRecord], f: ReportFilters) -> ReportData:
    f = validate_filters(store, f)
    recs = filter_records(store.records, vessel_id=f.vessel, week=f.week, location=f.location,
                          match="contains", include_ai_location=True, placeholder=store.settings.unlabelled_placeholder)
    recs = [r for r in recs if _in_range(r.inspection_date, f)]
    # one row per inspection record, chronological then by image reference
    seen: set[str] = set()
    rows = []
    for r in sorted(recs, key=lambda r: (r.inspection_date or dt.date.max, r.week_number or 0, r.image_id or "~", r.record_id)):
        if r.record_id not in seen:
            seen.add(r.record_id)
            rows.append(r)

    week_norm = normalise(f.week) if f.week else None
    picked = []
    for a in analyses:
        if f.vessel and a.vessel_id != f.vessel:
            continue
        if f.classification and a.classification is not f.classification:
            continue
        if not (_in_range(a.earlier.inspection_date, f) and _in_range(a.later.inspection_date, f)):
            continue
        if f.location and not a.matches(location=f.location):
            continue
        if week_norm and week_norm not in (normalise(a.earlier.week), normalise(a.later.week)):
            continue
        picked.append(a)
    picked.sort(key=lambda a: (a.later.inspection_date, a.created_at), reverse=True)

    dated = [r.inspection_date for r in rows if r.inspection_date]
    period = (f.date_from or (min(dated) if dated else None), f.date_to or (max(dated) if dated else None))
    vessels = {r.vessel_id: r.vessel_name for r in (rows or store.records) if not f.vessel or r.vessel_id == f.vessel}
    vessel_name = next(iter(vessels.values())) if len(vessels) == 1 else "All vessels"

    unique = {(r.extracted_photograph.sha256 if r.extracted_photograph and r.extracted_photograph.sha256 else r.record_id)
              for r in rows if r.image_status == "resolved"}
    locations: dict[str, int] = {}
    for r in rows:
        if r.official_location_labelled and r.official_location:
            key = " ".join(r.official_location.split())
            locations[key] = locations.get(key, 0) + 1
    summary = {
        "appearances": len(rows),
        "unique_photographs": len(unique),
        "inspection_dates": len(set(dated)),
        "weeks": sorted({r.week for r in rows}, key=lambda w: next((x.week_number or 0 for x in rows if x.week == w), 0)),
        "locations": dict(sorted(locations.items(), key=lambda kv: (-kv[1], kv[0]))),
        "not_labelled": sum(1 for r in rows if not r.official_location_labelled),
        "missing_photographs": sum(1 for r in rows if r.image_status != "resolved"),
        "undated": sum(1 for r in rows if not r.inspection_date),
        "analyses": len(picked),
        "simulated_analyses": sum(1 for a in picked if a.simulated),
        "approved_analyses": sum(1 for a in picked if not a.simulated and a.review.status is ReviewStatus.APPROVED),
    }
    return ReportData(filters=f, vessel_name=vessel_name, generated_at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0),
                      records=rows, analyses=picked, period=period, summary=summary)
