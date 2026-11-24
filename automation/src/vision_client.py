"""Vision-capable AI client interface, with Gemini and Groq implementations and a no-op dry-run
client.

Keeping the real API behind this narrow interface means classifier.py and everything above it
never needs to change if the provider changes later (see automation/README.md). Provider choice
is a single settings.yaml value (`provider: gemini` or `provider: groq`), resolved in
build_vision_client() below -- Gemini remains the default.

PHASE 3C PERFORMANCE NOTE: the classification contract (person_visible, activity_identified,
activity_confidence, ai_suggested_location, location_confidence) is now expressed as a Pydantic
schema passed via response_schema/response_mime_type="application/json" instead of being spelled
out as prose in the prompt. Every rule from the original (Phase 3b) prose prompt is preserved --
most moved onto the specific schema field they govern, with only the cross-cutting rules
(never invent from ambiguous/indirect evidence, judge the two confidences independently, prefer
"Needs Review" when uncertain) and the confidence-level definitions left in PROMPT itself. This
shortens the combined prompt+schema text by roughly a quarter versus the old prose-only prompt,
and -- more importantly -- removes markdown-fence/JSON-parse failures as a possible error source
entirely, since the API enforces the schema structurally. Not yet validated live (quota
exhausted) -- see automation/README.md "Known limitations".
"""

from __future__ import annotations

import enum
import math
import os
import time
import types as _pytypes
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pydantic
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_exponential_jitter,
)

from .image_prep import DEFAULT_MAX_DIMENSION, resize_for_classification


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient server-side failures (5xx, network) with backoff. Never retry a 429
    RESOURCE_EXHAUSTED (quota) or other 4xx client error -- those won't be fixed by retrying
    within seconds, and blindly retrying one just burns through what quota remains (confirmed
    directly: a free-tier 20-requests/day cap was exhausted during this project's own testing,
    and retrying the same exhausted call 5x per image made it measurably worse).
    """
    try:
        from google.genai import errors
    except ImportError:
        return False
    return isinstance(exc, errors.ServerError)


# Phase P2-8: provider-neutral stop/breaker signals. classifier.py and run_pipeline.py check these
# bases, so a Groq failure of the same kind reaches the SAME existing stop / circuit-breaker
# handling. Every Gemini class below subclasses its base (and RuntimeError, as before), so every
# Gemini code path, isinstance check and exception type name is unchanged.


class ProviderServiceUnavailableError(RuntimeError):
    """A provider's server-side (5xx) failure that survived the client's bounded retries. Counted
    by run_pipeline.py's run-wide consecutive-failure circuit breaker."""


class ProviderRateLimitedError(RuntimeError):
    """A provider HTTP 429 that is not a confirmed Gemini DAILY quota exhaustion (that one is
    GeminiQuotaExhaustedError). run_pipeline.py stops the whole run on it: no further requests.
    kind is "per_minute" (only ever confirmed for Gemini) or "ambiguous" (the window was not
    identified -- never guessed). retry_delay_seconds is for reporting only, never slept on."""

    KINDS = ("per_minute", "ambiguous")

    def __init__(self, redacted_message: str, retry_delay_seconds: float | None, kind: str):
        if kind not in self.KINDS:
            raise ValueError(f"unknown rate-limit kind {kind!r}")
        self.retry_delay_seconds = retry_delay_seconds
        self.kind = kind
        super().__init__(redacted_message)


class ProviderRequestTimeoutError(RuntimeError):
    """A provider request exceeded the client-side timeout after a single attempt (never retried
    within the image). Counted by the circuit breaker, tracked separately from server errors."""


class ProviderPermanentError(RuntimeError):
    """M2: a confirmed provider-wide error that will fail every remaining photo the same way, so
    run_pipeline.py stops the whole run on it (no further requests) instead of sending one doomed
    request per photo. Raised after a single attempt -- these 4xx errors are never retried.
    kind is one of:
      "authentication" -- HTTP 401, or a Gemini HTTP 400 whose ErrorInfo reason is API_KEY_INVALID
      "permission"     -- HTTP 403
      "not_found"      -- HTTP 404 (in practice: the configured model name)
      "configuration"  -- a Gemini HTTP 400 with status FAILED_PRECONDITION (an account/region
                          precondition Google reports for the whole project, not for one photo)
    Every other HTTP 400 (e.g. an image-specific INVALID_ARGUMENT, or Groq's json_validate_failed)
    stays an ordinary per-photo failure. Carries only an already-redacted message."""

    KINDS = ("authentication", "permission", "not_found", "configuration")

    def __init__(self, redacted_message: str, kind: str, status_code: int | None):
        if kind not in self.KINDS:
            raise ValueError(f"unknown permanent-error kind {kind!r}")
        self.kind = kind
        self.status_code = status_code
        super().__init__(redacted_message)


class GeminiQuotaExhaustedError(RuntimeError):
    """Confirmed Gemini free-tier DAILY generate_content request quota exhaustion (HTTP 429,
    status RESOURCE_EXHAUSTED, with a per-day quota violation identified in the response) --
    deliberately a distinct type from an ordinary classification failure, so run_pipeline.py
    can recognize this one specific condition and stop making further requests entirely
    (current image, rest of the week, and every subsequent week) instead of just failing one
    image and moving on. Carries only an already-redacted message and the parsed retry delay --
    never the raw original exception, so there is no path for an API key to leak through it.
    """

    def __init__(self, redacted_message: str, retry_delay_seconds: float | None,
                 quota_limit: int | None = None, quota_id: str | None = None):
        self.retry_delay_seconds = retry_delay_seconds
        # Phase P1-3: the daily limit exactly as Google's 429 reported it (QuotaFailure
        # quotaValue / quotaId), or None when the response did not say. Reporting only -- never
        # used to pace, sleep or schedule anything, and never replaced by an assumed value.
        self.quota_limit = quota_limit
        self.quota_id = quota_id
        super().__init__(redacted_message)


class GeminiServiceUnavailableError(ProviderServiceUnavailableError):
    """Phase AE3: a Gemini request that still failed with a server-side error (any 5xx
    google.genai.errors.ServerError -- in practice 503 UNAVAILABLE "high demand") after every
    retry in the client's RetryPolicy was used up. Distinct from an ordinary failure so
    run_pipeline.py's run-wide consecutive-failure circuit breaker can count it. Subclasses
    RuntimeError so every existing `except RuntimeError` / "plain runtime error" expectation still
    holds. Carries only an already-redacted message -- never the raw original exception.
    """


