"""Orchestrates a user-requested comparison of one earlier and one later photograph."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import threading
import uuid
from typing import Optional

from ..infra.circuit_breaker import CircuitBreaker, CircuitOpen
from ..models import InspectionRecord
from ..services.image_service import ImageNotFound, InvalidImageId
from ..services.inspection_store import InspectionStore
from .pair_validation import PairValidation, PairValidator
from .prompts import PROMPT_VERSION, build_prompt
from .providers import ConditionProvider, ImageInput, ProviderError
from .records import AnalysisRecord, CompareRequest, PhotoRef, RequestedBy, ReviewEvent, ReviewRequest, ReviewStatus
from .review_policy import ReviewNotAllowed, check_review
from .schema import Classification, InvalidAIResponse, apply_policy, validate_assessment
from .store import AnalysisStore

REANALYSABLE = {ReviewStatus.REJECTED, ReviewStatus.NEEDS_REASSESSMENT}
# HTTP status for each blocking pair-validation error.
VALIDATION_STATUS = {
    "unknown_record": 404, "same_record": 400, "different_vessels": 400, "undated_record": 422,
    "same_inspection_date": 400, "invalid_date_order": 400, "same_image": 400,
    "photograph_unavailable": 422, "photograph_unreadable": 422, "file_integrity": 422,
}
# Warnings that make any model classification untrustworthy: the result is downgraded to NEEDS_REVIEW.
DOWNGRADING_WARNINGS = {"visually_identical", "near_duplicate", "location_conflict"}


class AnalysisError(Exception):
    """A request-level failure with a user-safe code and message."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AnalysisService:
    def __init__(self, inspections: InspectionStore, store: AnalysisStore, provider: ConditionProvider,
                 breaker: Optional[CircuitBreaker] = None):
        self.inspections = inspections
        self.store = store
        self.provider = provider
        # Infrastructure: stops calling a provider that keeps failing (see app/infra/circuit_breaker.py).
        self.breaker = breaker or CircuitBreaker()
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self.validator = PairValidator(inspections)

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        ok, reason = self.provider.available()
        return {
            "available": ok,
            "message": reason,
            "provider": self.provider.name,
            "model": self.provider.model,
            "simulated": self.provider.simulated,
            "live": self.provider.live,
            "prompt_version": PROMPT_VERSION,
            "classifications": ["DETERIORATED", "AMELIORATED", "STABLE", "NEEDS_REVIEW"],
            "review_statuses": [s.value for s in ReviewStatus],
            "store": self.store.status(),
            "circuit": self.breaker.snapshot(),
        }

    # ------------------------------------------------------------------ helpers
    def _record(self, record_id: str, side: str) -> InspectionRecord:
        rec = self.inspections.by_id.get(record_id)
        if rec is None:
            raise AnalysisError(404, "unknown_record", f"The {side} inspection record was not found.")
        return rec

    def _photo(self, rec: InspectionRecord, side: str) -> tuple[bytes, str]:
        if rec.image_status != "resolved" or not rec.image_id:
            raise AnalysisError(422, "photograph_unavailable", f"The {side} photograph is not available.")
        try:
            path, media = self.inspections.images.resolve(rec.image_id)
            with open(path, "rb") as f:  # read-only
                return f.read(), media
        except (InvalidImageId, ImageNotFound, OSError):
            raise AnalysisError(422, "photograph_unavailable", f"The {side} photograph could not be read.") from None

    @staticmethod
    def _ref(rec: InspectionRecord) -> PhotoRef:
        p = rec.extracted_photograph
        return PhotoRef(
            record_id=rec.record_id, image_id=rec.image_id, sha256=p.sha256 if p else None,
            inspection_date=rec.inspection_date, date_source=rec.date_source, report_date=rec.report_date,
            week=rec.week, official_location=rec.official_location if rec.official_location_labelled else None,
            ai_suggested_location=rec.ai_suggested_location,
        )

    def _dedup_key(self, e: PhotoRef, l: PhotoRef) -> str:
        parts = [e.record_id, l.record_id, e.sha256 or e.image_id, l.sha256 or l.image_id,
                 self.provider.name, self.provider.model, PROMPT_VERSION]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    # ------------------------------------------------------------------ compare
    def validate(self, earlier_record_id: str, later_record_id: str) -> PairValidation:
        """Pre-analysis eligibility check. Reads only the two selected files; never calls the AI."""
        return self.validator.validate(earlier_record_id, later_record_id)

    def validate_pair(self, req: CompareRequest) -> tuple[InspectionRecord, InspectionRecord, PairValidation]:
        v = self.validate(req.earlier_record_id, req.later_record_id)
        err = v.first_error()
        if err is not None:
            raise AnalysisError(VALIDATION_STATUS.get(err.code, 400), err.code, err.message)
        e, l = self.inspections.by_id[req.earlier_record_id], self.inspections.by_id[req.later_record_id]
        for side, given, actual in (("earlier", req.earlier_inspection_date, e.inspection_date),
                                    ("later", req.later_inspection_date, l.inspection_date)):
            if given is not None and given != actual:
                raise AnalysisError(400, "date_mismatch", f"The {side} inspection date does not match the record.")
        return e, l, v

    def compare(self, req: CompareRequest, requested_by=None) -> tuple[AnalysisRecord, bool]:
        """requested_by: the authenticated Principal running the analysis (stored for the SV-03 self-approval
        check); never taken from the request body."""
        e, l, validation = self.validate_pair(req)
        e_bytes, e_media = self._photo(e, "earlier")
        l_bytes, l_media = self._photo(l, "later")
        e_ref, l_ref = self._ref(e), self._ref(l)
        key = self._dedup_key(e_ref, l_ref)

        previous = self.store.latest_for_key(key)
        if previous is not None:
            if not req.reanalyse:
                return previous, True
            if previous.review.status not in REANALYSABLE:
                raise AnalysisError(409, "already_analysed",
                                    "This pair already has an analysis. Re-analysis is allowed only after it is "
                                    "marked Rejected or Needs reassessment.")

        if validation.requires_acknowledgement and not req.acknowledge_warnings:
            raise AnalysisError(409, "validation_warnings",
                                "This pair has validation warnings. Read and acknowledge them before analysing.")

        ok, reason = self.provider.available()
        if not ok:
            raise AnalysisError(503, "ai_unavailable", reason)

        with self._lock:
            if key in self._in_flight:
                raise AnalysisError(409, "analysis_in_progress", "This pair is already being analysed.")
            self._in_flight.add(key)
        try:
            interval = (l.inspection_date - e.inspection_date).days
            prompt = build_prompt(location=req.location or e_ref.official_location or e_ref.ai_suggested_location,
                                  earlier_date=e.inspection_date.isoformat(),
                                  later_date=l.inspection_date.isoformat(), interval_days=interval)
            try:
                self.breaker.before_call()
            except CircuitOpen as exc:
                wait = int(math.ceil(exc.retry_after))
                raise AnalysisError(503, "ai_circuit_open",
                                    "The AI service failed several times in a row, so analysis is paused for about "
                                    f"{wait} seconds. No request was sent and no result was saved. Please try again later.",
                                    wait) from None
            try:
                result = self.provider.analyse(
                    ImageInput(e.image_id, e_bytes, e_media, e.inspection_date.isoformat()),
                    ImageInput(l.image_id, l_bytes, l_media, l.inspection_date.isoformat()),
                    prompt,
                )
            except ProviderError as exc:
                self.breaker.record_failure(exc.retry_after_seconds)
                raise AnalysisError(exc.http_status, exc.code, type(exc).message, exc.retry_after_seconds) from None
            except BaseException:
                self.breaker.record_failure()  # unexpected crash inside the provider also counts (and frees a trial)
                raise
            # The provider answered. An answer that fails validation (below) is not an outage.
            self.breaker.record_success()
            try:
                assessment = validate_assessment(result.raw)
            except InvalidAIResponse as exc:
                raise AnalysisError(502, "invalid_ai_response",
                                    f"The AI service returned an invalid response ({exc.reason}). No result was saved.") from None
            policy = apply_policy(assessment)
            classification, adjustments = policy.classification, list(policy.adjustments)
            review_required, review_reasons = policy.human_review_required, list(policy.review_reasons)
            codes = {w.code for w in validation.warnings}
            if codes & DOWNGRADING_WARNINGS and classification != Classification.NEEDS_REVIEW:
                adjustments.append(f"{classification.value} -> NEEDS_REVIEW: pair validation warning "
                                   f"({', '.join(sorted(codes & DOWNGRADING_WARNINGS))})")
                classification = Classification.NEEDS_REVIEW
            if validation.requires_human_review:
                review_required = True
                review_reasons += [f"Pair validation: {w.message}" for w in validation.warnings]
            now = _now()
            rec = AnalysisRecord(
                analysis_id=f"CA-{now:%Y%m%d}-{uuid.uuid4().hex[:10]}",
                dedup_key=key, created_at=now,
                vessel_id=e.vessel_id, vessel_name=e.vessel_name, location=req.location,
                earlier=e_ref, later=l_ref, interval_days=interval,
                provider=self.provider.name, model=self.provider.model, prompt_version=PROMPT_VERSION,
                simulated=self.provider.simulated,
                ai_result=assessment,
                classification=classification, model_classification=assessment.classification,
                confidence=policy.confidence or assessment.confidence, comparison_reliability=assessment.comparison_reliability,
                policy_adjustments=adjustments, human_review_required=review_required,
                review_reasons=review_reasons, pair_validation=validation.summary(), policy_version=policy.policy_version,
                usage={k: v for k, v in (result.usage or {}).items() if v is not None},
                latency_seconds=result.latency_seconds,
                supersedes=previous.analysis_id if previous is not None else None,
                requested_by=(RequestedBy(user_id=requested_by.user_id, username=requested_by.username,
                                          role=requested_by.role) if requested_by is not None else None),
            )
            try:
                self.store.save(rec)
            except OSError:
                raise AnalysisError(500, "storage_error", "The analysis completed but could not be saved.") from None
            return rec, False
        finally:
            with self._lock:
                self._in_flight.discard(key)

    # ------------------------------------------------------------------ review
    def review(self, analysis_id: str, req: ReviewRequest, reviewer) -> AnalysisRecord:
        """reviewer: the authenticated Principal -- never the name sent in the request.

        SV-03: the transition is checked against the CURRENT saved status (app/ai/review_policy.py) inside the
        lock, before anything is copied or written, so a refused decision changes nothing and two concurrent
        decisions cannot both be applied to a pending assessment."""
        with self._lock:
            rec = self.store.get(analysis_id)
            if rec is None:
                raise AnalysisError(404, "unknown_analysis", "Analysis not found.")
            try:
                decision = check_review(rec.review.status, req.status, actor_id=reviewer.user_id,
                                        actor_is_admin=reviewer.role == "admin",
                                        requested_by_id=rec.requested_by.user_id if rec.requested_by else None,
                                        note=req.note)
            except ReviewNotAllowed as exc:
                raise AnalysisError(exc.status, exc.code, exc.message) from None
            event = ReviewEvent(status=req.status, reviewer=reviewer.display_name, reviewer_id=reviewer.user_id,
                                reviewer_username=reviewer.username, reviewer_role=reviewer.role, note=req.note or None, at=_now(),
                                previous_status=rec.review.status, correction=decision.correction,
                                self_review=decision.self_review)
            # The AI result, classification and policy fields are never changed by a review.
            updated = rec.model_copy(deep=True)
            updated.review.status = req.status
            updated.review.history.append(event)
            try:
                return self.store.save(updated)
            except OSError:
                raise AnalysisError(500, "storage_error", "The review could not be saved.") from None

    def get(self, analysis_id: str) -> Optional[AnalysisRecord]:
        return self.store.get(analysis_id)
