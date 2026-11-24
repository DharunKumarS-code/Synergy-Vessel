"""Layer 3: AI suggestion providers and response validation.

Configuration is dedicated to this feature and independent of the condition-analysis AI:

* ``SYNERGY_LOCATION_AI_PROVIDER``   -- disabled (default) | mock | <live provider name>
* ``SYNERGY_LOCATION_AI_MODEL``      -- model for a live provider
* ``SYNERGY_LOCATION_AI_API_KEY``    -- the dedicated key; read from the process environment only when needed,
  never stored in settings, logged, audited, returned by an endpoint or sent to the frontend
* ``SYNERGY_LOCATION_AI_ALLOW_LIVE`` -- "1" permits live calls

A live provider is used only when it is selected AND the dedicated key is present AND live calls are allowed.
Live adapters: Groq (Phase 9.1, app/location_intel/groq.py) and Gemini (app/location_intel/gemini.py). Gemini uses the
project's existing Gemini key (GEMINI_API_KEY in automation/.env, via the automation loader) instead of the dedicated
key, and only after live calls are allowed. Any other live name reports itself as unavailable and the import falls
back to deterministic rules plus human review.
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .rules import KnownLocations, _numbers, key

KEY_ENV = "SYNERGY_LOCATION_AI_API_KEY"
LIVE_ADAPTERS: dict[str, Any] = {}          # name -> factory(settings, key); Groq registered below (Phase 9.1)
MAX_REASON = 300


def dedicated_key_configured() -> bool:
    """Whether the dedicated key is present. Only ever exposes this boolean."""
    return bool(os.environ.get(KEY_ENV, "").strip())


@dataclass
class LocationQuery:
    original: str
    worksheet: Optional[str] = None
    workbook: Optional[str] = None
    image_filename: Optional[str] = None
    source_cell: Optional[str] = None
    rule_candidates: tuple[str, ...] = ()


@dataclass
class Suggestion:
    location: Optional[str]                  # an EXISTING location (canonical spelling) or None = unknown
    confidence: float
    reason: str
    alternatives: tuple[str, ...] = ()       # other EXISTING locations the AI found equally plausible (= ambiguous)


class InvalidResponse(ValueError):
    pass


class ProviderUnavailable(RuntimeError):
    pass


class LocationProvider(Protocol):
    name: str
    simulated: bool
    available: bool
    message: str

    def suggest(self, queries: list[LocationQuery], known: list[str], model: Optional[str]) -> Any:
        """Return a JSON string or dict: {"suggestions": [{"original", "location", "confidence", "reason"}]}."""


def validate_response(raw: Any, queries: list[LocationQuery], known: KnownLocations) -> tuple[dict[str, Suggestion], list[str]]:
    """Strictly validate a provider response. Returns ({query key: Suggestion}, problems). Raises InvalidResponse
    when the response as a whole is unusable. Items naming a location the dataset does not use are discarded:
    AI may only map to existing locations, never invent one."""
    if isinstance(raw, (bytes, str)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise InvalidResponse("the response is not valid JSON") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("suggestions"), list):
        raise InvalidResponse("the response has no 'suggestions' list")
    wanted = {key(q.original): q for q in queries}
    out: dict[str, Suggestion] = {}
    problems: list[str] = []
    for item in raw["suggestions"]:
        if not isinstance(item, dict) or not isinstance(item.get("original"), str):
            problems.append("an item without an 'original' text was ignored")
            continue
        k = key(item["original"])
        if k not in wanted or k in out:
            problems.append(f"an item for an unrequested or repeated location '{item['original'][:60]}' was ignored")
            continue
        conf = item.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            problems.append(f"'{item['original'][:60]}': confidence missing or outside 0-1; ignored")
            continue
        loc = item.get("location")
        if loc is not None and not isinstance(loc, str):
            problems.append(f"'{item['original'][:60]}': location is not text; ignored")
            continue
        resolved = known.resolve(loc) if loc and loc.strip() else None
        if loc and loc.strip() and resolved is None:
            problems.append(f"'{item['original'][:60]}': suggested '{loc[:60]}' is not an existing location; ignored")
            continue
        # Phase 9.4: number agreement, as in the rules. A numbered location (Cargo Hold 3) is accepted only when the
        # original text carries exactly the same number(s): never a different hold, never a guessed number.
        if resolved is not None and _numbers(key(resolved)) and _numbers(key(resolved)) != _numbers(k):
            problems.append(f"'{item['original'][:60]}': suggested '{resolved[:60]}' does not match the number(s) in the "
                            "text; ignored")
            continue
        reason = " ".join(str(item.get("reason") or "").split())[:MAX_REASON]
        out[k] = Suggestion(resolved, round(float(conf), 2), reason, _alternatives(item.get("alternatives"), resolved, k, known, problems))
    return out, problems


def _alternatives(raw: Any, chosen: Optional[str], k: str, known: KnownLocations, problems: list[str]) -> tuple[str, ...]:
    """Validated ambiguity alternatives: existing locations only, same number rule, never the chosen one, at most 3."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("an 'alternatives' value that is not a list was ignored")
        return ()
    out: list[str] = []
    for alt in raw:
        resolved = known.resolve(alt) if isinstance(alt, str) and alt.strip() else None
        if resolved is None or (_numbers(key(resolved)) and _numbers(key(resolved)) != _numbers(k)):
            problems.append(f"an alternative '{str(alt)[:60]}' is not a matching existing location; ignored")
            continue
        if resolved != chosen and resolved not in out:
            out.append(resolved)
    return tuple(out[:3])