class GeminiRateLimitedError(ProviderRateLimitedError):
    """Phase P1-1: a Gemini HTTP 429 that is NOT a confirmed daily-quota exhaustion, raised only
    after the client's bounded handling is used up. kind is one of:
      "per_minute" -- the response confirms a per-minute limit (QuotaFailure quotaId/description
                      or message naming a per-minute window); the client already waited and
                      retried once (see GeminiVisionClient._should_retry) and it persisted, or
                      Google asked for a wait longer than the configured maximum.
      "ambiguous"  -- a 429 whose response identifies neither a daily nor a per-minute window.
                      Never retried and never guessed into either category.
    run_pipeline.py stops the run on either (no further requests), like a daily-quota stop, but
    reports it distinctly. Subclasses RuntimeError, so every "plain runtime error" expectation for
    a non-daily 429 still holds. Carries only an already-redacted message. KINDS and __init__ are
    inherited unchanged from ProviderRateLimitedError (Phase P2-8)."""


# Phase P1-1: at most this many waits-and-retries per image for a confirmed per-minute 429. One is
# enough to outlast a per-minute window; more would only spend attempts on a limit that persists.
MAX_RATE_LIMIT_RETRIES_PER_IMAGE = 1


@dataclass(frozen=True)
class RetryPolicy:
    """Phase AE3: per-client retry settings for 5xx failures, read from settings.yaml's
    `rate_limit` block (see from_settings). max_retries counts retries AFTER the first attempt,
    so the default 4 means 5 total attempts per image. Waits grow exponentially with random
    jitter: min(backoff_seconds * 2**(n-1) + uniform(0, jitter_seconds), max_backoff_seconds) --
    with the defaults roughly 4-8s, 8-12s, 16-20s, 32-36s. 429/4xx are never retried regardless.

    Phase AE5 (R4): request_timeout_seconds is the per-request HTTP timeout handed to the SDK
    (types.HttpOptions(timeout=<ms>)), which also sends Google an X-Server-Timeout header with the
    same value. Must be an integer >= MIN_REQUEST_TIMEOUT_SECONDS: successful calls have been
    observed to take up to 214s, and a too-short value makes the server answer 504 (retried and
    counted as a server error) instead of a real result. A client-side timeout is never retried.

    Phase P1-1: max_backoff_seconds also bounds the wait for a per-minute 429 (see
    GeminiVisionClient._should_retry). Request pacing (requests_per_minute) is deliberately NOT
    part of this policy -- see requests_per_minute_from_settings().
    """

    MIN_REQUEST_TIMEOUT_SECONDS = 30

    max_retries: int = 4
    backoff_seconds: float = 4
    max_backoff_seconds: float = 60
    jitter_seconds: float = 4
    request_timeout_seconds: int = 300

    def __post_init__(self):
        def _num(name, value, integer=False):
            ok_type = int if integer else (int, float)
            # P2 remediation (defect 3): NaN / +inf / -inf (YAML .nan / .inf) are rejected --
            # NaN slips through "< 0" and either would break the backoff wait computation.
            if (isinstance(value, bool) or not isinstance(value, ok_type) or value < 0
                    or not math.isfinite(value)):
                kind = "a non-negative integer" if integer else "a finite, non-negative number"
                raise VisionAPIError(f"rate_limit.{name} must be {kind}, got {value!r}")

        _num("max_retries", self.max_retries, integer=True)
        _num("backoff_seconds", self.backoff_seconds)
        _num("max_backoff_seconds", self.max_backoff_seconds)
        _num("jitter_seconds", self.jitter_seconds)
        if self.max_backoff_seconds < self.backoff_seconds:
            raise VisionAPIError(
                "rate_limit.max_backoff_seconds must be >= rate_limit.backoff_seconds "
                f"({self.max_backoff_seconds!r} < {self.backoff_seconds!r})"
            )
        t = self.request_timeout_seconds
        if isinstance(t, bool) or not isinstance(t, int) or t < self.MIN_REQUEST_TIMEOUT_SECONDS:
            raise VisionAPIError(
                "rate_limit.request_timeout_seconds must be an integer >= "
                f"{self.MIN_REQUEST_TIMEOUT_SECONDS}, got {t!r}"
            )

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    @property
    def request_timeout_ms(self) -> int:
        return self.request_timeout_seconds * 1000

    def wait_strategy(self):
        return wait_exponential_jitter(
            initial=self.backoff_seconds, max=self.max_backoff_seconds, jitter=self.jitter_seconds
        )

    @classmethod
    def from_settings(cls, rate_limit: dict | None) -> "RetryPolicy":
        """Build from settings.yaml's `rate_limit` block. Missing keys fall back to the class
        defaults; unrelated keys (requests_per_minute -- read by requests_per_minute_from_settings
        -- and circuit_breaker_threshold, which run_pipeline.py reads) are ignored here."""
        if rate_limit is not None and not isinstance(rate_limit, dict):
            # P2 remediation: checked before `or {}`, so a falsy non-mapping ([] / "" / 0) is
            # rejected too instead of silently meaning "all defaults". None still means defaults.
            raise VisionAPIError(f"rate_limit must be a mapping, got {type(rate_limit).__name__}")
        rate_limit = rate_limit or {}
        fields = ("max_retries", "backoff_seconds", "max_backoff_seconds", "jitter_seconds",
                  "request_timeout_seconds")
        return cls(**{k: rate_limit[k] for k in fields if k in rate_limit})


def requests_per_minute_from_settings(rate_limit: dict | None) -> int | None:
    """Phase P1-1: validated rate_limit.requests_per_minute -- Gemini's client-side pacing cap
    (minimum gap of 60/requests_per_minute seconds between HTTP request starts, retries
    included). This project's own conservative ceiling, not Google's published limit. Missing or
    null disables pacing. Raises VisionAPIError for anything other than an integer >= 1."""
    value = (rate_limit or {}).get("requests_per_minute") if isinstance(rate_limit, dict) else None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VisionAPIError(
            f"rate_limit.requests_per_minute must be an integer >= 1 or null, got {value!r}"
        )
    return value


GROQ_RPM_SETTING = "groq_requests_per_minute"


def groq_requests_per_minute_from_settings(rate_limit: dict | None, required: bool = False) -> int | None:
    """Phase P2-9: validated rate_limit.groq_requests_per_minute -- Groq's own client-side pacing
    cap, independent of Gemini's requests_per_minute (minimum gap of 60/N seconds between Groq
    HTTP request starts, 5xx retries included). An operator-chosen safety cap, never a provider
    quota: no default is assumed, because Groq limits differ by account, model and tier.

    Any present value must be an integer >= 1 (bool, float, string, 0 and negatives are
    rejected) -- in every mode and for every provider, like the other rate_limit settings.
    Missing or null returns None, except when `required` (a live Groq run): then it is a
    configuration error, so a Groq run can never start unpaced by omission."""
    value = rate_limit.get(GROQ_RPM_SETTING) if isinstance(rate_limit, dict) else None
    if value is None:
        if required:
            raise VisionAPIError(
                f"rate_limit.{GROQ_RPM_SETTING} is required when provider is groq: set it to an "
                "integer >= 1 -- your own cap on Groq requests per minute, chosen from your Groq "
                "account's limits (no default is assumed)"
            )
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VisionAPIError(
            f"rate_limit.{GROQ_RPM_SETTING} must be an integer >= 1, got {value!r}"
        )
    return value


