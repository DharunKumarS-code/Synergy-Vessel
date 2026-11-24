"""Dashboard backend settings.

Inspection data is read-only. The backend never loads ``.env`` itself; the only
exception is the opt-in live condition-analysis provider (Gemini, or Groq), which uses the automation's
own key loader (``GEMINI_API_KEY`` / ``GROQ_API_KEY``) and only when ``SYNERGY_AI_PROVIDER=gemini|groq`` *and*
``SYNERGY_AI_ALLOW_LIVE=1`` are set. Environment overrides:

* ``SYNERGY_DATA_ROOT``    -- data root (tests point it at synthetic fixtures)
* ``SYNERGY_ANALYSIS_DIR`` -- condition-analysis store (default dashboard/data/condition_analyses)
* ``SYNERGY_IMPORT_DIR``   -- Excel import store (default dashboard/data/imports)
* ``SYNERGY_AUTH_DIR``     -- accounts + audit log (default dashboard/data/auth)
* ``SYNERGY_BACKUP_DIR``   -- backups (default "<project parent>/Synergy Vessel Backups", outside the project)
* ``SYNERGY_AI_PROVIDER``  -- disabled (default) | mock | gemini | groq  (Gemini restored 2026-11-23 as the intended provider)
* ``SYNERGY_AI_ALLOW_LIVE``-- "1" permits live Gemini/Groq requests (requires user approval)
* ``SYNERGY_LOCATION_AI_PROVIDER`` / ``_MODEL`` / ``_ALLOW_LIVE`` -- Phase 9 Excel Location Intelligence, fully
  separate from the condition-analysis AI. Its dedicated key (``SYNERGY_LOCATION_AI_API_KEY``) is never read
  into these settings; see app/location_intel/providers.py.
* ``SYNERGY_AI_MODEL``     -- optional model override for the selected provider. Gemini: a text-output
  ``gemini-*`` model id (image-generation models refused); absent/invalid -> the automation's settings.yaml model.
  Groq: only documented image-capable models (GROQ_VISION_MODELS); anything else -> qwen/qwen3.8-27b.
  automation/config/settings.yaml and the automation pipeline are never changed by this setting.

Infrastructure (2026-11-23; see dashboard/INFRASTRUCTURE.md):

* ``SYNERGY_RATE_LIMIT_ENABLED``  -- "0" turns API rate limiting off (default on)
* ``SYNERGY_RATE_LIMIT_<CATEGORY>`` -- requests per minute per client for LOGIN, AI_ANALYSIS, REPORTS, IMPORTS,
  WRITES, IMAGES, READS (0 = unlimited for that category); defaults in app/infra/rate_limit.py
* ``SYNERGY_TRUSTED_PROXIES``     -- comma-separated proxy addresses whose X-Forwarded-For is trusted (e.g. 127.0.0.1
  when running behind the nginx reverse proxy); empty by default
* ``SYNERGY_CORS_ORIGINS``        -- comma-separated browser origins allowed to call the API directly
* ``SYNERGY_CACHE_TTL_SECONDS``   -- lifetime of cached read-only summaries (default 300; 0 disables the cache)
* ``SYNERGY_AI_BREAKER_THRESHOLD`` / ``SYNERGY_AI_BREAKER_RECOVERY_SECONDS`` -- consecutive AI provider failures
  before analysis is paused (default 3), and the pause before one trial request (default 60)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# dashboard/backend/app/config.py -> parents[3] == project root
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ANALYSIS_DIR = DEFAULT_DATA_ROOT / "dashboard" / "data" / "condition_analyses"
DEFAULT_IMPORT_DIR = DEFAULT_DATA_ROOT / "dashboard" / "data" / "imports"
DEFAULT_AUTH_DIR = DEFAULT_DATA_ROOT / "dashboard" / "data" / "auth"
# Next to the project, never inside it, so a backup can never include itself.
DEFAULT_BACKUP_DIR = DEFAULT_DATA_ROOT.parent / "Synergy Vessel Backups"
AI_PROVIDERS = ("disabled", "mock", "gemini", "groq")
# Text-output Gemini model ids only (e.g. gemini-3.6-flash); image-generation models are refused.
GEMINI_MODEL_RE = re.compile(r"^gemini-[a-z0-9][a-z0-9.-]{0,62}$")
# Condition analysis sends two photographs, so only models Groq documents as image-capable are allowed
# (console.groq.com/docs/vision, checked 2026-11-20; qwen: up to 3 images/request, JSON mode with images).
# A text-only model is never assumed to support vision.
GROQ_VISION_MODELS = ("qwen/qwen3.8-27b", "meta-models/muse-spark-open")
log = logging.getLogger(__name__)


def parse_ai_model(value: str | None, provider: str = "groq") -> str | None:
    """Validated SYNERGY_AI_MODEL value for ``provider``, or None to keep that provider's default model."""
    model = (value or "").strip().lower()
    if not model:
        return None
    if provider == "gemini":
        if not GEMINI_MODEL_RE.match(model) or "image" in model:
            log.warning("Ignoring invalid SYNERGY_AI_MODEL; using the configured default Gemini model.")
            return None
        return model
    if model not in GROQ_VISION_MODELS:
        log.warning("Ignoring SYNERGY_AI_MODEL: not a supported Groq vision model; using the default model.")
        return None
    return model


