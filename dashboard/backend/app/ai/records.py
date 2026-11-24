"""Persisted analysis record and request/response models for the AI endpoints."""

from __future__ import annotations

import datetime as dt
import enum
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schema import SCHEMA_VERSION, Classification, Level, StrictConditionAssessment

RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
ANALYSIS_ID_RE = re.compile(r"^CA-\d{8}-[0-9a-f]{10}$")


class ReviewStatus(str, enum.Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_REASSESSMENT = "NEEDS_REASSESSMENT"


class PhotoRef(BaseModel):
    """Snapshot of the inspection record at analysis time (copied, never written back)."""

    record_id: str
    image_id: str
    sha256: Optional[str] = None
    inspection_date: dt.date
    date_source: Optional[str] = None
    report_date: Optional[str] = None
    week: str
    official_location: Optional[str] = None
    ai_suggested_location: Optional[str] = None


class ReviewEvent(BaseModel):
    status: ReviewStatus
    reviewer: str  # display name at the time of review (free text on pre-Phase-8.2 events)
    # Phase 8.2: identity from the authenticated session (absent on older events).
    reviewer_id: Optional[str] = None
    reviewer_username: Optional[str] = None
    reviewer_role: Optional[str] = None
    note: Optional[str] = None
    at: dt.datetime
    # SV-03 (absent on older events): the status before this decision, whether it was an administrator's
    # correction of an earlier final decision, and whether an administrator approved their own analysis.
    previous_status: Optional[ReviewStatus] = None
    correction: bool = False
    self_review: bool = False


class RequestedBy(BaseModel):
    """SV-03: the signed-in user who ran the analysis (taken from the session; absent on older records)."""

    user_id: str
    username: str
    role: str


class Review(BaseModel):
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    history: list[ReviewEvent] = Field(default_factory=list)


class AnalysisRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_schema_version: int = 1
    response_schema_version: int = SCHEMA_VERSION
    analysis_id: str
    dedup_key: str
    created_at: dt.datetime
    vessel_id: str
    vessel_name: str
    location: Optional[str] = Field(None, description="Location the user searched for (context only)")
    earlier: PhotoRef
    later: PhotoRef
    interval_days: int
    provider: str
    model: str
    prompt_version: str
    simulated: bool = Field(description="True for mock-provider output: not a real assessment")
    # The validated AI response exactly as accepted -- immutable after creation.
    ai_result: StrictConditionAssessment
    # Server-side policy outcome.
    classification: Classification
    model_classification: Classification
    confidence: Level
    comparison_reliability: Level
    policy_adjustments: list[str] = Field(default_factory=list)
    human_review_required: bool
    review_reasons: list[str] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    latency_seconds: Optional[float] = None
    supersedes: Optional[str] = None
    # Phase 5A: summary of the pre-analysis image-pair validation (absent on older records).
    pair_validation: Optional[dict] = None
    # Phase 8.1: rule set that produced classification/confidence (absent on older records -> v1).
    policy_version: Optional[str] = None
    # SV-03: who ran the analysis (used to stop reviewers approving their own work). None on older records.
    requested_by: Optional[RequestedBy] = None
    review: Review = Field(default_factory=Review)

    def matches(self, *, vessel_id=None, classification=None, review_status=None, image_id=None,
                location=None, earlier_record_id=None, later_record_id=None) -> bool:
        if vessel_id and self.vessel_id != vessel_id:
            return False
        if classification and self.classification.value != classification:
            return False
        if review_status and self.review.status.value != review_status:
            return False
        if image_id and image_id not in (self.earlier.image_id, self.later.image_id):
            return False
        if earlier_record_id and self.earlier.record_id != earlier_record_id:
            return False
        if later_record_id and self.later.record_id != later_record_id:
            return False
        if location:
            needle = " ".join(location.split()).casefold()
            hay = [self.location, self.earlier.official_location, self.later.official_location,
                   self.earlier.ai_suggested_location, self.later.ai_suggested_location]
            if not any(h and needle in " ".join(h.split()).casefold() for h in hay):
                return False
        return True


# --------------------------------------------------------------------------- requests
class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    earlier_record_id: str = Field(max_length=96)
    later_record_id: str = Field(max_length=96)
    location: Optional[str] = Field(None, max_length=200)
    earlier_inspection_date: Optional[dt.date] = None
    later_inspection_date: Optional[dt.date] = None
    reanalyse: bool = Field(False, description="Allowed only when the previous result was REJECTED or NEEDS_REASSESSMENT")
    acknowledge_warnings: bool = Field(False, description="The user has read the pair-validation warnings")

    @field_validator("earlier_record_id", "later_record_id")
    @classmethod
    def _ids(cls, v: str) -> str:
        if not RECORD_ID_RE.match(v):
            raise ValueError("invalid record identifier")
        return v

    @model_validator(mode="after")
    def _order(self):
        if self.earlier_record_id == self.later_record_id:
            raise ValueError("the earlier and later photographs must be different records")
        e, l = self.earlier_inspection_date, self.later_inspection_date
        if e and l and e >= l:
            raise ValueError("earlier_inspection_date must be before later_inspection_date")
        return self


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: ReviewStatus
    # Ignored since Phase 8.2: the reviewer is always taken from the signed-in session. Accepted only so
    # older clients that still send it are not rejected; it can never change the recorded identity.
    reviewer: Optional[str] = Field(None, max_length=80)
    note: Optional[str] = Field(None, max_length=1000)


class AnalysisView(AnalysisRecord):
    """SV-02: the API response shape of a saved assessment (never stored). ``requested_by_me`` is computed by the
    server for the signed-in user. For viewers and reviewers, account identifiers are removed: ``requested_by`` and
    each review event's ``reviewer_id`` / ``reviewer_username``. The reviewer's display name and role remain.
    Administrators see the full record."""

    requested_by_me: bool = False


def present(rec: AnalysisRecord, viewer) -> AnalysisView:
    """The response for ``viewer`` (an authenticated Principal). The stored record itself is never changed."""
    view = AnalysisView.model_validate({**rec.model_dump(),
                                        "requested_by_me": bool(rec.requested_by is not None and viewer is not None
                                                                and rec.requested_by.user_id == viewer.user_id)})
    if not (viewer is not None and viewer.is_admin):
        view.requested_by = None
        for event in view.review.history:
            event.reviewer_id = None
            event.reviewer_username = None
    return view


class CompareResponse(BaseModel):
    reused: bool
    analysis: AnalysisView


class HistoryPage(BaseModel):
    total: int
    count: int
    offset: int
    limit: int
    items: list[AnalysisView]