class GeminiRequestTimeoutError(ProviderRequestTimeoutError):
    """Phase AE5 (R4): a Gemini request exceeded the client-side HTTP timeout
    (RetryPolicy.request_timeout_seconds). Deliberately NOT treated as a 503: it is ambiguous
    (overload, a stalled network path, or a very slow answer), the server may already have
    processed -- and possibly counted -- the request, and each attempt can take the full timeout,
    so it is never retried within the same image. It does count toward run_pipeline.py's
    consecutive-failure circuit breaker, tracked separately from server errors. Carries only an
    already-redacted message."""


def _timeout_exception_types() -> tuple[type, ...]:
    """httpx2.TimeoutException and/or httpx.TimeoutException, whichever are importable (the SDK
    uses httpx2 when installed, httpx otherwise). Covers Connect/Read/Write/PoolTimeout only --
    an ordinary ConnectError is NOT a TimeoutException subclass and is deliberately not matched."""
    found = []
    for module_name in ("httpx2", "httpx"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        exc_type = getattr(module, "TimeoutException", None)
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException):
            found.append(exc_type)
    return tuple(found)


def _is_request_timeout(exc: BaseException) -> bool:
    types_ = _timeout_exception_types()
    return bool(types_) and isinstance(exc, types_)


def _is_confirmed_daily_quota_exhausted(exc: BaseException) -> bool:
    """True only for a 429 whose response explicitly identifies Gemini's free-tier DAILY
    generate_content quota as what was exhausted -- never for a per-minute rate limit, never
    for any other 4xx/5xx shape, and never guessed from the status code alone. Any malformed or
    missing error metadata is treated as "not confirmed" (returns False), never raised.
    """
    try:
        from google.genai import errors
    except ImportError:
        return False
    if not isinstance(exc, errors.ClientError):
        return False
    if getattr(exc, "code", None) != 429 or exc.status != "RESOURCE_EXHAUSTED":
        return False
    try:
        message = (exc.message or "").lower()
        if "per day" in message or "daily" in message:
            return True
        error_details = (exc.details or {}).get("error", {}).get("details", []) or []
        for d in error_details:
            if not isinstance(d, dict) or not str(d.get("@type", "")).endswith("QuotaFailure"):
                continue
            for violation in d.get("violations", []) or []:
                quota_id = str(violation.get("quotaId", "")).lower()
                description = str(violation.get("description", "")).lower()
                if "day" in quota_id or "per day" in description or "daily" in description:
                    return True
    except Exception:
        return False
    return False


def _extract_gemini_retry_delay_seconds(exc: BaseException) -> float | None:
    """Best-effort extraction of Google's own suggested RetryInfo.retryDelay (e.g. "6843s") from
    a Gemini error's response details, for logging only -- never used to sleep or auto-resume.
    Returns None if missing or malformed; never raises.
    """
    try:
        error_details = (getattr(exc, "details", None) or {}).get("error", {}).get("details", []) or []
        for d in error_details:
            if not isinstance(d, dict) or not str(d.get("@type", "")).endswith("RetryInfo"):
                continue
            delay_str = str(d.get("retryDelay", "")).strip().rstrip("s")
            return float(delay_str) if delay_str else None
    except Exception:
        return None
    return None


def _extract_gemini_daily_quota_limit(exc: BaseException) -> tuple[int | None, str | None]:
    """Phase P1-3: (quotaValue, quotaId) of the first QuotaFailure violation that names a daily
    window (quotaId containing "PerDay", e.g. GenerateRequestsPerDayPerProjectPerModel-FreeTier),
    as Google reported it. (None, None) when absent or malformed; never raises. Reporting only."""
    try:
        error_details = (getattr(exc, "details", None) or {}).get("error", {}).get("details", []) or []
        for d in error_details:
            if not isinstance(d, dict) or not str(d.get("@type", "")).endswith("QuotaFailure"):
                continue
            for violation in d.get("violations", []) or []:
                quota_id = str(violation.get("quotaId", ""))
                if "perday" not in quota_id.lower():
                    continue
                value = str(violation.get("quotaValue", "")).strip()
                limit = int(value) if value.isdigit() and int(value) > 0 else None
                return limit, quota_id
    except Exception:
        return None, None
    return None, None


def _is_confirmed_per_minute_rate_limit(exc: BaseException) -> bool:
    """Phase P1-1: True only for a 429 RESOURCE_EXHAUSTED whose response explicitly names a
    per-minute window -- a QuotaFailure violation whose quotaId contains "PerMinute" (Google's
    quota-id pattern, e.g. GenerateRequestsPerMinutePerProjectPerModel) or whose description
    says "per minute", or the same phrase in the message. Mirrors the daily check: never inferred
    from the status code alone, malformed metadata -> False, never raises. Callers must check
    _is_confirmed_daily_quota_exhausted first, so a response naming both windows counts as daily.
    """
    try:
        from google.genai import errors
    except ImportError:
        return False
    if not isinstance(exc, errors.ClientError):
        return False
    if getattr(exc, "code", None) != 429 or exc.status != "RESOURCE_EXHAUSTED":
        return False
    try:
        if "per minute" in (exc.message or "").lower():
            return True
        error_details = (exc.details or {}).get("error", {}).get("details", []) or []
        for d in error_details:
            if not isinstance(d, dict) or not str(d.get("@type", "")).endswith("QuotaFailure"):
                continue
            for violation in d.get("violations", []) or []:
                quota_id = str(violation.get("quotaId", "")).lower()
                description = str(violation.get("description", "")).lower()
                if "perminute" in quota_id or "per minute" in description:
                    return True
    except Exception:
        return False
    return False


def _gemini_429_kind(exc: BaseException) -> str | None:
    """Phase P1-1: classify a Gemini HTTP 429 as "daily", "per_minute" or "ambiguous" (any 429
    that confirms neither). None for anything that is not a google.genai ClientError with code
    429. Daily always wins when a response somehow names both windows."""
    try:
        from google.genai import errors
    except ImportError:
        return None
    if not isinstance(exc, errors.ClientError) or getattr(exc, "code", None) != 429:
        return None
    if _is_confirmed_daily_quota_exhausted(exc):
        return "daily"
    if _is_confirmed_per_minute_rate_limit(exc):
        return "per_minute"
    return "ambiguous"


class GeminiPermanentError(ProviderPermanentError):
    """M2: a confirmed provider-wide Gemini 4xx (see ProviderPermanentError)."""


# HTTP status -> ProviderPermanentError kind, for 401/403/404 (both providers).
_PERMANENT_STATUS_KINDS = {401: "authentication", 403: "permission", 404: "not_found"}


