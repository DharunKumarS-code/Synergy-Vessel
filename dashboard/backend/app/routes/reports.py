"""Inspection report endpoints (read-only; generated on request, nothing is written to disk)."""

import re
from typing import Annotated, Optional

import datetime as dt
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from ..ai.schema import Classification
from ..reports.builder import ReportData, ReportError, ReportFilters, build_report
from ..reports.render import analyses_csv, inspection_pdf, inspections_csv
from ..services.image_service import ImageNotFound, InvalidImageId
from ..services.inspection_store import InspectionStore
from ..auth.deps import require_user
from .deps import get_cache, get_store

# SV-02: previews and downloads need a signed-in account (viewer, reviewer or administrator).
router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_user)])


def filters(
    vessel: Annotated[Optional[str], Query(max_length=80)] = None,
    week: Annotated[Optional[str], Query(max_length=40)] = None,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    location: Annotated[Optional[str], Query(max_length=200)] = None,
    classification: Optional[Classification] = None,
) -> ReportFilters:
    return ReportFilters(vessel=vessel, week=week, date_from=date_from, date_to=date_to,
                         location=location, classification=classification)


Filters = Annotated[ReportFilters, Depends(filters)]
Store = Annotated[InspectionStore, Depends(get_store)]


def _data(request: Request, store: InspectionStore, f: ReportFilters, *, require_content: bool) -> ReportData:
    svc = getattr(request.app.state, "analysis", None)
    analyses = svc.store.all() if svc is not None else []
    d = build_report(store, analyses, f)
    if require_content and not d.records and not d.analyses:
        raise ReportError(422, "empty_report", "No inspection records match these filters, so there is nothing to report.")
    return d


def _filename(d: ReportData, ext: str, kind: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", d.vessel_name.lower()).strip("-") or "vessel"
    return f"synergy-{kind}_{slug}_{d.generated_at:%Y%m%d-%H%M}.{ext}"


def _download(content: bytes, media: str, filename: str) -> Response:
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })


@router.get("/preview")
def preview(request: Request, store: Store, f: Filters) -> dict:
    svc = getattr(request.app.state, "analysis", None)
    # Depends on the inspection data (owner) and the saved assessments (version, read BEFORE computing, so a
    # save that happens meanwhile can only make the entry newer than its key, never stale).
    version = svc.store.version if svc is not None else 0
    key = ("report_preview", f.vessel, f.week, f.date_from, f.date_to, f.location,
           f.classification.value if f.classification else None, version)
    return get_cache(request).get_or_compute(key, lambda: _preview(request, store, f), owner=store)


def _preview(request: Request, store: InspectionStore, f: ReportFilters) -> dict:
    d = _data(request, store, f, require_content=False)
    return {
        "vessel_name": d.vessel_name,
        "period": {"from": d.period[0], "to": d.period[1]},
        "summary": d.summary,
        "analyses": [{"analysis_id": a.analysis_id, "classification": a.classification.value,
                      "review_status": a.review.status.value, "simulated": a.simulated} for a in d.analyses],
    }


@router.get("/inspections.csv")
def inspections(request: Request, store: Store, f: Filters) -> Response:
    d = _data(request, store, f, require_content=True)
    return _download(inspections_csv(d), "text/csv; charset=utf-8", _filename(d, "csv", "inspections"))


@router.get("/analyses.csv")
def analyses(request: Request, store: Store, f: Filters) -> Response:
    d = _data(request, store, f, require_content=False)
    if not d.analyses:
        raise ReportError(422, "no_analyses", "No saved AI analyses match these filters.")
    return _download(analyses_csv(d), "text/csv; charset=utf-8", _filename(d, "csv", "ai-analyses"))


@router.get("/inspection.pdf")
def pdf(request: Request, store: Store, f: Filters) -> Response:
    d = _data(request, store, f, require_content=True)

    def read_image(image_id: str) -> Optional[bytes]:
        try:
            path, _ = store.images.resolve(image_id)
            with open(path, "rb") as fh:  # read-only
                return fh.read()
        except (InvalidImageId, ImageNotFound, OSError):
            return None

    return _download(inspection_pdf(d, read_image), "application/pdf", _filename(d, "pdf", "inspection-report"))
