"""Live Groq provider for AI Condition Analysis -- DISABLED unless explicitly approved.

Replaces the former Gemini adapter (user decision, 2026-11-20). It reuses the project's existing Groq setup:

* Key: ``GROQ_API_KEY`` -- the key the automation's ``GroqVisionClient`` already uses. It is loaded
  through the automation's own loader (``automation/src/config.load_provider_environment``: reads
  ``automation/.env``; variables already in the environment win), only once live use is approved.
  No second key configuration is introduced. The key is never logged, returned or put in an error.
* Transport: plain HTTPS to Groq's OpenAI-compatible chat-completions endpoint with ``httpx``, like the
  dashboard's existing Location AI Groq adapter (no extra SDK needed).
* Model: ``qwen/qwen3.8-27b`` -- the project's existing Groq vision model. Groq's vision documentation
  lists it as image-capable with up to 3 images per request (one pair = 2) and "JSON Object Mode" together
  with images. Only Groq's documented vision models are accepted (see app.config.GROQ_VISION_MODELS).

Safety gates (both required before any request can be made):
  1. SYNERGY_AI_PROVIDER=groq
  2. SYNERGY_AI_ALLOW_LIVE=1   (set only after the user explicitly approves live analysis)

The adapter only transports: it returns the model's JSON text. Strict validation, policy, mandatory human
review, de-duplication, storage and audit logging all stay in the service and are unchanged.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from .prompts import EARLIER_LABEL, LATER_LABEL
from .providers import (
    ConditionProvider,
    ImageInput,
    ProviderError,
    ProviderRateLimited,
    ProviderRejected,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)
from .schema import ConditionAssessment

log = logging.getLogger(__name__)

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
KEY_VAR = "GROQ_API_KEY"
DEFAULT_MODEL = "qwen/qwen3.8-27b"
# Per-model request options. Qwen: "none" = instruct mode (no hidden reasoning), which keeps the answer to the
# JSON object only. Models not listed get no extra options.
MODEL_OPTIONS = {"qwen/qwen3.8-27b": {"reasoning_effort": "none"}}
# Originals are ~850x637 px and are sent unchanged; anything larger is downscaled to bound request size
# (Groq's limit for a request with images is 20 MB).
DEFAULT_MAX_DIMENSION = 1024
TIMEOUT = httpx.Timeout(60.0, connect=5.0)
MAX_ATTEMPTS = 3            # 1 request + up to 2 retries, for HTTP 5xx only (never for 429 or 4xx)
BACKOFF_SECONDS = (2.0, 4.0)
MIN_GAP_SECONDS = 2.0       # client-side pacing between request starts (retries included)
MAX_OUTPUT_TOKENS = 4096
TEMPERATURE = 0.2

JSON_INSTRUCTIONS = (
    "\nOutput format: respond with ONE JSON object only -- no markdown fences, no text before or after it. "
    "It must conform exactly to this JSON Schema (all listed fields, no additional fields):\n"
)


def _schema_text() -> str:
    return json.dumps(ConditionAssessment.model_json_schema(), separators=(",", ":"))


def _load_automation_environment(automation_root: Path) -> None:
    """Run the automation's own key loader (reads automation/.env; existing variables win). Writes nothing."""
    root = str(Path(automation_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # never write __pycache__ into automation/
    try:
        cfg = importlib.import_module("src.config")
    finally:
        sys.dont_write_bytecode = previous
    cfg.load_provider_environment()


def _clean_key(raw: Optional[str]) -> Optional[str]:
    """The usable key, or None. Strips whitespace / BOM / quotes and checks the Groq key shape. Never logged."""
    if not raw:
        return None
    key = raw.replace("﻿", "").strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
    return key if key.startswith("gsk_") and len(key) >= 20 and not any(c.isspace() for c in key) else None


def _downscale(data: bytes, media_type: str, max_dim: int) -> tuple[bytes, str]:
    """Return the image unchanged when it is within max_dim; otherwise a JPEG copy (the original is never touched)."""
    if not max_dim:
        return data, media_type
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) <= max_dim:
                return data, media_type
            im = im.convert("RGB")
            im.thumbnail((max_dim, max_dim))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=90)
            return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - unreadable here means: send as-is; the pair check already verified it
        return data, media_type