def _gemini_permanent_kind(exc: BaseException) -> str | None:
    """M2: the ProviderPermanentError kind for a confirmed provider-wide Gemini 4xx, else None.
    401/403/404 by status code; a 400 only when Google's own response confirms it is not about
    the photo: ErrorInfo reason API_KEY_INVALID (-> "authentication") or status
    FAILED_PRECONDITION (-> "configuration"). Malformed attributes -> None, never raises."""
    try:
        from google.genai import errors
    except ImportError:
        return None
    if not isinstance(exc, errors.ClientError):
        return None
    try:
        code = getattr(exc, "code", None)
        if code in _PERMANENT_STATUS_KINDS:
            return _PERMANENT_STATUS_KINDS[code]
        if code != 400:
            return None
        error_details = (exc.details or {}).get("error", {}).get("details", []) or []
        for d in error_details:
            if (isinstance(d, dict) and str(d.get("@type", "")).endswith("ErrorInfo")
                    and d.get("reason") == "API_KEY_INVALID"):
                return "authentication"
        if getattr(exc, "status", None) == "FAILED_PRECONDITION":
            return "configuration"
    except Exception:
        return None
    return None


def _groq_permanent_kind(exc: BaseException) -> str | None:
    """M2: the ProviderPermanentError kind for a Groq 401/403/404 (groq.APIStatusError), else
    None. No Groq HTTP 400 is treated as provider-wide: its error-code format has not been
    verified by this project, and its known 400 (json_validate_failed) is per-photo. Malformed
    attributes -> None, never raises."""
    try:
        import groq
    except ImportError:
        return None
    try:
        if isinstance(exc, groq.APIStatusError):
            return _PERMANENT_STATUS_KINDS.get(getattr(exc, "status_code", None))
    except Exception:
        return None
    return None


def _is_retryable_groq(exc: BaseException) -> bool:
    """Same policy as _is_retryable, for Groq: retry only 5xx (groq.InternalServerError covers
    every status_code >= 500 per the SDK's _make_status_error), never 429 (groq.RateLimitError)
    or any other 4xx.
    """
    try:
        import groq
    except ImportError:
        return False
    return isinstance(exc, groq.InternalServerError)


# Phase P2-8: Groq failure classification. Verified from the installed groq SDK's own source
# (0.37.1 and 1.7.0): groq.Groq() defaults to max_retries=2 and silently re-sends on 408, 409,
# 429, every 5xx, timeouts and connection errors, underneath this module's own retry layer -- so
# GroqVisionClient now passes max_retries=0. 429 -> groq.RateLimitError (an APIStatusError with
# .status_code and .response.headers); timeout -> groq.APITimeoutError (an APIConnectionError).

# Per-attempt HTTP timeout for Groq: the SDK's own previous default (60s total, 5s connect), now
# explicit. Groq-only; Gemini keeps rate_limit.request_timeout_seconds.
GROQ_REQUEST_TIMEOUT_SECONDS = 60.0
GROQ_CONNECT_TIMEOUT_SECONDS = 5.0


class GroqServiceUnavailableError(ProviderServiceUnavailableError):
    """Phase P2-8: a Groq 5xx (groq.InternalServerError) after every bounded retry (5 attempts)."""


class GroqRateLimitedError(ProviderRateLimitedError):
    """Phase P2-8: any Groq HTTP 429. Groq's docs describe per-minute and per-day limits, but this
    project has never observed a real Groq 429 and its body/header format is not verified, so the
    window is never inferred: every Groq 429 is kind "ambiguous" and stops the run (the safe
    choice -- it may be a daily quota). Never retried."""


# Groq request defaults (GroqVisionClient constructor), named so M3's classification_fingerprint
# can include them without inspecting the class.
GROQ_DEFAULT_REASONING_EFFORT = "none"
GROQ_DEFAULT_REASONING_FORMAT = "hidden"


class GroqPermanentError(ProviderPermanentError):
    """M2: a Groq HTTP 401/403/404 (see ProviderPermanentError). Never retried."""


class GroqRequestTimeoutError(ProviderRequestTimeoutError):
    """Phase P2-8: a Groq request exceeded GROQ_REQUEST_TIMEOUT_SECONDS (groq.APITimeoutError).
    Never retried within the image -- the server may already have processed it."""


def _is_groq_rate_limited(exc: BaseException) -> bool:
    """True for any Groq HTTP 429 (groq.RateLimitError, or any APIStatusError whose status_code is
    429). Malformed attributes -> False, never raises."""
    try:
        import groq
    except ImportError:
        return False
    if isinstance(exc, groq.RateLimitError):
        return True
    return isinstance(exc, groq.APIStatusError) and getattr(exc, "status_code", None) == 429


def _is_groq_timeout(exc: BaseException) -> bool:
    try:
        import groq
    except ImportError:
        return False
    return isinstance(exc, groq.APITimeoutError)


def _extract_groq_retry_after_seconds(exc: BaseException) -> float | None:
    """The standard Retry-After header (or the SDK-recognised retry-after-ms) of a Groq error
    response, in seconds -- for the operator message only, never slept on. Missing, malformed,
    negative, non-finite or HTTP-date values -> None (a date is not converted). Never raises."""
    try:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if headers is None:
            return None
        for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
            raw = headers.get(name)
            if raw is None:
                continue
            try:
                value = float(str(raw).strip()) / scale
            except ValueError:
                continue
            if math.isfinite(value) and value >= 0:
                return value
        return None
    except Exception:
        return None


class PersonVisible(str, enum.Enum):
    YES = "Yes"
    NO = "No"
    UNCERTAIN = "Uncertain"


