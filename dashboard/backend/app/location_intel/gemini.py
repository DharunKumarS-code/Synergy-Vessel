"""Gemini live adapter for Excel Location Intelligence (added 2026-11-20, alongside the Groq adapter).

Independent of AI Condition Analysis (which uses Groq): it is selected only by SYNERGY_LOCATION_AI_PROVIDER=gemini and
never changes SYNERGY_AI_* settings or the condition provider.

Reuses the project's existing Gemini configuration:
* Key: ``GEMINI_API_KEY`` (``GOOGLE_API_KEY`` accepted as a fallback), the names the automation's Gemini client uses,
  loaded through the automation's own loader (``automation/src/config.load_provider_environment``: reads
  ``automation/.env``; variables already in the environment win). The loader is run ONLY after live location AI is
  approved (SYNERGY_LOCATION_AI_ALLOW_LIVE=1). The key is sent in the ``x-goog-api-key`` header (never in the URL)
  and is never logged, returned, audited or put in an error message.
* Model: ``SYNERGY_LOCATION_AI_MODEL`` when it names a Gemini model, else the automation's model (``gemini-3.8-flash``).

Only the location-identification context is sent: the location text, workbook / worksheet names, source cell, the
rule-based candidates and the list of existing Official Locations. No photographs, dates or other inspection data.

The adapter only transports: it returns the model's JSON text, which the service validates exactly as for every other
provider (existing locations only, number agreement, confidence 0-1, short reason). Every answer stays a suggestion
that a person must review; nothing is applied automatically.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, Optional

import httpx

from .groq import SYSTEM_PROMPT as BASE_PROMPT

log = logging.getLogger(__name__)
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
DEFAULT_MODEL = "gemini-3.8-flash"          # the automation pipeline's configured Gemini model (config/settings.yaml)
MODEL_RE = re.compile(r"gemini-[a-z0-9][a-z0-9.\-]{0,60}")
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")   # Google API key shape (39 characters)
MAX_ITEMS_PER_REQUEST = 40
MAX_OUTPUT_TOKENS = 4096
MAX_ALTERNATIVES = 3

SYSTEM_PROMPT = BASE_PROMPT + (
    " Ambiguity: when the original text plausibly refers to MORE THAN ONE location in 'known_locations', set "
    "'location' to null and list those locations (copied exactly, at most 3) in 'alternatives'; otherwise give an "
    "empty 'alternatives' list. Each entry may therefore also contain \"alternatives\": [\"<known location>\", ...]."
)

# Gemini structured output (OpenAPI subset). The service still validates every field.
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "suggestions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "original": {"type": "STRING"},
                    "location": {"type": "STRING", "nullable": True},
                    "confidence": {"type": "NUMBER"},
                    "reason": {"type": "STRING"},
                    "alternatives": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["original", "location", "confidence", "reason"],
            },
        },
    },
    "required": ["suggestions"],
}


class GeminiError(RuntimeError):
    """A request failed. The message never contains the key, headers or response body."""


def load_automation_environment(automation_root: Path) -> None:
    """Run the automation's own key loader (reads automation/.env; existing variables win). Writes nothing."""
    root = str(Path(automation_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # never write __pycache__ into automation/
    try:
        cfg = importlib.import_module("src.config")
    finally:
        sys.dont_write_bytecode = previous
    cfg.load_provider_environment()


def clean_key(raw: Optional[str]) -> Optional[str]:
    """The usable key or None (missing, empty placeholder, quotes/BOM removed, wrong shape). Never logged."""
    if not raw:
        return None
    key = raw.replace("﻿", "").strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
    return key if KEY_RE.fullmatch(key) else None


def find_key(loader: Callable[[], None]) -> Optional[str]:
    """GEMINI_API_KEY / GOOGLE_API_KEY from the environment, running the automation loader if neither is usable."""
    def current():
        return next((k for k in (clean_key(os.environ.get(v)) for v in KEY_VARS) if k), None)

    key = current()
    if key is None:
        try:
            loader()
        except Exception:  # noqa: BLE001 - reported as "not configured"; details may contain paths
            log.warning("The automation key loader could not be run.")
        key = current()
    return key


def resolve_model(configured: Optional[str]) -> str:
    m = (configured or "").strip().lower()
    return m if MODEL_RE.fullmatch(m) else DEFAULT_MODEL


def build_request(queries, known: list[str]) -> dict:
    items = [{"original": q.original, "workbook": q.workbook, "worksheet": q.worksheet, "source_cell": q.source_cell,
              "candidates": list(q.rule_candidates)} for q in queries]
    user = json.dumps({"known_locations": list(known), "items": items}, ensure_ascii=False)
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": MAX_OUTPUT_TOKENS,
                             "responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA},
    }


class GeminiLocationProvider:
    name, simulated, available = "gemini", False, True
    key_configured = True

    def __init__(self, model: str, api_key: str, timeout_s: float, client: Optional[httpx.Client] = None):
        self.model = model
        self.__key = api_key                       # name-mangled; never part of repr/status/logs
        self.timeout_s = float(timeout_s)
        self._client = client
        self.message = f"Live location AI: Gemini ({model})."
        self.requests_made = 0

    def __repr__(self) -> str:
        return f"GeminiLocationProvider(model={self.model!r}, key=<hidden>)"

    def _post(self, payload: dict) -> dict:
        headers = {"x-goog-api-key": self.__key, "Content-Type": "application/json"}
        client = self._client or httpx.Client(timeout=self.timeout_s)
        try:
            self.requests_made += 1
            resp = client.post(ENDPOINT.format(model=self.model), json=payload, headers=headers, timeout=self.timeout_s)
        except httpx.TimeoutException:
            raise GeminiError("the Gemini request timed out") from None
        except httpx.HTTPError as exc:
            raise GeminiError(f"the Gemini request failed ({type(exc).__name__})") from None
        finally:
            if self._client is None:
                client.close()
        if resp.status_code != 200:
            log.warning("location AI (Gemini) returned HTTP %s", resp.status_code)
            raise GeminiError(f"Gemini returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise GeminiError("Gemini returned a non-JSON body") from None

    def suggest(self, queries, known, model=None) -> str:
        """Return {"suggestions": [...]} as JSON text (validated by the service)."""
        combined: list = []
        for start in range(0, len(queries), MAX_ITEMS_PER_REQUEST):
            body = self._post(build_request(queries[start:start + MAX_ITEMS_PER_REQUEST], known))
            try:
                candidate = body["candidates"][0]
                text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if isinstance(p, dict))
            except (KeyError, IndexError, TypeError, AttributeError):
                raise GeminiError("Gemini returned no usable answer (blocked or empty)") from None
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                return text                                          # validation reports "not valid JSON"
            if not isinstance(parsed, dict) or not isinstance(parsed.get("suggestions"), list):
                return json.dumps(parsed)                            # validation reports the missing list
            combined.extend(parsed["suggestions"])
        return json.dumps({"suggestions": combined})


def factory(settings, loader: Optional[Callable[[], None]] = None):
    """Build the Gemini provider, or a disabled provider explaining what is missing. Never makes a request.
    Called only after live location AI has been approved (see providers.build_provider)."""
    from .providers import DisabledProvider

    loader = loader or (lambda: load_automation_environment(settings.automation_root))
    model = resolve_model(settings.location_ai_model)
    key = find_key(loader)
    if key is None:
        p = DisabledProvider("gemini", "Gemini location AI is not available: GEMINI_API_KEY is not configured "
                                       "(missing, empty or not a Google API key) in automation/.env.")
        p.model, p.key_configured = model, False
        return p
    return GeminiLocationProvider(model, key, settings.location_ai_timeout_s)
