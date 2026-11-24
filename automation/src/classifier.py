"""Turn a VisionClient's raw response into a guardrailed ClassificationResult.

Guardrail policy: anything that doesn't fit the closed vocabulary, or that the client raised
an error for, is downgraded to "Needs Review" rather than trusted verbatim or silently defaulted
to a guessed location/activity -- matching the project's "never invent" requirement. Activity
and location confidence are tracked independently (see ClassificationResult) so uncertainty
about one is never allowed to leak into the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .vision_client import (
    GeminiQuotaExhaustedError,
    ProviderPermanentError,
    ProviderRateLimitedError,
    ProviderRequestTimeoutError,
    ProviderServiceUnavailableError,
    VisionClient,
)

VALID_CONFIDENCE = {"High", "Medium", "Low", "Needs Review"}
NEEDS_REVIEW = "Needs Review"
NO_ACTIVITY = "No activity observed"
# Phase P0-2: Activity Identified for photos whose location comes from an official workbook
# heading. These are never sent to the AI, so no activity assessment was made -- this value says
# so explicitly instead of asserting NO_ACTIVITY (a finding nobody made). Never produced by
# classify_image() and never part of the AI prompt/schema.
NOT_ASSESSED_OFFICIAL_LOCATION = "Not Assessed (Official Location)"
PERSON_VISIBLE_UNCONFIRMED_TASK = "A person is visible; specific task not confirmed"

# Phrases different vision models have been observed (or could plausibly be expected) to use in
# place of the project's exact "unclear task" vocabulary. Matched case-insensitively, trimmed --
# first discovered via a live Groq call returning "Not visible" (see automation/README.md "Known
# limitations"), which the original exact-match-only check silently let through unnormalized.
_AMBIGUOUS_TASK_PHRASES = {
    NEEDS_REVIEW.lower(),
    NO_ACTIVITY.lower(),
    "",
    "not visible",
    "unclear",
    "unknown",
    "task unclear",
    "not confirmed",
}


def _is_ambiguous_task(activity: str) -> bool:
    return activity.strip().lower() in _AMBIGUOUS_TASK_PHRASES


# Phase D: known-location vocabulary, verified directly (read-only) against this project's real
# "Final Official vs AI Location Report.xlsx" files across all 11 weeks -- every Official
# Location heading actually observed, plus the generic AI-era terms already in use. This is a
# provider-agnostic safety net (applies identically regardless of which VisionClient produced the
# response), not an enum -- a genuinely new, correctly-identified specific location that isn't in
# this set yet is never rejected or rewritten, only flagged (via confidence) for human review.
# See automation/README.md "Known limitations" for why a closed enum was rejected instead.
_KNOWN_LOCATION_TERMS = {
    "main deck",
    "hatch coaming",
    "boat deck area",
    "bridge deck",
    "cargo hold no. 1",
    "cargo hold no. 2",
    "cargo hold no. 3",
    "cargo hold no. 4",
    "cargo hold no. 5",
    "cargo hold no. 6",
    "cargo hold no. 7",
    "forecastle deck area",
    "funnel deck area",
    "gangway entrance",
    "monkey island",
    "poop deck and mooring winch",
    "port and starboard main deck facing forward view",
    "rescue boat and garbage storage deck",
    "top view main deck",
}

# Matches e.g. "Cross deck Fwd of # 1" or "Cross deck Between # 5&6" case-insensitively, including
# hold numbers never yet seen in this project's real data (e.g. "# 9") -- the numbered pattern
# itself is what's trusted, not a fixed list of every number, since new hold positions are a
# legitimate, expected source of new-but-valid location text.
_CROSS_DECK_PATTERN = re.compile(r"^cross deck (fwd of|between) #\s*\d+(&\d+)?$")


def _is_recognized_location(location: str) -> bool:
    normalized = location.strip().lower()
    if normalized in _KNOWN_LOCATION_TERMS:
        return True
    return bool(_CROSS_DECK_PATTERN.match(normalized))


@dataclass
class ClassificationResult:
    activity_identified: str
    activity_confidence: str
    ai_suggested_location: str
    location_confidence: str
    person_visible: str | None = None
    error: str | None = None
    # Set only for a confirmed Gemini daily-quota-exhaustion failure (see
    # vision_client.GeminiQuotaExhaustedError) -- run_pipeline.py uses this to stop making any
    # further requests, in this week and every subsequent one. False/None for every other
    # outcome, including an ordinary visual Needs Review that completed successfully.
    quota_exhausted: bool = False
    retry_delay_seconds: float | None = None
    # Phase AE3: set only when the provider still returned a 5xx after every configured retry
    # (vision_client.ProviderServiceUnavailableError: Gemini's, or Groq's since P2-8) -- counted by run_pipeline.py's run-wide
    # consecutive-failure circuit breaker. Never set together with quota_exhausted.
    service_unavailable: bool = False
    # Phase AE5: set only for a client-side request timeout (vision_client.
    # ProviderRequestTimeoutError, Gemini or Groq) -- also counted by the circuit breaker, but tracked separately
    # from service_unavailable since a timeout is not equivalent to an explicit 5xx answer.
    request_timeout: bool = False
    # Phase P1-1: set for a non-daily HTTP 429 (vision_client.ProviderRateLimitedError) after the
    # client's bounded handling -- rate_limit_kind is "per_minute" or "ambiguous". run_pipeline.py
    # stops the run on it. Never set together with quota_exhausted.
    rate_limited: bool = False
    rate_limit_kind: str | None = None
    # Phase P1-3: the daily limit Google's quota 429 reported (quotaValue / quotaId), or None.
    # Reporting only; set only together with quota_exhausted.
    quota_limit: int | None = None
    quota_id: str | None = None
    # M2: set only for a confirmed provider-wide 4xx (vision_client.ProviderPermanentError):
    # "authentication", "permission", "not_found" or "configuration", plus its HTTP status.
    # run_pipeline.py stops the run on it.
    provider_error_kind: str | None = None
    provider_status_code: int | None = None


def _clean_str(value, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _clean_confidence(value) -> str:
    return value if value in VALID_CONFIDENCE else "Low"


def classify_image(client: VisionClient, image_bytes: bytes) -> ClassificationResult:
    try:
        raw = client.classify(image_bytes)
    except Exception as exc:  # noqa: BLE001 -- any client/network/parse failure must degrade safely
        return ClassificationResult(
            activity_identified=NEEDS_REVIEW,
            activity_confidence=NEEDS_REVIEW,
            ai_suggested_location=NEEDS_REVIEW,
            location_confidence=NEEDS_REVIEW,
            person_visible="Uncertain",
            error=f"{type(exc).__name__}: {exc}",
            quota_exhausted=isinstance(exc, GeminiQuotaExhaustedError),
            retry_delay_seconds=getattr(exc, "retry_delay_seconds", None),
            # Phase P2-8: provider-neutral bases -- the Gemini subclasses match exactly as before,
            # and the Groq equivalents now reach the same stop / circuit-breaker handling.
            service_unavailable=isinstance(exc, ProviderServiceUnavailableError),
            request_timeout=isinstance(exc, ProviderRequestTimeoutError),
            rate_limited=isinstance(exc, ProviderRateLimitedError),
            rate_limit_kind=getattr(exc, "kind", None) if isinstance(exc, ProviderRateLimitedError) else None,
            quota_limit=getattr(exc, "quota_limit", None) if isinstance(exc, GeminiQuotaExhaustedError) else None,
            quota_id=getattr(exc, "quota_id", None) if isinstance(exc, GeminiQuotaExhaustedError) else None,
            provider_error_kind=getattr(exc, "kind", None) if isinstance(exc, ProviderPermanentError) else None,
            provider_status_code=(getattr(exc, "status_code", None)
                                  if isinstance(exc, ProviderPermanentError) else None),
        )

    person_visible = raw.get("person_visible")
    if person_visible not in ("Yes", "No", "Uncertain"):
        person_visible = "Uncertain"

    activity = _clean_str(raw.get("activity_identified"), NEEDS_REVIEW)
    activity_confidence = _clean_confidence(raw.get("activity_confidence"))
    location = _clean_str(raw.get("ai_suggested_location"), NEEDS_REVIEW)
    location_confidence = _clean_confidence(raw.get("location_confidence"))

    # Enforce the activity rules as hard guardrails -- never trust the model's self-report of
    # person_visible vs. activity_identified to already be consistent on its own.
    if person_visible == "Uncertain":
        activity = NEEDS_REVIEW
        activity_confidence = NEEDS_REVIEW
    elif person_visible == "No":
        activity = NO_ACTIVITY
        activity_confidence = "High" if activity_confidence == NEEDS_REVIEW else activity_confidence
    elif person_visible == "Yes" and _is_ambiguous_task(activity):
        # a person was reported visible but no specific, observable task was described --
        # preserve that as an honest "presence confirmed, task unclear" rather than either
        # inventing a task or silently reporting "no activity" against a person being present.
        # _is_ambiguous_task catches every phrasing a model has used (or plausibly could use)
        # for this, not just Gemini's original exact strings -- see _AMBIGUOUS_TASK_PHRASES.
        activity = PERSON_VISIBLE_UNCONFIRMED_TASK
        activity_confidence = "Needs Review"

    if location == NEEDS_REVIEW:
        location_confidence = NEEDS_REVIEW
    elif not _is_recognized_location(location):
        # Never rewrite or discard the text (it may well be correct -- just unverifiable against
        # the known vocabulary) and never default it to "Main Deck" -- only flag it for human
        # review via confidence, exactly like the ambiguous-activity guardrail above.
        location_confidence = NEEDS_REVIEW

    return ClassificationResult(
        activity_identified=activity,
        activity_confidence=activity_confidence,
        ai_suggested_location=location,
        location_confidence=location_confidence,
        person_visible=person_visible,
    )