class Confidence(str, enum.Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    NEEDS_REVIEW = "Needs Review"


class ClassificationSchema(pydantic.BaseModel):
    person_visible: PersonVisible = pydantic.Field(
        description=(
            "'Yes' only if a person is clearly and unambiguously visible -- never for a shape, "
            "shadow, or silhouette that merely could be mistaken for one. Otherwise 'Uncertain' "
            "(if something ambiguous is present) or 'No'."
        )
    )
    activity_identified: str = pydantic.Field(
        description=(
            "Exactly 'No activity observed' if person_visible is 'No'. Exactly 'Needs Review' if "
            "person_visible is 'Uncertain'. Exactly 'A person is visible; specific task not "
            "confirmed' if person_visible is 'Yes' but the task is not clearly observable. "
            "Otherwise, a short description of ONLY a directly, visually observable action -- "
            "never inferred merely from equipment presence, rust, or corrosion."
        )
    )
    activity_confidence: Confidence = pydantic.Field(
        description="Confidence in activity_identified only -- independent of location_confidence."
    )
    ai_suggested_location: str = pydantic.Field(
        description=(
            "The most specific ship area the visible evidence supports, in priority order: "
            "(1) a visible painted marking/compartment id (e.g. '1P','1S','2P', a hold/hatch "
            "number) naming the nearby area, e.g. a visible '1S' near a cross-deck walkway "
            "supports 'Cross deck Fwd of # 1'; (2) a recognizable structure (hatch cover, hatch "
            "coaming, lifeboat, davit, funnel, deck machinery, crane) named specifically, e.g. a "
            "close-up of a hatch cover's underside and securing cleats is 'Hatch Coaming', not "
            "'Main Deck'; (3) overall visual composition/surrounding structure if 1-2 are "
            "insufficient. Use a generic label like 'Main Deck' only if nothing more specific is "
            "supported. Exactly 'Needs Review' if no location can be supported at all. Never "
            "invent an exact location (a hold number, deck name, etc.) the image does not "
            "actually support."
        )
    )
    location_confidence: Confidence = pydantic.Field(
        description="Confidence in ai_suggested_location only -- independent of activity_confidence."
    )


PROMPT = """You are inspecting a single photograph taken during a routine ship deck inspection.

Classify only what is directly, visually observable in this photograph. Never invent a person, \
a specific activity, or an exact location from ambiguous or indirect evidence -- this includes \
never inferring an activity merely from equipment presence, rust, or corrosion. Judge \
activity_confidence and location_confidence independently: uncertainty about one must never \
change the other.

Confidence levels: High = clear, direct visual evidence. Medium = reasonable visual evidence, \
but some uncertainty. Low = weak or ambiguous evidence. Needs Review = evidence is insufficient \
for a reliable classification. When genuinely uncertain, prefer a lower confidence or \
"Needs Review" over forcing a specific classification."""


# Groq-only addendum (see GroqVisionClient._call) -- never applied to Gemini. PROMPT above is
# already tuned and validated against Gemini's behavior (Phase 3b/3c); rather than risk
# regressing that by editing PROMPT itself, qwen/qwen3.8-27b's first live call (Week 30/001,
# 2026-11-12) showed it needs these rules spelled out more explicitly than PROMPT alone conveys:
# it reported person_visible=Yes with no person actually in the photo, used the unrecognized
# phrase "Not visible" instead of the schema's exact required wording, and invented a generic
# location label ("Industrial Vessel Deck") not in this project's vocabulary. If a future model
# needs its own different adjustments, give it its own addendum rather than growing this one or
# editing the shared PROMPT.
GROQ_PROMPT_ADDENDUM = """Additional rules for this model specifically:
- Return person_visible "Yes" only for a clearly, unambiguously identifiable human figure. \
Equipment, shadows, pipes, valves, railings, ladders, and deck machinery are never evidence of a \
person, however person-shaped they may appear. If no such figure is present, use "No"; if \
genuinely ambiguous, use "Uncertain" -- never "Yes" for either case.
- A person being visible does not by itself establish any work activity. If person_visible is \
"Yes" but no specific, directly observable task is confirmed, activity_identified must be \
exactly the phrase "A person is visible; specific task not confirmed" -- never a different \
phrase such as "Not visible", "Unclear", or "Unknown".
- For ai_suggested_location, follow the priority order already given: a visible marking, then a \
specific named structure, then a generic fallback. The generic fallback must be an established \
term such as "Main Deck" -- never an invented generic label such as "Industrial Vessel Deck" or \
similar paraphrases.
- Never assign High confidence, for activity or location, without specific, direct visual \
evidence actually present in this photograph.
- Respond with ONLY the JSON object matching the required schema. Do not include any \
explanatory text, notes, disclaimers, or commentary before, after, or instead of it -- even if \
the image seems unusual or you are uncertain; express that uncertainty only through the \
schema's own fields (e.g. "Uncertain" or "Needs Review"), never as free text outside the JSON."""


class VisionAPIError(RuntimeError):
    pass


@dataclass
class UsageInfo:
    """Diagnostic info for the Phase 3d before/after comparison -- never consumed by
    classifier.py or the pipeline, which only ever use classify()'s plain dict. All fields are
    None when unavailable (e.g. NullVisionClient, or a provider that doesn't report them)."""

    latency_seconds: float
    input_text_tokens: int | None = None
    input_image_tokens: int | None = None
    output_tokens: int | None = None
    thoughts_tokens: int | None = None
    total_tokens: int | None = None


class VisionClient(ABC):
    @abstractmethod
    def classify(self, image_bytes: bytes) -> dict:
        """Return a dict with keys: activity_identified, ai_suggested_location,
        location_confidence. Must never raise for a well-formed image; API/parse failures
        should be caught internally and returned as a 'Needs Review' / Low-confidence result
        by the caller (see classifier.py), with the underlying error logged."""
        raise NotImplementedError

    def classify_with_usage(self, image_bytes: bytes) -> tuple[dict, UsageInfo]:
        """Default implementation: just times classify() and reports no token info. Overridden
        by GeminiVisionClient to also report real usage_metadata from the single underlying
        API call (never makes a second call to get this -- see GeminiVisionClient's override).
        """
        t0 = time.time()
        result = self.classify(image_bytes)
        return result, UsageInfo(latency_seconds=time.time() - t0)


class NullVisionClient(VisionClient):
    """Used when no API key is configured. Lets the rest of the pipeline (extraction, report
    generation) be exercised end-to-end in --dry-run mode without ever calling a real API.
    """

    def classify(self, image_bytes: bytes) -> dict:
        return {
            "activity_identified": "Needs Review",
            "ai_suggested_location": "Needs Review",
            "location_confidence": "Low",
        }


class GeminiVisionClient(VisionClient):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        thinking_level: str | None = None,
        max_image_dimension: int = DEFAULT_MAX_DIMENSION,
        retry_policy: RetryPolicy | None = None,
        requests_per_minute: int | None = None,
    ):
        """thinking_level: one of "LOW", "MEDIUM", "HIGH" to cap the model's internal reasoning
        budget (not all levels are accepted by every model -- an unsupported level raises
        VisionAPIError immediately rather than silently falling back). None = model default.
        Not yet validated live against this project's images -- see README "Known limitations".

        max_image_dimension: images are downscaled (never upscaled) to this on the long side
        before being sent for classification -- see image_prep.py for why. Pass 0 to disable.

        retry_policy: Phase AE3 -- 5xx retry/backoff settings (None = RetryPolicy() defaults:
        5 total attempts, jittered exponential waits capped at 60s).
        """
        self._retry_policy = retry_policy or RetryPolicy()
        # Phase P1-1 state: per-image count of per-minute-429 retries (reset by
        # _call_and_handle_errors), and request pacing (start time of the previous HTTP request).
        self._backoff_wait = self._retry_policy.wait_strategy()
        self._rate_limit_retries = 0
        self._last_request_start: float | None = None
        self._pace_sleep = time.sleep  # injectable for offline tests
        rpm = requests_per_minute_from_settings({"requests_per_minute": requests_per_minute})
        self._min_request_interval = 60.0 / rpm if rpm else 0.0
        # Rebind the tenacity-decorated _call per instance with this policy's stop/wait. Bound
        # via MethodType (not functools.partial) so `client._call.retry` still exposes this
        # instance's own Retrying object -- tests override `.retry.wait`/`.retry.sleep` on it
        # without touching the shared class-level decorator or any other client instance.
        # Phase P1-1: the retry predicate/wait now also cover ONE bounded retry of a confirmed
        # per-minute 429; stop_after_attempt still caps total HTTP attempts per image.
        self._call = _pytypes.MethodType(
            type(self)._call.retry_with(
                stop=stop_after_attempt(self._retry_policy.max_attempts),
                wait=self._retry_wait,
                retry=retry_if_exception(self._should_retry),
            ),
            self,
        )
        try:
            from google import genai
        except ImportError as exc:
            raise VisionAPIError(
                "google-genai is not installed. Run: pip install -r automation/requirements.txt"
            ) from exc

        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise VisionAPIError(
                "No Gemini API key found. Set GEMINI_API_KEY (or GOOGLE_API_KEY) in "
                "automation/.env -- see automation/config/.env.example."
            )
        from google.genai import types as _types

        # Phase AE5 (R4): client-level HTTP timeout (milliseconds) for every request. Only
        # `timeout` is set -- never `retry_options` -- so the SDK keeps its default single attempt
        # and this class's own tenacity policy remains the only retry layer.
        self._client = genai.Client(
            api_key=key,
            http_options=_types.HttpOptions(timeout=self._retry_policy.request_timeout_ms),
        )
        self._model = model
        self._api_key = key  # kept only to redact from exception text below; never logged directly
        self._max_image_dimension = max_image_dimension

        thinking_config = None
        if thinking_level is not None:
            try:
                level = _types.ThinkingLevel[thinking_level.upper()]
            except KeyError as exc:
                raise VisionAPIError(
                    f"Unknown thinking_level '{thinking_level}'. "
                    f"Valid: {[m.name for m in _types.ThinkingLevel]}"
                ) from exc
            thinking_config = _types.ThinkingConfig(thinking_level=level)

        self._generate_config = _types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ClassificationSchema,
            thinking_config=thinking_config,
        )

    # Class-level defaults only -- __init__ rebinds _call per instance with its RetryPolicy's
    # stop/wait (the retry predicate and reraise=True are inherited unchanged).
    @retry(
        stop=stop_after_attempt(RetryPolicy().max_attempts),
        wait=RetryPolicy().wait_strategy(),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _call(self, image_bytes: bytes):
        from google.genai import types

        if self._max_image_dimension:
            image_bytes = resize_for_classification(image_bytes, self._max_image_dimension)

        self._pace()  # P1-1: runs before EVERY HTTP attempt, retries included
        return self._client.models.generate_content(
            model=self._model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                PROMPT,
            ],
            config=self._generate_config,
        )

    def _pace(self) -> None:
        """P1-1: enforce 60/requests_per_minute seconds between HTTP request starts. No-op when
        requests_per_minute is None. Uses a monotonic clock; a Ctrl+C during the sleep
        propagates normally (KeyboardInterrupt is never caught here)."""
        interval = self._min_request_interval
        if not interval:
            return
        now = time.monotonic()
        if self._last_request_start is not None:
            remaining = self._last_request_start + interval - now
            if remaining > 0:
                self._pace_sleep(remaining)
                now = time.monotonic()
        self._last_request_start = now

    def _rate_limit_wait_seconds(self, exc: BaseException) -> float | None:
        """P1-1: how long to wait before retrying a confirmed per-minute 429: Google's own
        RetryInfo.retryDelay when present, otherwise max_backoff_seconds (one full bounded wait).
        Returns None when Google asks for longer than max_backoff_seconds -- then it is not
        treated as a short per-minute wait at all, and the image is not retried."""
        delay = _extract_gemini_retry_delay_seconds(exc)
        cap = self._retry_policy.max_backoff_seconds
        if delay is None:
            return cap
        return delay if 0 <= delay <= cap else None

    def _should_retry(self, exc: BaseException) -> bool:
        """tenacity retry predicate: 5xx exactly as before (_is_retryable); additionally ONE
        retry per image of a confirmed per-minute 429 whose wait fits the bound. Daily-quota,
        ambiguous 429s and every other 4xx are never retried."""
        if _is_retryable(exc):
            return True
        if (_gemini_429_kind(exc) == "per_minute"
                and self._rate_limit_retries < MAX_RATE_LIMIT_RETRIES_PER_IMAGE
                and self._rate_limit_wait_seconds(exc) is not None):
            self._rate_limit_retries += 1
            return True
        return False

    def _retry_wait(self, retry_state) -> float:
        """tenacity wait: the bounded rate-limit wait after a per-minute 429, otherwise the
        unchanged jittered exponential backoff (5xx)."""
        outcome = getattr(retry_state, "outcome", None)
        exc = outcome.exception() if outcome is not None and outcome.failed else None
        if exc is not None and _gemini_429_kind(exc) == "per_minute":
            wait = self._rate_limit_wait_seconds(exc)
            if wait is not None:
                return wait
        return self._backoff_wait(retry_state)

    def _call_and_handle_errors(self, image_bytes: bytes):
        self._rate_limit_retries = 0  # the per-minute retry budget is per image
        try:
            return self._call(image_bytes)
        except Exception as exc:
            # Defensive: some SDK/HTTP error messages can echo request details. Never let the
            # raw key reach a log file via an exception's string representation. Done here
            # (outside the retried call) so tenacity's retry-worthiness check above still sees
            # the original exception type/status, not a generic re-wrapped one.
            redacted = str(exc).replace(self._api_key, "***REDACTED***")
            if _is_confirmed_daily_quota_exhausted(exc):
                quota_limit, quota_id = _extract_gemini_daily_quota_limit(exc)
                raise GeminiQuotaExhaustedError(
                    redacted, _extract_gemini_retry_delay_seconds(exc),
                    quota_limit=quota_limit, quota_id=quota_id,
                ) from None
            kind = _gemini_429_kind(exc)
            if kind in GeminiRateLimitedError.KINDS:
                # P1-1: checked right after the daily check, so a daily quota always wins.
                raise GeminiRateLimitedError(
                    redacted, _extract_gemini_retry_delay_seconds(exc), kind
                ) from None
            if _is_retryable(exc):
                # A 5xx that survived every retry in this client's RetryPolicy -- checked only
                # after the quota check above, so quota detection always takes priority.
                raise GeminiServiceUnavailableError(redacted) from None
            if _is_request_timeout(exc):
                # Phase AE5: checked after quota and 5xx, so both keep priority. Never retried
                # (_is_retryable only matches ServerError), so this is after a single attempt.
                raise GeminiRequestTimeoutError(
                    f"request exceeded the {self._retry_policy.request_timeout_seconds}s client "
                    f"timeout ({type(exc).__name__}: {redacted})"
                ) from None
            permanent = _gemini_permanent_kind(exc)
            if permanent is not None:
                # M2: checked after quota/429/5xx/timeout, so those keep priority. A 4xx is
                # never retried (_should_retry), so this is after exactly one request.
                raise GeminiPermanentError(redacted, permanent, getattr(exc, "code", None)) from None
            raise RuntimeError(redacted) from None

    @staticmethod
    def _parse_response(response) -> dict:
        if response.parsed is not None:
            parsed = response.parsed
            return parsed.model_dump() if isinstance(parsed, pydantic.BaseModel) else dict(parsed)

        # Defensive fallback: response_schema should always populate .parsed, but if the SDK
        # or model ever returns schema-shaped JSON without populating it, don't just discard
        # a usable response -- parse the raw text the same way the pre-schema code path did.
        import json

        return json.loads(response.text)

    @staticmethod
    def _usage_info(response, latency_seconds: float) -> UsageInfo:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return UsageInfo(latency_seconds=latency_seconds)

        image_tokens = None
        text_tokens = None
        for detail in meta.prompt_tokens_details or []:
            if str(detail.modality).endswith("IMAGE"):
                image_tokens = detail.token_count
            elif str(detail.modality).endswith("TEXT"):
                text_tokens = detail.token_count

        return UsageInfo(
            latency_seconds=latency_seconds,
            input_text_tokens=text_tokens,
            input_image_tokens=image_tokens,
            output_tokens=meta.candidates_token_count,
            thoughts_tokens=meta.thoughts_token_count,
            total_tokens=meta.total_token_count,
        )

    def classify(self, image_bytes: bytes) -> dict:
        response = self._call_and_handle_errors(image_bytes)
        return self._parse_response(response)

    def classify_with_usage(self, image_bytes: bytes) -> tuple[dict, UsageInfo]:
        t0 = time.time()
        response = self._call_and_handle_errors(image_bytes)
        latency = time.time() - t0
        return self._parse_response(response), self._usage_info(response, latency)