@dataclass(frozen=True)
class Settings:
    data_root: Path = DEFAULT_DATA_ROOT
    report_filename: str = "Final Official vs AI Location Report.json"
    image_dir_name: str = "Extracted Images - New"
    register_template: str = "Week_{week:02d}_Image_Register.xlsx"
    register_sheet: str = "Image Register"
    allowed_image_suffixes: tuple[str, ...] = (".jpg", ".jpeg", ".png")
    # Placeholder the hand-built reports use for a missing Official Location.
    unlabelled_placeholder: str = "Not Labelled"
    cors_origins: tuple[str, ...] = field(
        default=("http://localhost:5173", "http://localhost:3000")
    )
    # Phase 5 -- AI condition analysis (separate store; disabled unless configured).
    analysis_dir: Path = DEFAULT_ANALYSIS_DIR
    ai_provider: str = "disabled"
    ai_allow_live: bool = False
    ai_mock_scenario: str = "stable"
    ai_model: str | None = None  # None -> groq_provider.DEFAULT_MODEL (qwen/qwen3.8-27b)
    automation_root: Path = DEFAULT_DATA_ROOT / "automation"
    # Phase 7 -- Excel register imports (separate append-only store; production folders are never written).
    import_dir: Path = DEFAULT_IMPORT_DIR
    # Phase 8.2 -- accounts + audit log. None -> DEFAULT_AUTH_DIR, resolved at call time (see resolved_auth_dir).
    auth_dir: Path | None = None
    session_idle_seconds: int = 8 * 3600

    # Phase 8.3 -- backups. None -> DEFAULT_BACKUP_DIR (resolved at call time, like auth_dir).
    backup_dir: Path | None = None

    # Phase 9 -- Excel Location Intelligence (own configuration; the API key is never stored here).
    location_ai_provider: str = "disabled"
    location_ai_model: str | None = None
    location_ai_allow_live: bool = False
    location_ai_timeout_s: float = 20.0

    # Infrastructure (2026-11-23): rate limiting, caching, circuit breaker. In-process state only.
    rate_limit_enabled: bool = True
    rate_limits: tuple[tuple[str, int], ...] = ()  # overrides of app.infra.rate_limit.DEFAULT_LIMITS
    trusted_proxies: tuple[str, ...] = ()
    cache_ttl_seconds: float = 300.0
    ai_breaker_threshold: int = 3
    ai_breaker_recovery_seconds: float = 60.0

    # SV-02: /docs, /redoc and /openapi.json. On for local development; off when SYNERGY_ENV=production
    # (unless SYNERGY_API_DOCS=1) or when SYNERGY_API_DOCS=0.
    api_docs: bool = True
    # SV-02: force the Secure flag on the photo cookie (set SYNERGY_COOKIE_SECURE=1 behind an HTTPS proxy). It is
    # also set automatically whenever the request itself arrives over HTTPS.
    cookie_secure: bool = False

    @property
    def resolved_auth_dir(self) -> Path:
        import sys
        return self.auth_dir or sys.modules[__name__].DEFAULT_AUTH_DIR

    @property
    def resolved_backup_dir(self) -> Path:
        import sys
        return self.backup_dir or sys.modules[__name__].DEFAULT_BACKUP_DIR


