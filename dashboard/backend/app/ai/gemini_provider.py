"""Live Gemini provider -- reuses the existing automation client; DISABLED unless approved.

Reuse, not reimplementation: ``_PairClient`` subclasses
``automation/src/vision_client.GeminiVisionClient`` so the existing behaviour applies unchanged:
5xx retry with jittered backoff, request pacing (settings.yaml ``rate_limit``), daily-quota /
per-minute / ambiguous 429 detection (never retried blindly), client timeout, API-key redaction
in errors, and token-usage capture. Only the request body differs: two labelled photographs plus
the condition prompt, with ``ConditionAssessment`` as the response schema.

Safety gates (both required before any request can be made):
  1. SYNERGY_AI_PROVIDER=gemini
  2. SYNERGY_AI_ALLOW_LIVE=1   (set only after the user explicitly approves a live run)
The key is read from automation/.env via the automation's own loader, lazily on the first
analysis -- never at import or start-up, never logged, never returned by any endpoint.
Nothing in automation/ is modified.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from pathlib import Path

from .providers import (
    ConditionProvider,
    ImageInput,
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRejected,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
)
from .schema import ConditionAssessment

# Originals are ~850x637 px; a 1024 px cap leaves them byte-for-byte untouched (the automation's
# resize helper skips re-encoding when the image is already small enough) while still bounding
# any unexpectedly large future image. Condition detail matters more here than for location
# classification, which uses 768 px.
DEFAULT_MAX_DIMENSION = 1024


def _automation_modules(automation_root: Path):
    root = str(automation_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    # Never write __pycache__ into automation/ (it must stay byte-for-byte unchanged).
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        vc = importlib.import_module("src.vision_client")
        cfg = importlib.import_module("src.config")
    finally:
        sys.dont_write_bytecode = previous
    return vc, cfg


def _build_pair_client_class(vc):
    from tenacity import retry, retry_if_exception, stop_after_attempt

    class _PairClient(vc.GeminiVisionClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            from google.genai import types

            self._generate_config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ConditionAssessment,
                thinking_config=self._generate_config.thinking_config,
            )

        # Same class-level defaults as the parent; __init__ (parent) rebinds this per instance
        # with the configured RetryPolicy and the parent's 429-aware retry predicate.
        @retry(
            stop=stop_after_attempt(vc.RetryPolicy().max_attempts),
            wait=vc.RetryPolicy().wait_strategy(),
            retry=retry_if_exception(vc._is_retryable),
            reraise=True,
        )
        def _call(self, payload):
            from google.genai import types

            earlier, later, prompt, labels = payload
            e_bytes, l_bytes = earlier.data, later.data
            if self._max_image_dimension:
                e_bytes = vc.resize_for_classification(e_bytes, self._max_image_dimension)
                l_bytes = vc.resize_for_classification(l_bytes, self._max_image_dimension)
            self._pace()
            return self._client.models.generate_content(
                model=self._model,
                contents=[
                    labels[0],
                    types.Part.from_bytes(data=e_bytes, mime_type=earlier.media_type),
                    labels[1],
                    types.Part.from_bytes(data=l_bytes, mime_type=later.media_type),
                    prompt,
                ],
                config=self._generate_config,
            )

    return _PairClient


class GeminiConditionProvider(ConditionProvider):
    name = "gemini"
    live = True

    def __init__(self, automation_root: Path, *, allow_live: bool, model: str | None = None,
                 thinking_level: str | None = None, max_image_dimension: int = DEFAULT_MAX_DIMENSION):
        self.automation_root = automation_root
        self.allow_live = allow_live
        self.thinking_level = thinking_level
        self.max_image_dimension = max_image_dimension
        self._client = None
        self._lock = threading.Lock()
        self._settings_error: str | None = None
        self._settings: dict = {}
        try:
            _, cfg = _automation_modules(automation_root)
            self._settings = cfg.load_settings() or {}  # settings.yaml only -- does not touch .env
        except Exception:  # noqa: BLE001 - reported via status, never raised at start-up
            self._settings_error = "automation settings could not be read"
        self.model = model or self._settings.get("model") or "unknown"

    def available(self) -> tuple[bool, str]:
        if self._settings_error:
            return False, "Gemini is configured but its settings could not be read."
        if (self._settings.get("provider") or "gemini") != "gemini":
            return False, "The automation settings select a provider other than Gemini."
        if not self.allow_live:
            return False, "Live Gemini analysis is awaiting approval (SYNERGY_AI_ALLOW_LIVE is not set)."
        return True, f"Gemini live analysis enabled ({self.model})."

    def _get_client(self, vc, cfg):
        with self._lock:
            if self._client is None:
                cfg.load_provider_environment()  # reads automation/.env into os.environ; never echoed
                rate_limit = self._settings.get("rate_limit")
                policy = vc.RetryPolicy.from_settings(rate_limit)
                rpm = vc.requests_per_minute_from_settings(rate_limit)
                self._client = _build_pair_client_class(vc)(
                    model=self.model,
                    thinking_level=self.thinking_level,
                    max_image_dimension=self.max_image_dimension,
                    retry_policy=policy,
                    requests_per_minute=rpm,
                )
            return self._client

    def analyse(self, earlier: ImageInput, later: ImageInput, prompt: str) -> ProviderResult:
        ok, reason = self.available()
        if not ok:
            raise ProviderUnavailable(reason)
        from .prompts import EARLIER_LABEL, LATER_LABEL

        vc, cfg = _automation_modules(self.automation_root)
        try:
            client = self._get_client(vc, cfg)
        except vc.VisionAPIError:
            raise ProviderUnavailable("The Gemini client could not be configured.") from None
        labels = (EARLIER_LABEL.format(date=earlier.date), LATER_LABEL.format(date=later.date))
        t0 = time.time()
        try:
            response = client._call_and_handle_errors((earlier, later, prompt, labels))
        except vc.GeminiQuotaExhaustedError as exc:
            raise ProviderQuotaExhausted(retry_after_seconds=exc.retry_delay_seconds) from None
        except vc.ProviderRateLimitedError as exc:
            raise ProviderRateLimited(retry_after_seconds=exc.retry_delay_seconds) from None
        except vc.ProviderServiceUnavailableError:
            raise ProviderUnavailable() from None
        except vc.ProviderRequestTimeoutError:
            raise ProviderTimeout() from None
        except vc.ProviderPermanentError:
            raise ProviderRejected() from None
        except Exception:  # noqa: BLE001 - redacted by the parent; details never reach the client
            raise ProviderError() from None
        latency = time.time() - t0
        raw = response.parsed if response.parsed is not None else response.text
        usage = client._usage_info(response, latency)
        return ProviderResult(
            raw=raw,
            usage={
                "input_text_tokens": usage.input_text_tokens,
                "input_image_tokens": usage.input_image_tokens,
                "output_tokens": usage.output_tokens,
                "thoughts_tokens": usage.thoughts_tokens,
                "total_tokens": usage.total_tokens,
            },
            latency_seconds=round(latency, 2),
        )
