"""Review status transition policy (security finding SV-03, 2026-11-23).

The server -- never the browser -- decides which review decisions are allowed:

    from \\ to          APPROVED            REJECTED            NEEDS_REASSESSMENT   PENDING_REVIEW
    PENDING_REVIEW      any reviewer (*)    any reviewer        any reviewer         409
    APPROVED            --                  admin correction    admin correction     409
    REJECTED            admin correction    --                  admin correction     409
    NEEDS_REASSESSMENT  admin correction    admin correction    --                   409

    --  same status: 409 (nothing to change)
    (*) self-approval: a reviewer may not approve an analysis they ran themselves (403). Administrators may; the
        decision is then flagged ``self_review`` in the review history and the audit log. Analyses saved before
        SV-03 do not record who ran them, so no self-approval check is possible for them.

A decided assessment (APPROVED / REJECTED / NEEDS_REASSESSMENT) is final for ordinary reviewers. REJECTED and
NEEDS_REASSESSMENT are resolved by re-analysing the photographs, which creates a NEW assessment (see
AnalysisService.compare). Only an administrator can correct a decision, and must give a reason; the correction
is appended to the review history (nothing is overwritten) and audited as ``analysis_review_corrected``.
No decision can ever return an assessment to PENDING_REVIEW.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .records import ReviewStatus

FINAL_STATUSES = frozenset({ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.NEEDS_REASSESSMENT})
MIN_CORRECTION_REASON = 10

LABELS = {
    ReviewStatus.PENDING_REVIEW: "Awaiting review",
    ReviewStatus.APPROVED: "Approved",
    ReviewStatus.REJECTED: "Rejected",
    ReviewStatus.NEEDS_REASSESSMENT: "Reassessment requested",
}


class ReviewNotAllowed(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class ReviewDecision:
    correction: bool   # an administrator changed an earlier final decision
    self_review: bool  # an administrator approved an analysis they ran themselves


def check_review(current: ReviewStatus, requested: ReviewStatus, *, actor_id: str, actor_is_admin: bool,
                 requested_by_id: Optional[str], note: Optional[str]) -> ReviewDecision:
    """Raise ReviewNotAllowed unless ``requested`` is allowed from ``current`` for this actor."""
    if requested == ReviewStatus.PENDING_REVIEW:
        raise ReviewNotAllowed(409, "invalid_review_transition",
                               "An assessment cannot be returned to Awaiting review. To have the photographs looked "
                               "at again, request a reassessment.")
    if requested == current:
        raise ReviewNotAllowed(409, "review_unchanged", f"This assessment is already {LABELS[current]}.")

    correction = current in FINAL_STATUSES
    if correction:
        if not actor_is_admin:
            raise ReviewNotAllowed(409, "review_already_decided",
                                   f"This assessment has already been decided ({LABELS[current]}). Only an "
                                   "administrator can correct a decision.")
        if len((note or "").strip()) < MIN_CORRECTION_REASON:
            raise ReviewNotAllowed(422, "correction_reason_required",
                                   f"Correcting a decision requires a reason of at least {MIN_CORRECTION_REASON} "
                                   "characters.")

    self_review = requested == ReviewStatus.APPROVED and bool(requested_by_id) and requested_by_id == actor_id
    if self_review and not actor_is_admin:
        raise ReviewNotAllowed(403, "self_approval_not_allowed",
                               "You cannot approve an assessment that you analysed yourself. Another reviewer or an "
                               "administrator must approve it.")
    return ReviewDecision(correction=correction, self_review=self_review)


__all__ = ["FINAL_STATUSES", "MIN_CORRECTION_REASON", "ReviewDecision", "ReviewNotAllowed", "check_review"]