def get_settings() -> Settings:
    env = os.environ
    kwargs: dict = {}
    if env.get("SYNERGY_DATA_ROOT"):
        kwargs["data_root"] = Path(env["SYNERGY_DATA_ROOT"]).resolve()
    if env.get("SYNERGY_IMPORT_DIR"):
        kwargs["import_dir"] = Path(env["SYNERGY_IMPORT_DIR"]).resolve()
    if env.get("SYNERGY_BACKUP_DIR"):
        kwargs["backup_dir"] = Path(env["SYNERGY_BACKUP_DIR"]).resolve()
    if env.get("SYNERGY_AUTH_DIR"):
        kwargs["auth_dir"] = Path(env["SYNERGY_AUTH_DIR"]).resolve()
    if env.get("SYNERGY_ANALYSIS_DIR"):
        kwargs["analysis_dir"] = Path(env["SYNERGY_ANALYSIS_DIR"]).resolve()
    provider = env.get("SYNERGY_AI_PROVIDER", "disabled").strip().lower()
    kwargs["ai_provider"] = provider if provider in AI_PROVIDERS else "disabled"
    kwargs["ai_allow_live"] = env.get("SYNERGY_AI_ALLOW_LIVE", "").strip() == "1"
    kwargs["ai_model"] = parse_ai_model(env.get("SYNERGY_AI_MODEL"), kwargs["ai_provider"])
    lp = env.get("SYNERGY_LOCATION_AI_PROVIDER", "").strip().lower()
    kwargs["location_ai_provider"] = lp if re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", lp) else "disabled"
    lm = env.get("SYNERGY_LOCATION_AI_MODEL", "").strip()
    kwargs["location_ai_model"] = lm if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,99}", lm) else None
    kwargs["location_ai_allow_live"] = env.get("SYNERGY_LOCATION_AI_ALLOW_LIVE", "").strip() == "1"
    if env.get("SYNERGY_AI_MOCK_SCENARIO"):
        kwargs["ai_mock_scenario"] = env["SYNERGY_AI_MOCK_SCENARIO"].strip().lower()
    kwargs.update(_infrastructure_settings(env))
    kwargs.update(_access_settings(env))
    return Settings(**kwargs)


def _access_settings(env) -> dict:
    """SV-02: API documentation and photo-cookie options."""
    production = env.get("SYNERGY_ENV", "").strip().lower() == "production"
    docs = env.get("SYNERGY_API_DOCS", "").strip()
    return {"api_docs": docs == "1" if docs in ("0", "1") else not production,
            "cookie_secure": env.get("SYNERGY_COOKIE_SECURE", "").strip() == "1"}


def _number(env, name: str, cast, minimum):
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = cast(raw)
    except ValueError:
        log.warning("Ignoring %s: not a number.", name)
        return None
    if value < minimum:
        log.warning("Ignoring %s: must be at least %s.", name, minimum)
        return None
    return value


def _infrastructure_settings(env) -> dict:
    from .infra.rate_limit import DEFAULT_LIMITS

    out: dict = {"rate_limit_enabled": env.get("SYNERGY_RATE_LIMIT_ENABLED", "1").strip() != "0"}
    overrides = []
    for category in DEFAULT_LIMITS:
        value = _number(env, f"SYNERGY_RATE_LIMIT_{category.upper()}", int, 0)
        if value is not None:
            overrides.append((category, value))
    out["rate_limits"] = tuple(overrides)
    for key, name in (("trusted_proxies", "SYNERGY_TRUSTED_PROXIES"), ("cors_origins", "SYNERGY_CORS_ORIGINS")):
        items = tuple(x.strip() for x in env.get(name, "").split(",") if x.strip())
        if items:
            out[key] = items
    for key, name, cast, minimum in (("cache_ttl_seconds", "SYNERGY_CACHE_TTL_SECONDS", float, 0),
                                     ("ai_breaker_threshold", "SYNERGY_AI_BREAKER_THRESHOLD", int, 1),
                                     ("ai_breaker_recovery_seconds", "SYNERGY_AI_BREAKER_RECOVERY_SECONDS", float, 1)):
        value = _number(env, name, cast, minimum)
        if value is not None:
            out[key] = value
    return out