class GroqConditionProvider(ConditionProvider):
    name = "groq"
    live = True
    simulated = False

    def __init__(self, automation_root: Path, *, allow_live: bool, model: Optional[str] = None,
                 transport: Optional[httpx.BaseTransport] = None, sleep: Callable[[float], None] = time.sleep,
                 max_image_dimension: int = DEFAULT_MAX_DIMENSION, env_loader: Optional[Callable[[], None]] = None):
        self.automation_root = Path(automation_root)
        self.allow_live = allow_live
        self.model = model or DEFAULT_MODEL
        self.max_image_dimension = max_image_dimension
        self._transport = transport
        self._sleep = sleep
        self._env_loader = env_loader or (lambda: _load_automation_environment(self.automation_root))
        self._lock = threading.Lock()
        self._last_start = 0.0

    # ------------------------------------------------------------------ configuration
    def _key(self) -> Optional[str]:
        """The configured key (never returned to callers outside this class). Loads automation/.env only when
        live use is approved and the key is not already in the environment."""
        if not self.allow_live:
            return None
        key = _clean_key(os.environ.get(KEY_VAR))
        if key is None:
            try:
                self._env_loader()
            except Exception:  # noqa: BLE001 - reported as "not configured"; details may contain paths
                log.warning("The automation key loader could not be run.")
            key = _clean_key(os.environ.get(KEY_VAR))
        return key

    def available(self) -> tuple[bool, str]:
        if not self.allow_live:
            return False, "Live Groq analysis is awaiting approval (SYNERGY_AI_ALLOW_LIVE is not set)."
        if self._key() is None:
            return False, f"Groq is selected but its API key is not configured ({KEY_VAR})."
        return True, f"Groq live analysis enabled ({self.model})."

    # ------------------------------------------------------------------ request
    def _payload(self, earlier: ImageInput, later: ImageInput, prompt: str) -> dict:
        def image_part(img: ImageInput) -> dict:
            data, media = _downscale(img.data, img.media_type, self.max_image_dimension)
            return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{base64.b64encode(data).decode('ascii')}"}}

        content = [
            {"type": "text", "text": EARLIER_LABEL.format(date=earlier.date)},
            image_part(earlier),
            {"type": "text", "text": LATER_LABEL.format(date=later.date)},
            image_part(later),
            {"type": "text", "text": prompt + JSON_INSTRUCTIONS + _schema_text()},
        ]
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
            "temperature": TEMPERATURE,
            "max_completion_tokens": MAX_OUTPUT_TOKENS,
            "stream": False,
            **MODEL_OPTIONS.get(self.model, {}),
        }

    def _pace(self) -> None:
        wait = self._last_start + MIN_GAP_SECONDS - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_start = time.monotonic()

    def analyse(self, earlier: ImageInput, later: ImageInput, prompt: str) -> ProviderResult:
        ok, reason = self.available()
        if not ok:
            raise ProviderUnavailable(reason)
        key = self._key()
        payload = self._payload(earlier, later, prompt)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        t0 = time.time()
        with self._lock, httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
            response = None
            for attempt in range(MAX_ATTEMPTS):
                self._pace()
                try:
                    response = client.post(ENDPOINT, headers=headers, json=payload)
                except httpx.TimeoutException:
                    raise ProviderTimeout() from None
                except httpx.HTTPError:
                    raise ProviderUnavailable() from None
                if response.status_code < 500:
                    break
                log.warning("Groq returned HTTP %s (attempt %s of %s).", response.status_code, attempt + 1, MAX_ATTEMPTS)
                if attempt + 1 < MAX_ATTEMPTS:
                    self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
            status = response.status_code
            if status >= 500:
                raise ProviderUnavailable()
            if status == 429:
                raise ProviderRateLimited(retry_after_seconds=_retry_after(response))
            if status in (401, 403, 404):
                log.warning("Groq refused the request (HTTP %s): check the key and that the model is available.", status)
                raise ProviderRejected()
            if status != 200:
                log.warning("Groq returned HTTP %s.", status)
                raise ProviderError()
            try:
                body = response.json()
                raw = body["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise ProviderError() from None
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return ProviderResult(
            raw=raw,
            usage={
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
            latency_seconds=round(time.time() - t0, 3),
        )


def _retry_after(response: httpx.Response) -> Optional[float]:
    value = response.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None
