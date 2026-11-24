"""Condition-comparison response schema, validation and policy rules.

Two layers:

* ``ConditionAssessment`` (+ ``Evidence``) is the *model-facing* schema sent to the provider (Groq:
  as the JSON Schema in the prompt, with JSON mode). It carries field descriptions only -- no ``extra='forbid'`` or length
  constraints, which structured-output schemas do not reliably accept (same convention as
  ``automation/src/vision_client.ClassificationSchema``).
* ``StrictConditionAssessment`` re-validates every response before anything is saved:
  unknown fields are rejected, strings are trimmed and length-limited, lists are bounded.

``apply_policy`` then enforces the classification rules server-side. The model's own
classification is never trusted blindly: inconsistent or low-reliability answers are
downgraded to NEEDS_REVIEW, and every adjustment is recorded with its reason.
"""

from __future__ import annotations

import enum
import json
from typing import Any, Optional

import pydantic
from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 1


class Classification(str, enum.Enum):
    DETERIORATED = "DETERIORATED"
    AMELIORATED = "AMELIORATED"
    STABLE = "STABLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class Level(str, enum.Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SameArea(str, enum.Enum):
    SAME_AREA = "SAME_AREA"
    PARTIAL_OVERLAP = "PARTIAL_OVERLAP"
    DIFFERENT_AREA = "DIFFERENT_AREA"
    UNCERTAIN = "UNCERTAIN"


class ImageQuality(str, enum.Enum):
    GOOD = "GOOD"
    ACCEPTABLE = "ACCEPTABLE"
    POOR = "POOR"


# --------------------------------------------------------------------------- model-facing schema
class Evidence(BaseModel):
    region: str = Field(description="Where in the frame, e.g. 'left winch drum base' or 'deck plating, lower right'.")
    earlier_appearance: str = Field(description="What is directly visible in this region in the EARLIER photograph.")
    later_appearance: str = Field(description="What is directly visible in the same region in the LATER photograph.")
    change: str = Field(description="The physical-condition change, stated only as far as both photographs show it.")


class ConditionAssessment(BaseModel):
    same_area: SameArea = Field(
        description="Whether both photographs show the same physical area. DIFFERENT_AREA or UNCERTAIN when "
        "the main structures cannot be matched between the two photographs."
    )
    earlier_image_quality: ImageQuality = Field(description="Usability of the EARLIER photograph for condition assessment.")
    later_image_quality: ImageQuality = Field(description="Usability of the LATER photograph for condition assessment.")
    classification: Classification = Field(
        description="DETERIORATED / AMELIORATED only with specific visible evidence of a physical-condition change "
        "in the same area. STABLE when no significant physical change can be established. NEEDS_REVIEW when a "
        "reliable comparison is not possible."
    )
    confidence: Level = Field(description="Confidence in the classification.")
    comparison_reliability: Level = Field(
        description="How reliably the two photographs can be compared at all (same area, angle, distance, lighting, sharpness)."
    )
    visual_observations: list[str] = Field(
        description="Short, factual observations of what is directly visible (1-8 items). No speculation."
    )
    deterioration_evidence: list[Evidence] = Field(
        description="Specific visible signs that condition worsened between EARLIER and LATER. Empty if none."
    )
    improvement_evidence: list[Evidence] = Field(
        description="Specific visible signs that condition improved between EARLIER and LATER. Empty if none."
    )
    non_condition_differences: list[str] = Field(
        description="Differences that are NOT physical-condition changes (lighting, shadows, angle, distance, "
        "orientation, resolution, temporary objects, people, cargo, water, covers). Empty if none."
    )
    limitations: list[str] = Field(description="Factors limiting this comparison. Empty if none.")
    explanation: str = Field(description="Concise explanation (2-5 sentences) grounded only in the listed evidence.")


# --------------------------------------------------------------------------- strict validation
MAX_ITEMS = 8
MAX_SHORT = 160
MAX_TEXT = 400
MAX_EXPLANATION = 1500


