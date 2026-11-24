"""AI condition analysis endpoints. Only POST endpoints can trigger or change anything."""

import datetime as dt
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..ai.pair_validation import PairValidation
from ..auth.deps import RequireReviewer, RequireUser, get_auth, require_user
from ..ai.records import (RECORD_ID_RE, ANALYSIS_ID_RE, AnalysisRecord, AnalysisView, CompareRequest, CompareResponse,
                          HistoryPage, ReviewRequest, ReviewStatus, present)
from ..ai.schema import Classification
from ..ai.service import AnalysisError, AnalysisService

# SV-02: reading needs a signed-in account; running analyses and reviewing needs a reviewer or administrator.
router = APIRouter(prefix="/api/ai-analysis", tags=["ai-analysis"], dependencies=[Depends(require_user)])

SortKey = Literal["analysed_desc", "analysed_asc", "later_date_desc", "later_date_asc", "classification", "review_status"]
_CLASS_ORDER = {"DETERIORATED": 0, "NEEDS_REVIEW": 1, "AMELIORATED": 2, "STABLE": 3}
_REVIEW_ORDER = {"PENDING_REVIEW": 0, "NEEDS_REASSESSMENT": 1, "REJECTED": 2, "APPROVED": 3}
SORTS = {
    "analysed_desc": (lambda a: a.created_at, True),
    "analysed_asc": (lambda a: a.created_at, False),
    "later_date_desc": (lambda a: (a.later.inspection_date, a.created_at), True),
    "later_date_asc": (lambda a: (a.later.inspection_date, a.created_at), False),
    "classification": (lambda a: (_CLASS_ORDER[a.classification.value], a.created_at.timestamp() * -1), False),
    "review_status": (lambda a: (_REVIEW_ORDER[a.review.status.value], a.created_at.timestamp() * -1), False),
}


def get_service(request: Request) -> AnalysisService:
    svc = getattr(request.app.state, "analysis", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="data not loaded")
    return svc


Svc = Annotated[AnalysisService, Depends(get_service)]


@router.get("/status")
def status(svc: Svc) -> dict:
    """Availability and configuration. Never includes credentials and never calls the provider."""
    return svc.status()


@router.get("/validate", response_model=PairValidation)
def validate(
    svc: Svc,
    earlier_record_id: str = Query(..., max_length=96),
    later_record_id: str = Query(..., max_length=96),
) -> PairValidation:
    """Image-pair eligibility check. Read-only; never calls the AI provider and never saves anything."""
    for rid in (earlier_record_id, later_record_id):
        if not RECORD_ID_RE.match(rid):
            raise HTTPException(status_code=400, detail="invalid record identifier")
    return svc.validate(earlier_record_id, later_record_id)


@router.post("/compare", response_model=CompareResponse)
def compare(body: CompareRequest, svc: Svc, user: RequireReviewer, request: Request) -> CompareResponse:
    audit = get_auth(request).audit
    try:
        rec, reused = svc.compare(body, requested_by=user)
    except AnalysisError as exc:
        audit.record("ai_analysis_failed", actor=user.actor(), target=f"{body.earlier_record_id}->{body.later_record_id}",
                     details={"code": exc.code, "status": exc.status})
        raise
    audit.record("ai_analysis_reused" if reused else "ai_analysis_run", actor=user.actor(), target=rec.analysis_id,
                 details={"earlier_record_id": body.earlier_record_id, "later_record_id": body.later_record_id,
                          "classification": rec.classification.value, "simulated": rec.simulated,
                          "policy_version": rec.policy_version})
    return CompareResponse(reused=reused, analysis=present(rec, user))


@router.get("/history", response_model=HistoryPage)
def history(
    svc: Svc,
    user: RequireUser,
    vessel: Optional[str] = None,
    location: Annotated[Optional[str], Query(max_length=200)] = None,
    classification: Optional[Classification] = None,
    review_status: Optional[ReviewStatus] = None,
    image_id: Annotated[Optional[str], Query(max_length=96)] = None,
    earlier_record_id: Annotated[Optional[str], Query(max_length=96)] = None,
    later_record_id: Annotated[Optional[str], Query(max_length=96)] = None,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    q: Annotated[Optional[str], Query(max_length=100)] = None,
    sort: SortKey = "analysed_desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HistoryPage:
    """Saved analyses only (never triggers analysis). date_from/date_to match when either inspection
    date falls in the range; q searches analysis id, image ids, record ids and locations."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from must be on or before date_to")
    items = svc.store.find(
        vessel_id=vessel, location=location,
        classification=classification.value if classification else None,
        review_status=review_status.value if review_status else None,
        image_id=image_id, earlier_record_id=earlier_record_id, later_record_id=later_record_id,
    )
    if date_from or date_to:
        def in_range(d: dt.date) -> bool:
            return (date_from is None or d >= date_from) and (date_to is None or d <= date_to)
        items = [a for a in items if in_range(a.earlier.inspection_date) or in_range(a.later.inspection_date)]
    if q and q.strip():
        needle = " ".join(q.split()).casefold()
        def hay(a: AnalysisRecord) -> str:
            return " ".join(filter(None, [a.analysis_id, a.earlier.image_id, a.later.image_id, a.earlier.record_id,
                                          a.later.record_id, a.location, a.earlier.official_location,
                                          a.later.official_location, a.earlier.ai_suggested_location,
                                          a.later.ai_suggested_location])).casefold()
        items = [a for a in items if needle in " ".join(hay(a).split())]
    items = sorted(items, key=SORTS[sort][0], reverse=SORTS[sort][1])
    page = items[offset: offset + limit]
    return HistoryPage(total=len(items), count=len(page), offset=offset, limit=limit,
                       items=[present(a, user) for a in page])  # SV-02: identity fields per role


def _checked_id(analysis_id: str) -> str:
    if not ANALYSIS_ID_RE.match(analysis_id):
        raise HTTPException(status_code=400, detail="invalid analysis id")
    return analysis_id


@router.get("/{analysis_id}", response_model=AnalysisView)
def get_analysis(analysis_id: str, svc: Svc, user: RequireUser) -> AnalysisView:
    rec = svc.get(_checked_id(analysis_id))
    if rec is None:
        raise HTTPException(status_code=404, detail="analysis not found")
    return present(rec, user)


@router.post("/{analysis_id}/review", response_model=AnalysisView)
def review(analysis_id: str, body: ReviewRequest, svc: Svc, user: RequireReviewer, request: Request) -> AnalysisView:
    """Transition rules and the self-approval check are enforced in AnalysisService.review (SV-03); a refused
    decision raises before anything is saved or audited as a review."""
    rec = svc.review(_checked_id(analysis_id), body, user)
    event = rec.review.history[-1]
    details = {"decision": body.status.value, "previous_status": event.previous_status.value,
               "note_provided": bool(body.note)}
    if event.self_review:
        details["self_review"] = True
    if event.correction:
        # An administrator changed an earlier final decision; the reason is the note (kept in the review history).
        details["reason"] = (body.note or "")[:200]
    get_auth(request).audit.record("analysis_review_corrected" if event.correction else "analysis_reviewed",
                                   actor=user.actor(), target=rec.analysis_id, details=details)
    return present(rec, user)