def _build_groq_strict_schema() -> dict:
    """Derive a Groq strict-mode-compatible JSON Schema from ClassificationSchema, without
    touching the shared Pydantic model itself (GeminiVisionClient passes ClassificationSchema
    directly to response_schema and must be unaffected). Reuses model_json_schema() rather than
    hand-duplicating the long field descriptions, then fixes up the two incompatibilities Pydantic
    produces by default for strict mode (an OpenAI-compatible contract Groq's API mirrors):

    1. No object in the schema has "additionalProperties": false -- strict mode requires it.
    2. Enum fields (person_visible, activity_confidence, location_confidence) are emitted as a
       bare {"$ref": ..., "description": ...} -- a "$ref" with sibling keys alongside it is not
       supported in strict mode, so each is inlined (enum values merged with its description)
       instead of referenced via $defs.

    Every field, its description, and its enum values are preserved exactly -- this only
    restructures the schema, it never weakens or drops a constraint.
    """
    base = ClassificationSchema.model_json_schema()
    defs = base.get("$defs", {})
    properties = {}
    for name, prop in base["properties"].items():
        ref = prop.get("$ref")
        if ref:
            def_name = ref.rsplit("/", 1)[-1]
            inlined = {k: v for k, v in defs[def_name].items() if k != "title"}
            properties[name] = {**inlined, **{k: v for k, v in prop.items() if k != "$ref"}}
        else:
            properties[name] = {k: v for k, v in prop.items() if k != "title"}
    return {
        "type": "object",
        "properties": properties,
        "required": base["required"],
        "additionalProperties": False,
    }


