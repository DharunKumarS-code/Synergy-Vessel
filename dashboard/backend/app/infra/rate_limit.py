"""In-process rate limiting for the API (sliding 60-second window).

Requests are grouped into categories, each with its own per-client limit per minute. A client is the
signed-in user (when the bearer token belongs to a live session) or otherwise the client IP address.
Sign-in attempts are always limited per IP address, in addition to the existing per-username lockout.

Limits are generous for normal browsing and strict only for expensive or sensitive actions:

    login        POST /api/auth/login                        10 / min per IP
    ai_analysis  POST /api/ai-analysis/compare                6 / min (each call may reach the AI provider)
    reports      GET  /api/reports/* downloads (not preview)  10 / min (PDF / CSV generation)
    imports      POST /api/imports and /api/imports/preview   10 / min (workbook upload and parsing)
    writes       any other POST / PUT / PATCH / DELETE        60 / min
    images       GET  /api/images/*                         1200 / min (photo grids load many at once)
    reads        any other GET under /api/                   600 / min (stops accidental request loops)

/api/health and CORS pre-flight (OPTIONS) requests are never limited. A limit of 0 disables that category.
State lives in this process only (see dashboard/INFRASTRUCTURE.md).

Client addresses: start uvicorn with --no-proxy-headers so it never rewrites the client address from
X-Forwarded-For; SYNERGY_TRUSTED_PROXIES is then the only switch that makes that header count (see client_ip).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Callable, Iterable, Mapping, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

WINDOW_SECONDS = 60.0
DEFAULT_LIMITS: dict[str, int] = {
    "login": 10,
    "ai_analysis": 6,
    "reports": 10,
    "imports": 10,
    "writes": 60,
    "images": 1200,
    "reads": 600,
}
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_UNLIMITED_PATHS = frozenset({"/api/health"})
log = logging.getLogger(__name__)


def categorise(method: str, path: str) -> Optional[str]:
    """The limit category for a request, or None when it is not limited."""
    method = method.upper()
    if not path.startswith("/api/") or path in _UNLIMITED_PATHS or method in ("OPTIONS", "HEAD"):
        return None
    if method == "POST" and path == "/api/auth/login":
        return "login"
    if method == "POST" and path == "/api/ai-analysis/compare":
        return "ai_analysis"
    if method == "POST" and path.rstrip("/") in ("/api/imports", "/api/imports/preview"):
        return "imports"
    if method in _WRITE_METHODS:
        return "writes"
    if path.startswith("/api/reports/") and path != "/api/reports/preview":
        return "reports"
    if path.startswith("/api/images/"):
        return "images"
    return "reads"


class RateLimiter:
    """Thread-safe sliding-window counter per (category, client)."""

    def __init__(self, limits: Mapping[str, int], *, window: float = WINDOW_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.limits = dict(limits)
        self.window = window
        self.clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def hit(self, category: str, client: str) -> Optional[float]:
        """Record one request. Returns None if allowed, else the seconds until the client may retry."""
        limit = self.limits.get(category, 0)
        if limit <= 0:
            return None
        now = self.clock()
        cutoff = now - self.window
        with self._lock:
            q = self._hits.setdefault((category, client), deque())
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, q[0] + self.window - now)
            q.append(now)
            self._calls += 1
            if self._calls % 2000 == 0:  # keep memory bounded: forget clients that have gone quiet
                for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
                    del self._hits[key]
        return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Shared identity for requests that carry X-Forwarded-For but did not come from a configured trusted proxy.
UNTRUSTED_FORWARDED = "forwarded-untrusted"


def client_ip(request: Request, trusted_proxies: Iterable[str] = ()) -> str:
    """The client address used as the rate-limit identity.

    X-Forwarded-For is honoured only when the direct peer is listed in SYNERGY_TRUSTED_PROXIES. From anyone else
    the header is never used as an address. Such requests also share ONE identity instead of the peer address:
    if the server in front already rewrote the peer from that header (uvicorn does this for localhost unless it is
    started with --no-proxy-headers), the "peer" itself may be forged, and a fresh value per request must not buy
    a fresh request budget. Clients that send no forwarding header -- every normal browser request -- are unaffected.
    """
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if peer in set(trusted_proxies):
        # The right-most entry is the address the trusted proxy itself saw; earlier entries can be forged.
        last = forwarded.split(",")[-1].strip()
        return last or peer
    if forwarded.strip():
        return UNTRUSTED_FORWARDED
    return peer


def install(app, limiter: RateLimiter, *, trusted_proxies: Iterable[str] = (),
            user_for_token: Optional[Callable[[str], Optional[str]]] = None) -> None:
    """Register the rate-limit middleware on ``app``. ``user_for_token`` maps a bearer token to a user id
    without extending the session (only used to choose the rate-limit key)."""
    proxies = tuple(trusted_proxies)

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        category = categorise(request.method, request.url.path)
        if category is None:
            return await call_next(request)
        ip = client_ip(request, proxies)
        client = f"ip:{ip}"
        if category != "login" and user_for_token is not None:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                user_id = user_for_token(token.strip())
                if user_id:
                    client = f"user:{user_id}"
        retry_after = limiter.hit(category, client)
        if retry_after is None:
            return await call_next(request)
        seconds = int(math.ceil(retry_after))
        log.warning("Rate limit reached: category=%s client=%s", category, client)  # never logs tokens
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many requests. Please wait {seconds} seconds and try again.",
                     "code": "too_many_requests", "retry_after_seconds": seconds},
            headers={"Retry-After": str(seconds)},
        )
