"""Synergy Vessel dashboard backend (inspection data read-only; changes require sign-in, Phase 8.2).

Run from dashboard/backend:
    .venv\\Scripts\\python -m uvicorn app.main:app --port 8000 --no-proxy-headers

--no-proxy-headers: uvicorn must not rewrite the client address from X-Forwarded-For (it trusts localhost by
default); SYNERGY_TRUSTED_PROXIES is the only setting that makes that header count (app/infra/rate_limit.py).
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import APP_VERSION
from .ai.providers import MOCK_SCENARIOS, ConditionProvider, DisabledProvider, MockConditionProvider
from .ai.service import AnalysisError, AnalysisService
from .ai.store import AnalysisStore
from .config import Settings, get_settings, parse_ai_model
from .auth.audit import AuditLog
from .auth.service import AuthService
from .auth.users import AccountError, UserStore
from .imports.service import ImportFailure, ImportService
from .infra import rate_limit
from .infra.cache import TTLCache
from .infra.circuit_breaker import CircuitBreaker
from .routes import ai_analysis, auth, health, images, imports, inspections, reports, vessels
from .services.inspection_store import InspectionStore


def build_provider(settings: Settings) -> ConditionProvider:
    if settings.ai_provider == "mock":
        scenario = settings.ai_mock_scenario if settings.ai_mock_scenario in MOCK_SCENARIOS else "stable"
        return MockConditionProvider(scenario)
    if settings.ai_provider == "gemini":
        from .ai.gemini_provider import GeminiConditionProvider

        return GeminiConditionProvider(settings.automation_root, allow_live=settings.ai_allow_live,
                                       model=parse_ai_model(settings.ai_model, "gemini"))
    if settings.ai_provider == "groq":
        from .ai.groq_provider import GroqConditionProvider

        return GroqConditionProvider(settings.automation_root, allow_live=settings.ai_allow_live,
                                     model=parse_ai_model(settings.ai_model, "groq"))
    return DisabledProvider()


def create_app(settings: Optional[Settings] = None, condition_provider: Optional[ConditionProvider] = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        auth_dir = settings.resolved_auth_dir
        app.state.auth = AuthService(UserStore(auth_dir), AuditLog(auth_dir), idle_seconds=settings.session_idle_seconds)
        app.state.store = InspectionStore.build(settings)
        analyses = AnalysisStore(settings.analysis_dir)
        analyses.load()
        breaker = CircuitBreaker(settings.ai_breaker_threshold, settings.ai_breaker_recovery_seconds)
        app.state.analysis = AnalysisService(app.state.store, analyses, condition_provider or build_provider(settings),
                                             breaker=breaker)

        def reload_store() -> None:
            # Build the new record set first; swap references only when it is complete.
            new = InspectionStore.build(settings)
            app.state.store = new
            app.state.analysis.inspections = new
            app.state.analysis.validator.store = new
            app.state.cache.clear()  # entries are tied to the old store anyway; this just frees memory

        app.state.imports = ImportService(settings, lambda: app.state.store, reload_store)
        yield

    docs = {} if settings.api_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}  # SV-02
    app = FastAPI(title="Synergy Vessel Inspection API", version=APP_VERSION, lifespan=lifespan, **docs)
    app.state.settings = settings  # SV-02: read by the sign-in routes (photo-cookie options)
    # Infrastructure (2026-11-23), kept outside the business logic: see dashboard/INFRASTRUCTURE.md.
    app.state.cache = TTLCache(settings.cache_ttl_seconds)
    if settings.rate_limit_enabled:
        app.state.rate_limiter = rate_limit.RateLimiter({**rate_limit.DEFAULT_LIMITS, **dict(settings.rate_limits)})

        def user_for_token(token: str):
            auth_service = getattr(app.state, "auth", None)
            return auth_service.peek(token) if auth_service is not None else None

        rate_limit.install(app, app.state.rate_limiter, trusted_proxies=settings.trusted_proxies,
                           user_for_token=user_for_token)
    # Added after the rate limiter, so CORS is the outer layer and 429 responses carry CORS headers too.
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["*"])
    for r in (health.router, vessels.router, inspections.router, images.router, ai_analysis.router, reports.router, imports.router,
              imports.review_router, auth.router, auth.admin):
        app.include_router(r)

    @app.exception_handler(AnalysisError)
    async def _analysis_error(_: Request, exc: AnalysisError) -> JSONResponse:
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code}, headers=headers)

    @app.exception_handler(AccountError)
    async def _account_error(_: Request, exc: AccountError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code}, headers=headers)

    @app.exception_handler(ImportFailure)
    async def _import_error(_: Request, exc: ImportFailure) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code, "guidance": exc.guidance})

    return app


app = create_app()