class DisabledProvider:
    simulated = False
    available = False

    def __init__(self, name: str, message: str):
        self.name, self.message = name, message

    def suggest(self, queries, known, model):  # pragma: no cover - never called
        raise ProviderUnavailable(self.message)


class MockLocationProvider:
    """Deterministic, offline stand-in for development and tests. ``responder`` may be given to script
    responses (dict, JSON text, or an exception to raise)."""

    name, simulated, available = "mock", True, True
    message = "Simulated location AI (mock). No external service is called."

    def __init__(self, responder=None):
        self.responder = responder
        self.calls: list[list[LocationQuery]] = []

    def suggest(self, queries, known, model):
        self.calls.append(list(queries))
        if self.responder is not None:
            res = self.responder(queries, known) if callable(self.responder) else self.responder
            if isinstance(res, BaseException):
                raise res
            return res
        items = []
        for q in queries:
            pool = list(q.rule_candidates) or known
            scored = sorted(((difflib.SequenceMatcher(None, key(q.original), key(n)).ratio(), n) for n in pool), reverse=True)
            if scored and scored[0][0] >= 0.5:
                items.append({"original": q.original, "location": scored[0][1], "confidence": round(min(0.85, scored[0][0] * 0.9), 2),
                              "reason": "Simulated: closest existing location by wording."})
            else:
                items.append({"original": q.original, "location": None, "confidence": 0.2,
                              "reason": "Simulated: no existing location fits this description."})
        return json.dumps({"suggestions": items})


def build_provider(settings) -> LocationProvider:
    name = (settings.location_ai_provider or "disabled").strip().lower()
    if name == "disabled":
        return DisabledProvider("disabled", "Location AI is disabled; locations are standardised by rules and human review.")
    if name == "mock":
        return MockLocationProvider()
    if name == "gemini":                       # own key (GEMINI_API_KEY), loaded only once live calls are allowed
        if not settings.location_ai_allow_live:
            return DisabledProvider(name, "Live location AI calls are not enabled (SYNERGY_LOCATION_AI_ALLOW_LIVE).")
        return LIVE_ADAPTERS["gemini"](settings)
    if not dedicated_key_configured():
        return DisabledProvider(name, f"Location AI provider '{name}' is selected but {KEY_ENV} is not configured.")
    if not settings.location_ai_allow_live:
        return DisabledProvider(name, "Live location AI calls are not enabled (SYNERGY_LOCATION_AI_ALLOW_LIVE).")
    factory = LIVE_ADAPTERS.get(name)
    if factory is None:
        return DisabledProvider(name, f"No live adapter for location AI provider '{name}' exists in this version.")
    return factory(settings, os.environ[KEY_ENV])


def _register_live_adapters() -> None:
    from .gemini import factory as gemini_factory
    from .groq import factory as groq_factory   # Phase 9.1

    LIVE_ADAPTERS.setdefault("groq", groq_factory)
    LIVE_ADAPTERS.setdefault("gemini", gemini_factory)


_register_live_adapters()