class GroqVisionClient(VisionClient):
    """Second VisionClient implementation, evaluated as a higher-quota/lower-cost alternative to
    Gemini (see automation/README.md). Not yet validated live -- see README "Known limitations".
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        reasoning_effort: str | None = GROQ_DEFAULT_REASONING_EFFORT,
        reasoning_format: str | None = GROQ_DEFAULT_REASONING_FORMAT,
        requests_per_minute: int | None = None,
    ):
        """reasoning_effort: one of "none", "low", "medium", "high", "default" (Groq's own
        values for qwen/qwen3.8-27b) to control the model's internal reasoning budget. Defaults
        to "none" here -- Phase G explicit instruct-mode pinning, NOT a fix for the Phase F
        structured-output failures (Groq's own docs already state "none" is this model's
        default; this only makes that pin explicit and immune to a future server-side default
        change). Pass None to omit the parameter entirely and fall back to the API's own default.

        reasoning_format: one of "hidden", "raw", "parsed" (confirmed supported by the installed
        groq SDK's own type stubs). Defaults to "hidden" so no reasoning trace competes with the
        schema-conforming answer in the response content. Pass None to omit it.
        """
        try:
            import groq
        except ImportError as exc:
            raise VisionAPIError(
                "groq is not installed. Run: pip install -r automation/requirements.txt"
            ) from exc

        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise VisionAPIError(
                "No Groq API key found. Set GROQ_API_KEY in automation/.env -- see "
                "automation/config/.env.example."
            )
        # Phase P2-8: max_retries=0 -- the SDK's hidden default (2 extra attempts on 429, 5xx,
        # timeouts and connection errors) is disabled, so _call's tenacity policy below (5xx only,
        # 5 attempts) is the ONLY retry layer and a 429 costs exactly one request. The timeout is
        # the SDK's own former default, made explicit.
        import httpx

        self._client = groq.Groq(
            api_key=key, max_retries=0,
            timeout=httpx.Timeout(GROQ_REQUEST_TIMEOUT_SECONDS, connect=GROQ_CONNECT_TIMEOUT_SECONDS),
        )
        self._model = model
        self._api_key = key  # kept only to redact from exception text below; never logged directly
        self._reasoning_effort = reasoning_effort
        self._reasoning_format = reasoning_format
        # Phase P2-9: Groq's own pacing (rate_limit.groq_requests_per_minute), independent of
        # Gemini's. None = unpaced -- only reachable by direct construction; build_vision_client()
        # refuses a live Groq client without a configured value.
        rpm = groq_requests_per_minute_from_settings({GROQ_RPM_SETTING: requests_per_minute})
        self._min_request_interval = 60.0 / rpm if rpm else 0.0
        self._last_request_start: float | None = None
        self._pace_sleep = time.sleep  # injectable for offline tests

    def _pace(self) -> None:
        """Phase P2-9: enforce at least 60/groq_requests_per_minute seconds between Groq HTTP
        request STARTS (monotonic clock). The first request never waits; nothing waits after a
        request -- only before the next one. A wait is bounded by one interval (<= 60s), even if
        the clock misbehaves. Ctrl+C during the wait propagates normally."""
        interval = self._min_request_interval
        if not interval:
            return
        now = time.monotonic()
        if self._last_request_start is not None:
            remaining = min(self._last_request_start + interval - now, interval)
            if remaining > 0:
                self._pace_sleep(remaining)
                now = time.monotonic()
        self._last_request_start = now

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_is_retryable_groq),
        reraise=True,
    )
    def _call(self, image_bytes: bytes):
        import base64

        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        kwargs = {}
        if self._reasoning_effort is not None:
            kwargs["reasoning_effort"] = self._reasoning_effort
        if self._reasoning_format is not None:
            kwargs["reasoning_format"] = self._reasoning_format

        self._pace()  # P2-9: before EVERY Groq HTTP attempt, 5xx retries included
        return self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{PROMPT}\n\n{GROQ_PROMPT_ADDENDUM}"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                        },
                    ],
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "classification",
                    "strict": True,
                    "schema": _build_groq_strict_schema(),
                },
            },
            **kwargs,
        )

    def _call_and_handle_errors(self, image_bytes: bytes):
        try:
            return self._call(image_bytes)
        except Exception as exc:
            # Same defensive redaction as GeminiVisionClient._call_and_handle_errors above.
            redacted = str(exc).replace(self._api_key, "***REDACTED***")
            # Phase P2-8: classified into the provider-neutral stop/breaker signals. A 429 is
            # checked first, so it always stops the run; anything unrecognised stays an ordinary
            # failure (plain RuntimeError: one failed image, the run continues, as before).
            if _is_groq_rate_limited(exc):
                raise GroqRateLimitedError(
                    redacted, _extract_groq_retry_after_seconds(exc), "ambiguous") from None
            if _is_retryable_groq(exc):
                raise GroqServiceUnavailableError(redacted) from None
            if _is_groq_timeout(exc):
                raise GroqRequestTimeoutError(
                    f"request exceeded the {GROQ_REQUEST_TIMEOUT_SECONDS:.0f}s client timeout "
                    f"({type(exc).__name__}: {redacted})"
                ) from None
            permanent = _groq_permanent_kind(exc)
            if permanent is not None:
                # M2: after 429/5xx/timeout; a 4xx is never retried, so one request only.
                raise GroqPermanentError(redacted, permanent, getattr(exc, "status_code", None)) from None
            raise RuntimeError(redacted) from None

    @staticmethod
    def _parse_response(response) -> dict:
        content = response.choices[0].message.content
        return ClassificationSchema.model_validate_json(content).model_dump()

    @staticmethod
    def _usage_info(response, latency_seconds: float) -> UsageInfo:
        usage = getattr(response, "usage", None)
        if usage is None:
            return UsageInfo(latency_seconds=latency_seconds)

        # Groq's usage object has no modality breakdown (input_text_tokens/input_image_tokens)
        # and no separate reasoning/thoughts token count -- those stay None, per UsageInfo's
        # contract of leaving unavailable fields as None rather than guessing.
        return UsageInfo(
            latency_seconds=latency_seconds,
            output_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        )

    def classify(self, image_bytes: bytes) -> dict:
        response = self._call_and_handle_errors(image_bytes)
        return self._parse_response(response)

    def classify_with_usage(self, image_bytes: bytes) -> tuple[dict, UsageInfo]:
        t0 = time.time()
        response = self._call_and_handle_errors(image_bytes)
        latency = time.time() - t0
        return self._parse_response(response), self._usage_info(response, latency)


def build_vision_client(provider: str, model: str, dry_run: bool,
                        rate_limit: dict | None = None) -> VisionClient:
    """rate_limit: settings.yaml's `rate_limit` block -- applied to Gemini's 5xx retry policy
    and request timeout only (Phase AE3/AE5). Groq keeps its own fixed retry decorator, unchanged.

    Phase AE5 (R1): the policy is built (and therefore validated) BEFORE the dry-run branch, so
    invalid settings fail in every mode and every provider. RetryPolicy is a pure dataclass --
    building it creates no client and makes no network call."""
    retry_policy, requests_per_minute = validate_client_settings(provider, rate_limit, dry_run)
    if dry_run:
        return NullVisionClient()
    if provider == "gemini":
        return GeminiVisionClient(model=model, retry_policy=retry_policy,
                                  requests_per_minute=requests_per_minute)
    # Phase P2-9: already validated as present and >= 1 by validate_client_settings() above.
    return GroqVisionClient(model=model, requests_per_minute=groq_requests_per_minute_from_settings(
        rate_limit, required=True))


SUPPORTED_PROVIDERS = ("gemini", "groq")


def validate_client_settings(provider: str, rate_limit: dict | None,
                             dry_run: bool) -> tuple[RetryPolicy, int | None]:
    """Every check build_vision_client() makes that needs no API key -- RetryPolicy (retry +
    timeout), requests_per_minute (P1-1, every mode) and, outside a dry run, the provider name.
    Pure: builds no client, reads no environment variable, loads no .env. Safety remediation:
    run_pipeline.main() calls this BEFORE loading .env, so a configuration error never needs it."""
    retry_policy = RetryPolicy.from_settings(rate_limit)
    requests_per_minute = requests_per_minute_from_settings(rate_limit)
    if not dry_run and provider not in SUPPORTED_PROVIDERS:
        raise VisionAPIError(f"Unsupported provider '{provider}'. Supported: gemini, groq.")
    # Phase P2-9: groq_requests_per_minute -- any present value is validated in every mode and
    # for every provider; it is REQUIRED only for a live Groq run (fail-safe: no assumed default).
    groq_requests_per_minute_from_settings(rate_limit, required=(provider == "groq" and not dry_run))
    return retry_policy, requests_per_minute


def classification_fingerprint(provider: str) -> str:
    """M3: SHA-256 over everything (besides the image and the model name) that shapes what this
    provider is asked: the prompt, the response schema, and the provider-specific request
    settings this code sends by default (Gemini: image downscaling and thinking level; Groq: the
    prompt addendum, the strict schema and the reasoning defaults). Read from the live
    definitions, never hard-coded, so editing any of them changes the fingerprint and stops
    cross-week reuse of results produced under the old version. Pure: no client, no network."""
    import hashlib
    import json

    parts = {"fingerprint_version": 1, "provider": provider, "prompt": PROMPT,
             "schema": ClassificationSchema.model_json_schema()}
    if provider == "groq":
        parts.update(groq_prompt_addendum=GROQ_PROMPT_ADDENDUM, groq_schema=_build_groq_strict_schema(),
                     reasoning_effort=GROQ_DEFAULT_REASONING_EFFORT,
                     reasoning_format=GROQ_DEFAULT_REASONING_FORMAT)
    else:
        # GeminiVisionClient's defaults: max_image_dimension=DEFAULT_MAX_DIMENSION, thinking_level=None
        parts.update(max_image_dimension=DEFAULT_MAX_DIMENSION, thinking_level=None)
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()