def _clean(value: Any, *, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    text = " ".join(value.split())
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return text


def _clean_list(values: Any, *, field: str, limit: int = MAX_TEXT, min_items: int = 0) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list")
    if not (min_items <= len(values) <= MAX_ITEMS):
        raise ValueError(f"{field} must have {min_items}-{MAX_ITEMS} items")
    return [_clean(v, limit=limit, field=field) for v in values]


class StrictEvidence(Evidence):
    model_config = ConfigDict(extra="forbid")

    @field_validator("region", mode="before")
    @classmethod
    def _region(cls, v):
        return _clean(v, limit=MAX_SHORT, field="region")

    @field_validator("earlier_appearance", "later_appearance", "change", mode="before")
    @classmethod
    def _text(cls, v, info):
        return _clean(v, limit=MAX_TEXT, field=info.field_name)


class StrictConditionAssessment(ConditionAssessment):
    model_config = ConfigDict(extra="forbid")

    deterioration_evidence: list[StrictEvidence]
    improvement_evidence: list[StrictEvidence]

    @field_validator("visual_observations", mode="before")
    @classmethod
    def _observations(cls, v):
        return _clean_list(v, field="visual_observations", min_items=1)

    @field_validator("non_condition_differences", "limitations", mode="before")
    @classmethod
    def _lists(cls, v, info):
        return _clean_list(v, field=info.field_name)

    @field_validator("deterioration_evidence", "improvement_evidence", mode="before")
    @classmethod
    def _evidence(cls, v, info):
        if not isinstance(v, list) or len(v) > MAX_ITEMS:
            raise ValueError(f"{info.field_name} must be a list of at most {MAX_ITEMS} items")
        return v

    @field_validator("explanation", mode="before")
    @classmethod
    def _explanation(cls, v):
        text = _clean(v, limit=MAX_EXPLANATION, field="explanation")
        if len(text) < 10:
            raise ValueError("explanation is too short")
        return text


class InvalidAIResponse(ValueError):
    """The provider's output is not a valid assessment. Nothing is saved."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_assessment(raw: Any) -> StrictConditionAssessment:
    """Parse and strictly validate raw provider output (dict, model instance or JSON text)."""
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise InvalidAIResponse("response is not valid JSON") from None
    if not isinstance(raw, dict):
        raise InvalidAIResponse("response is not a JSON object")
    try:
        return StrictConditionAssessment.model_validate(raw)
    except pydantic.ValidationError as exc:
        problems = sorted({".".join(str(p) for p in e["loc"]) or "response" for e in exc.errors()})
        raise InvalidAIResponse("invalid fields: " + ", ".join(problems[:10])) from None


# --------------------------------------------------------------------------- policy
POLICY_VERSION = "condition-policy-v2"  # v1 = Phase 5 structural rules; v2 adds Phase 8.1 text checks


class PolicyOutcome(BaseModel):
    classification: Classification
    adjustments: list[str]
    human_review_required: bool
    review_reasons: list[str]
    confidence: Optional[Level] = None  # effective confidence after downgrade-only caps
    rules_triggered: list[str] = []
    policy_version: str = POLICY_VERSION


def apply_policy(a: StrictConditionAssessment) -> PolicyOutcome:
    """Server-side rules applied after validation. Downgrades never upgrade."""
    reasons: list[str] = []
    c = a.classification
    if c is Classification.DETERIORATED and not a.deterioration_evidence:
        reasons.append("DETERIORATED was returned without any deterioration evidence")
    if c is Classification.AMELIORATED and not a.improvement_evidence:
        reasons.append("AMELIORATED was returned without any improvement evidence")
    if c is Classification.STABLE and (a.deterioration_evidence or a.improvement_evidence):
        reasons.append("STABLE was returned together with change evidence")
    if a.same_area in (SameArea.DIFFERENT_AREA, SameArea.UNCERTAIN):
        reasons.append(f"the photographs may not show the same area ({a.same_area.value})")
    if a.confidence is Level.LOW:
        reasons.append("classification confidence is LOW")
    if a.comparison_reliability is Level.LOW:
        reasons.append("comparison reliability is LOW")
    for side, q in (("earlier", a.earlier_image_quality), ("later", a.later_image_quality)):
        if q is ImageQuality.POOR:
            reasons.append(f"the {side} photograph quality is POOR")

    from .text_rules import evaluate  # local import: text_rules imports this module

    findings = evaluate(a)
    reasons += [f.message for f in findings if f.action == "downgrade"]

    final = c
    adjustments: list[str] = []
    if reasons and c is not Classification.NEEDS_REVIEW:
        final = Classification.NEEDS_REVIEW
        adjustments = [f"Changed from {c.value} to NEEDS_REVIEW: {r}." for r in reasons]
    confidence = a.confidence
    caps = [f.message for f in findings if f.action == "confidence"]
    if caps and confidence is Level.HIGH:
        confidence = Level.MEDIUM
        adjustments += [f"Confidence lowered from HIGH to MEDIUM: {m}." for m in caps]

    review_reasons = list(reasons) + caps + [f.message for f in findings if f.action == "review"]
    if final is Classification.NEEDS_REVIEW and not reasons:
        review_reasons.append("the AI could not establish a reliable comparison")
    if final is Classification.DETERIORATED:
        review_reasons.append("deterioration findings always require confirmation")
    if a.confidence is not Level.HIGH and "classification confidence is LOW" not in reasons:
        review_reasons.append(f"classification confidence is {a.confidence.value}")
    if a.comparison_reliability is Level.MEDIUM:
        review_reasons.append("comparison reliability is MEDIUM")
    if a.same_area is SameArea.PARTIAL_OVERLAP:
        review_reasons.append("the photographs only partially overlap")
    if a.deterioration_evidence and a.improvement_evidence:
        review_reasons.append("both deterioration and improvement evidence were reported")

    return PolicyOutcome(
        classification=final,
        adjustments=adjustments,
        human_review_required=bool(review_reasons),
        review_reasons=review_reasons,
        confidence=confidence,
        rules_triggered=[f.rule for f in findings],
    )
