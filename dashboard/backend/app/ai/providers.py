"""Condition-analysis providers.

* ``DisabledProvider``  -- the default. Reports the service as unavailable; never calls anything.
* ``MockConditionProvider`` -- deterministic, offline responses for tests and explicit local
  development. Every result it produces is stored with ``simulated: true``.
* ``app.ai.gemini_provider.GeminiConditionProvider`` -- live Gemini (the intended provider; reuses the
  automation's Gemini client), behind an explicit approval flag (see that module).
* ``app.ai.groq_provider.GroqConditionProvider`` -- live Groq (vision model), behind an explicit
  approval flag (see that module).

Providers return the *raw* response; validation and policy happen in the service.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# --------------------------------------------------------------------------- errors
class ProviderError(RuntimeError):
    """Base class. ``code`` and ``message`` are safe to show to users; never include secrets."""

    code = "provider_error"
    http_status = 502
    message = "The AI service could not complete the analysis."

    def __init__(self, detail: str | None = None, retry_after_seconds: float | None = None):
        super().__init__(detail or self.message)
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(ProviderError):
    code, http_status = "ai_unavailable", 503
    message = "The AI analysis service is not available right now."


class ProviderQuotaExhausted(ProviderError):
    code, http_status = "quota_exhausted", 429
    message = "The AI service's usage quota is exhausted. Please try again later."


class ProviderRateLimited(ProviderError):
    code, http_status = "rate_limited", 429
    message = "The AI service is receiving too many requests. Please wait a minute and try again."


class ProviderTimeout(ProviderError):
    code, http_status = "ai_timeout", 504
    message = "The AI service took too long to respond. No result was saved."


class ProviderRejected(ProviderError):
    code, http_status = "ai_rejected", 502
    message = "The AI service rejected the request. Please check the AI configuration."


# --------------------------------------------------------------------------- interface
@dataclass
class ImageInput:
    image_id: str
    data: bytes
    media_type: str
    date: str


@dataclass
class ProviderResult:
    raw: Any
    usage: dict = field(default_factory=dict)
    latency_seconds: float | None = None


class ConditionProvider:
    name = "base"
    model = "none"
    simulated = False
    live = False

    def available(self) -> tuple[bool, str]:
        return False, "No AI provider is configured."

    def analyse(self, earlier: ImageInput, later: ImageInput, prompt: str) -> ProviderResult:  # pragma: no cover
        raise NotImplementedError


class DisabledProvider(ConditionProvider):
    name = "disabled"

    def __init__(self, reason: str = "AI condition analysis is not enabled on this server."):
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    def analyse(self, earlier, later, prompt):
        raise ProviderUnavailable(self.reason)


# --------------------------------------------------------------------------- mock
_EVIDENCE = {
    "region": "deck plating, lower left of frame",
    "earlier_appearance": "Light surface rust staining along the weld seam.",
    "later_appearance": "Rust staining extends further along the same weld seam.",
    "change": "Visible corrosion staining has spread along the seam.",
}

MOCK_SCENARIOS: dict[str, dict] = {
    "deteriorated": {
        "same_area": "SAME_AREA",
        "earlier_image_quality": "GOOD",
        "later_image_quality": "GOOD",
        "classification": "DETERIORATED",
        "confidence": "MEDIUM",
        "comparison_reliability": "MEDIUM",
        "visual_observations": ["Both photographs show the same winch foundation and deck plating."],
        "deterioration_evidence": [_EVIDENCE],
        "improvement_evidence": [],
        "non_condition_differences": ["The later photograph has stronger sunlight and harder shadows."],
        "limitations": ["Slightly different camera position."],
        "explanation": "Corrosion staining along the weld seam is more extensive in the later photograph.",
    },
    "ameliorated": {
        "same_area": "SAME_AREA",
        "earlier_image_quality": "GOOD",
        "later_image_quality": "GOOD",
        "classification": "AMELIORATED",
        "confidence": "HIGH",
        "comparison_reliability": "HIGH",
        "visual_observations": ["Both photographs show the same hatch coaming."],
        "deterioration_evidence": [],
        "improvement_evidence": [{
            "region": "hatch coaming, centre",
            "earlier_appearance": "Flaking paint with exposed rust patches.",
            "later_appearance": "Uniform fresh coating over the same area.",
            "change": "The coating has been visibly restored.",
        }],
        "non_condition_differences": [],
        "limitations": [],
        "explanation": "The previously flaking coating on the hatch coaming appears freshly restored.",
    },
    "stable": {
        "same_area": "SAME_AREA",
        "earlier_image_quality": "GOOD",
        "later_image_quality": "ACCEPTABLE",
        "classification": "STABLE",
        "confidence": "HIGH",
        "comparison_reliability": "HIGH",
        "visual_observations": ["The same railing and deck section are visible in both photographs."],
        "deterioration_evidence": [],
        "improvement_evidence": [],
        "non_condition_differences": ["Different cloud cover."],
        "limitations": [],
        "explanation": "No significant visible change in coating or structure can be established.",
    },
    "needs_review": {
        "same_area": "UNCERTAIN",
        "earlier_image_quality": "ACCEPTABLE",
        "later_image_quality": "POOR",
        "classification": "NEEDS_REVIEW",
        "confidence": "LOW",
        "comparison_reliability": "LOW",
        "visual_observations": ["The later photograph is blurred and taken from a different angle."],
        "deterioration_evidence": [],
        "improvement_evidence": [],
        "non_condition_differences": ["Substantially different viewing angle."],
        "limitations": ["Later photograph is blurred.", "Areas cannot be matched reliably."],
        "explanation": "A reliable comparison is not possible because the areas cannot be matched.",
    },
}


class MockConditionProvider(ConditionProvider):
    """Offline provider. ``response`` may be a scenario name, a raw object/string, a callable
    ``(earlier, later, prompt) -> raw``, or an exception instance to raise."""

    name = "mock"
    model = "mock-condition-model"
    simulated = True

    def __init__(self, response: Any = "stable", usage: Optional[dict] = None):
        self.response = response
        self.usage = usage or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.calls: list[tuple[str, str]] = []

    def available(self) -> tuple[bool, str]:
        return True, "Mock provider (simulated results, development only)."

    def analyse(self, earlier: ImageInput, later: ImageInput, prompt: str) -> ProviderResult:
        self.calls.append((earlier.image_id, later.image_id))
        r: Any = self.response
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            r = r(earlier, later, prompt)
        if isinstance(r, str) and r in MOCK_SCENARIOS:
            r = copy.deepcopy(MOCK_SCENARIOS[r])
        return ProviderResult(raw=r, usage=dict(self.usage), latency_seconds=0.0)


ProviderFactory = Callable[[], ConditionProvider]
