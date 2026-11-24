"""Infrastructure (2026-11-23): rate limiting, caching, circuit breaker, reverse-proxy and CI configuration.

Everything here is offline: the AI provider is the built-in mock (or a scripted fake), clocks are fake where
timing matters, and the nginx / GitHub Actions files are only read, never run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.ai.providers import MockConditionProvider, ProviderQuotaExhausted, ProviderUnavailable
from app.config import Settings, _infrastructure_settings
from app.imports.parser import MAX_UPLOAD_BYTES
from app.infra.cache import TTLCache
from app.infra.circuit_breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker, CircuitOpen
from app.infra.rate_limit import DEFAULT_LIMITS, UNTRUSTED_FORWARDED, RateLimiter, categorise, client_ip
from app.main import create_app
from conftest import sign_in

DASHBOARD = Path(__file__).resolve().parents[2]
PROJECT = DASHBOARD.parent
NGINX = DASHBOARD / "deploy" / "nginx" / "synergy-vessel.conf"
PROXY_ADDRESS = "127.0.0.1:8088"  # fix M2: 8080 is used by another program on this computer
CI = PROJECT / ".github" / "workflows" / "ci.yml"


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def app_client(data_root, tmp_path):
    clients = []

    def _make(provider=None, **overrides):
        settings = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses", **overrides)
        c = TestClient(create_app(settings, condition_provider=provider or MockConditionProvider("stable")))
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def pair(client, earlier="W30-0001", later="W34-0001", **extra):
    recs = client.app.state.store.records
    rid = lambda image: next(r.record_id for r in recs if r.image_id == image and r.source == "json_report")
    return {"earlier_record_id": rid(earlier), "later_record_id": rid(later), "acknowledge_warnings": True, **extra}


# =========================================================================== rate limiting
@pytest.mark.parametrize("method,path,category", [
    ("POST", "/api/auth/login", "login"),
    ("POST", "/api/ai-analysis/compare", "ai_analysis"),
    ("POST", "/api/imports", "imports"),
    ("POST", "/api/imports/preview", "imports"),
    ("POST", "/api/imports/IMP-1/withdraw", "writes"),
    ("POST", "/api/ai-analysis/CA-1/review", "writes"),
    ("GET", "/api/reports/inspection.pdf", "reports"),
    ("GET", "/api/reports/inspections.csv", "reports"),
    ("GET", "/api/reports/preview", "reads"),
    ("GET", "/api/images/W30-0001", "images"),
    ("GET", "/api/inspections", "reads"),
    ("GET", "/api/health", None),
    ("OPTIONS", "/api/ai-analysis/compare", None),
    ("GET", "/compare", None),
])
def test_requests_are_categorised(method, path, category):
    assert categorise(method, path) == category


def test_sliding_window_blocks_then_recovers():
    clock = Clock()
    rl = RateLimiter({"login": 3, "reads": 0}, clock=clock)
    assert [rl.hit("login", "ip:a") for _ in range(3)] == [None, None, None]
    assert rl.hit("login", "ip:a") == pytest.approx(60.0)          # 4th within the minute: blocked
    assert rl.hit("login", "ip:b") is None                         # other clients are unaffected
    clock.t += 30
    assert rl.hit("login", "ip:a") == pytest.approx(30.0)
    clock.t += 30.01
    assert rl.hit("login", "ip:a") is None                         # the oldest requests left the window
    assert all(rl.hit("reads", "ip:a") is None for _ in range(5000))  # 0 = unlimited


def test_forwarded_for_is_trusted_only_from_a_configured_proxy():
    class Req:
        def __init__(self, host, xff=None):
            self.client = type("C", (), {"host": host})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    assert client_ip(Req("10.0.0.5")) == "10.0.0.5"                                   # normal request: own address
    assert client_ip(Req("10.0.0.5", "1.2.3.4")) == UNTRUSTED_FORWARDED              # not trusted: never an address (M1)
    assert client_ip(Req("127.0.0.1", "6.6.6.6, 1.2.3.4"), ["127.0.0.1"]) == "1.2.3.4"  # right-most = seen by proxy
    assert client_ip(Req("127.0.0.1"), ["127.0.0.1"]) == "127.0.0.1"


def test_login_attempts_are_limited_per_address_with_429_and_retry_after(app_client):
    c = app_client(rate_limits=(("login", 3),))
    bad = {"username": "nobody", "password": "wrong-password"}
    assert [c.post("/api/auth/login", json=bad).status_code for _ in range(3)] == [401, 401, 401]
    r = c.post("/api/auth/login", json=bad, headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 429
    assert r.json()["code"] == "too_many_requests"
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"  # the browser can read the 429
    assert "wrong-password" not in r.text
    assert c.get("/api/health").status_code == 200                              # never limited


def test_ai_analysis_is_limited_per_user_and_other_users_are_unaffected(app_client):
    provider = MockConditionProvider("stable")
    c = app_client(provider, rate_limits=(("ai_analysis", 2),))
    sign_in(c, "reviewer")
    first = c.post("/api/ai-analysis/compare", json=pair(c))
    assert first.status_code == 200
    assert c.post("/api/ai-analysis/compare", json=pair(c)).status_code == 200   # reused result, still counted
    blocked = c.post("/api/ai-analysis/compare", json=pair(c))
    assert blocked.status_code == 429 and blocked.json()["code"] == "too_many_requests"
    assert len(provider.calls) == 1
    sign_in(c, "reviewer")                                                      # a different user, same address
    assert c.post("/api/ai-analysis/compare", json=pair(c)).status_code == 200


def test_normal_browsing_is_not_restricted_by_default(app_client):
    c = app_client()
    sign_in(c, "viewer")                                                   # SV-02
    assert DEFAULT_LIMITS["reads"] >= 600 and DEFAULT_LIMITS["images"] >= 1200
    assert all(c.get("/api/vessels").status_code == 200 for _ in range(150))


def test_rate_limiting_can_be_switched_off(app_client):
    c = app_client(rate_limit_enabled=False, rate_limits=(("reads", 1),))
    assert not hasattr(c.app.state, "rate_limiter")
    sign_in(c, "viewer")                                                   # SV-02
    assert [c.get("/api/vessels").status_code for _ in range(5)] == [200] * 5


def test_infrastructure_settings_come_from_the_environment_and_bad_values_are_ignored():
    out = _infrastructure_settings({
        "SYNERGY_RATE_LIMIT_ENABLED": "0", "SYNERGY_RATE_LIMIT_LOGIN": "5", "SYNERGY_RATE_LIMIT_READS": "lots",
        "SYNERGY_RATE_LIMIT_IMAGES": "-1", "SYNERGY_TRUSTED_PROXIES": "127.0.0.1, ::1",
        "SYNERGY_CORS_ORIGINS": "http://127.0.0.1:8088", "SYNERGY_CACHE_TTL_SECONDS": "30",
        "SYNERGY_AI_BREAKER_THRESHOLD": "0", "SYNERGY_AI_BREAKER_RECOVERY_SECONDS": "120",
    })
    assert out["rate_limit_enabled"] is False
    assert out["rate_limits"] == (("login", 5),)
    assert out["trusted_proxies"] == ("127.0.0.1", "::1")
    assert out["cors_origins"] == ("http://127.0.0.1:8088",)
    assert out["cache_ttl_seconds"] == 30.0
    assert "ai_breaker_threshold" not in out                       # below the minimum of 1: ignored
    assert out["ai_breaker_recovery_seconds"] == 120.0
    defaults = _infrastructure_settings({})
    assert defaults == {"rate_limit_enabled": True, "rate_limits": ()}


# --------------------------------------------------------------------------- fix M1: forged X-Forwarded-For
def _forged(i: int) -> dict:
    return {"X-Forwarded-For": f"198.51.100.{i}"}


# SV-02: data views need an account, so these address-identity checks use the public session check, which
# is rate-limited in the same "reads" category.
SESSION = "/api/auth/session"


def test_m1_forged_forwarded_for_never_creates_a_new_identity(app_client):
    c = app_client(rate_limits=(("reads", 3),))
    codes = [c.get(SESSION, headers=_forged(i)).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]                      # all forged values share ONE budget
    blocked = c.get(SESSION, headers=_forged(99))
    assert blocked.status_code == 429 and blocked.json()["code"] == "too_many_requests"
    assert int(blocked.headers["Retry-After"]) >= 1
    assert c.get(SESSION).status_code == 200                # a normal request keeps its own budget


def test_m1_bypass_stays_closed_even_if_uvicorn_rewrites_the_client_address(app_client):
    """Reproduces the verified bypass: uvicorn's default proxy-header handling (without --no-proxy-headers)
    replaces the client address with the forged header before the app runs. Before the fix every forged value
    got a fresh budget (all 200); now they share one identity."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    inner = app_client(rate_limits=(("reads", 3),))
    with TestClient(ProxyHeadersMiddleware(inner.app, trusted_hosts="*")) as c:
        codes = [c.get(SESSION, headers=_forged(i)).status_code for i in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        assert c.get(SESSION).status_code == 200            # requests without the header are unaffected


def test_m1_sign_in_limit_cannot_be_dodged_with_forged_addresses(app_client):
    c = app_client(rate_limits=(("login", 3),))
    bad = {"username": "nobody", "password": "wrong-password"}
    codes = [c.post("/api/auth/login", json=bad, headers=_forged(i)).status_code for i in range(4)]
    assert codes == [401, 401, 401, 429]


def test_m1_explicitly_trusted_proxy_still_identifies_each_client(app_client):
    # TestClient connects as "testclient": configure it as the trusted proxy, like 127.0.0.1 behind nginx.
    c = app_client(rate_limits=(("reads", 2),), trusted_proxies=("testclient",))
    first = [c.get(SESSION, headers={"X-Forwarded-For": "203.0.113.1"}).status_code for _ in range(3)]
    assert first == [200, 200, 429]
    assert c.get(SESSION, headers={"X-Forwarded-For": "203.0.113.2"}).status_code == 200
    # nginx appends the address it saw; a client-supplied left-hand entry cannot choose the identity
    spoof = c.get(SESSION, headers={"X-Forwarded-For": "203.0.113.2, 203.0.113.1"})
    assert spoof.status_code == 429


def test_m1_signed_in_users_keep_their_own_budget_with_or_without_the_header(app_client):
    c = app_client(rate_limits=(("reads", 2),))
    sign_in(c, "reviewer")
    assert [c.get(SESSION, headers=_forged(i)).status_code for i in range(3)] == [200, 200, 429]
    del c.headers["Authorization"]
    assert c.get(SESSION, headers=_forged(7)).status_code == 200   # the shared forwarded bucket is separate
    sign_in(c, "reviewer")                                                 # another user: unaffected
    assert c.get(SESSION).status_code == 200
    assert c.get("/api/auth/session").json()["authenticated"] is True      # authentication itself unchanged


def test_m1_documented_backend_start_commands_disable_uvicorn_proxy_headers():
    guide = (DASHBOARD / "OPERATOR_GUIDE.md").read_text(encoding="utf-8")
    infra = (DASHBOARD / "INFRASTRUCTURE.md").read_text(encoding="utf-8")
    main = (DASHBOARD / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    for text in (guide, infra, main):
        commands = re.findall(r"-m uvicorn app\.main:app[^\n]*", text)
        assert commands, "no backend start command found"
        assert all("--no-proxy-headers" in cmd for cmd in commands), commands


# =========================================================================== caching
def test_cache_hits_expires_and_is_bounded():
    clock = Clock()
    cache = TTLCache(ttl_seconds=10, max_entries=2, clock=clock)
    owner = object()
    calls = []
    compute = lambda: calls.append(1) or len(calls)
    assert cache.get_or_compute("k", compute, owner=owner) == 1
    assert cache.get_or_compute("k", compute, owner=owner) == 1          # hit
    clock.t += 10.01
    assert cache.get_or_compute("k", compute, owner=owner) == 2          # expired -> recomputed
    cache.get_or_compute("a", compute, owner=owner)
    cache.get_or_compute("b", compute, owner=owner)
    assert cache.stats()["entries"] == 2                                 # oldest entry evicted


def test_cache_entries_belong_to_one_data_set():
    cache = TTLCache(ttl_seconds=300)
    old, new = object(), object()
    assert cache.get_or_compute("vessels", lambda: "old data", owner=old) == "old data"
    assert cache.get_or_compute("vessels", lambda: "new data", owner=new) == "new data"


def test_zero_ttl_disables_the_cache():
    cache = TTLCache(ttl_seconds=0)
    n = iter(range(10))
    assert [cache.get_or_compute("k", lambda: next(n)) for _ in range(3)] == [0, 1, 2]


def test_vessels_and_locations_are_served_from_the_cache(app_client):
    c = app_client()
    sign_in(c, "viewer")
    first = c.get("/api/vessels").json()
    misses = c.app.state.cache.misses
    assert c.get("/api/vessels").json() == first
    assert c.app.state.cache.misses == misses and c.app.state.cache.hits >= 1
    loc = c.get("/api/locations").json()
    assert c.get("/api/locations").json() == loc


def test_a_data_reload_invalidates_cached_summaries(app_client):
    c = app_client()
    sign_in(c, "viewer")
    c.get("/api/vessels")
    before = c.app.state.store
    c.app.state.imports._reload()                      # what an import / withdrawal / location decision does
    assert c.app.state.store is not before
    assert c.app.state.cache.stats()["entries"] == 0
    misses = c.app.state.cache.misses
    c.get("/api/vessels")
    assert c.app.state.cache.misses == misses + 1      # recomputed from the new data


def test_report_preview_reflects_a_new_review_immediately(app_client):
    c = app_client()
    sign_in(c, "reviewer")
    rec = c.post("/api/ai-analysis/compare", json=pair(c)).json()["analysis"]
    status = lambda: {a["analysis_id"]: a["review_status"] for a in c.get("/api/reports/preview").json()["analyses"]}
    assert status()[rec["analysis_id"]] == "PENDING_REVIEW"
    assert status()[rec["analysis_id"]] == "PENDING_REVIEW"            # second call: cached
    sign_in(c, "reviewer")                                             # SV-03: another reviewer approves
    r = c.post(f"/api/ai-analysis/{rec['analysis_id']}/review", json={"status": "APPROVED", "note": None})
    assert r.status_code == 200
    assert status()[rec["analysis_id"]] == "APPROVED"                  # never stale after an update


def test_sessions_and_assessments_are_not_cached(app_client):
    c = app_client()
    sign_in(c, "reviewer")
    c.get("/api/auth/session")
    c.get("/api/ai-analysis/history")
    keys = [k[0][0] for k in c.app.state.cache._data]
    assert not any(k in ("session", "auth", "history", "analysis") for k in keys)


# =========================================================================== circuit breaker
def test_breaker_opens_after_the_threshold_and_recovers_through_half_open():
    clock = Clock()
    b = CircuitBreaker(failure_threshold=3, recovery_seconds=60, clock=clock)
    for _ in range(2):
        b.before_call(); b.record_failure()
    assert b.state == CLOSED
    b.before_call(); b.record_failure()
    assert b.state == OPEN
    with pytest.raises(CircuitOpen) as exc:
        b.before_call()
    assert exc.value.retry_after == pytest.approx(60)
    clock.t += 60
    assert b.state == HALF_OPEN
    b.before_call()                                    # the single trial
    with pytest.raises(CircuitOpen):
        b.before_call()                                # a second caller waits for the trial
    b.record_success()
    assert b.state == CLOSED and b.snapshot()["consecutive_failures"] == 0


def test_a_failed_trial_reopens_and_a_longer_provider_wait_is_respected():
    clock = Clock()
    b = CircuitBreaker(failure_threshold=1, recovery_seconds=60, clock=clock)
    b.before_call(); b.record_failure(retry_after=600)  # e.g. quota exhausted, retry in 10 minutes
    assert b.snapshot()["retry_after_seconds"] == 600
    clock.t += 600
    b.before_call(); b.record_failure()
    assert b.state == OPEN and b.snapshot()["retry_after_seconds"] == 60


def test_successes_reset_the_failure_count():
    b = CircuitBreaker(failure_threshold=2)
    b.before_call(); b.record_failure()
    b.before_call(); b.record_success()
    b.before_call(); b.record_failure()
    assert b.state == CLOSED


class Scripted(MockConditionProvider):
    """Mock provider whose next answers are scripted: an exception is raised, anything else is returned."""

    def __init__(self, *answers):
        super().__init__("stable")
        self.answers = list(answers)

    def analyse(self, earlier, later, prompt):
        self.response = self.answers.pop(0) if self.answers else "stable"
        return super().analyse(earlier, later, prompt)


def test_an_unavailable_ai_provider_pauses_analysis_without_calls_fake_results_or_lost_records(app_client, tmp_path):
    provider = Scripted(ProviderUnavailable(), ProviderUnavailable(), ProviderUnavailable())
    c = app_client(provider, ai_breaker_threshold=3, ai_breaker_recovery_seconds=60)
    clock = Clock()
    c.app.state.analysis.breaker = CircuitBreaker(3, 60, clock=clock)
    sign_in(c, "reviewer")
    codes = [c.post("/api/ai-analysis/compare", json=pair(c)).json()["code"] for _ in range(3)]
    assert codes == ["ai_unavailable"] * 3
    r = c.post("/api/ai-analysis/compare", json=pair(c))
    assert r.status_code == 503 and r.json()["code"] == "ai_circuit_open"
    assert r.headers["Retry-After"] == "60"
    assert "no result was saved" in r.json()["detail"]
    assert len(provider.calls) == 3                    # the 4th request never reached the provider
    assert not list((tmp_path / "analyses").glob("*.json")) if (tmp_path / "analyses").exists() else True
    assert c.get("/api/ai-analysis/status").json()["circuit"]["state"] == "open"
    clock.t += 60                                      # recovery: one trial request is let through
    ok = c.post("/api/ai-analysis/compare", json=pair(c))
    assert ok.status_code == 200 and ok.json()["analysis"]["simulated"] is True
    assert c.get("/api/ai-analysis/status").json()["circuit"]["state"] == "closed"
    assert len(list((tmp_path / "analyses").glob("*.json"))) == 1


def test_existing_records_stay_available_while_the_circuit_is_open(app_client):
    c = app_client(Scripted("stable", ProviderQuotaExhausted(retry_after_seconds=3600)), ai_breaker_threshold=1)
    sign_in(c, "reviewer")
    saved = c.post("/api/ai-analysis/compare", json=pair(c)).json()["analysis"]
    c.post(f"/api/ai-analysis/{saved['analysis_id']}/review", json={"status": "REJECTED", "note": "retry"})
    assert c.post("/api/ai-analysis/compare", json=pair(c, reanalyse=True)).json()["code"] == "quota_exhausted"
    blocked = c.post("/api/ai-analysis/compare", json=pair(c, reanalyse=True))
    assert blocked.json()["code"] == "ai_circuit_open" and int(blocked.headers["Retry-After"]) > 3000
    assert c.get(f"/api/ai-analysis/{saved['analysis_id']}").status_code == 200
    assert c.get("/api/ai-analysis/history").json()["total"] == 1
    # Reviews do not depend on the AI provider. SV-03: a decided assessment can only be corrected by an
    # administrator, with a reason.
    sign_in(c, "admin")
    fix = c.post(f"/api/ai-analysis/{saved['analysis_id']}/review",
                 json={"status": "APPROVED", "note": "Checked on board; correct after all."})
    assert fix.status_code == 200 and fix.json()["review"]["history"][-1]["correction"] is True


def test_an_invalid_ai_answer_is_not_counted_as_an_outage(app_client):
    c = app_client(Scripted({"nonsense": True}, {"nonsense": True}), ai_breaker_threshold=1)
    sign_in(c, "reviewer")
    assert c.post("/api/ai-analysis/compare", json=pair(c)).json()["code"] == "invalid_ai_response"
    assert c.post("/api/ai-analysis/compare", json=pair(c)).json()["code"] == "invalid_ai_response"
    assert c.app.state.analysis.breaker.state == CLOSED


# =========================================================================== reverse proxy (static checks)
def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _nginx() -> str:
    """The configuration as nginx sees it: comments removed and `include file.conf;` (SV-07: security_headers.conf,
    proxy_api.conf, next to the main file) replaced by that file's content."""
    def inline(m):
        return _strip_comments((NGINX.parent / m.group(1)).read_text(encoding="utf-8"))
    return re.sub(r"^\s*include\s+([\w.-]+\.conf)\s*;", inline, _strip_comments(NGINX.read_text(encoding="utf-8")), flags=re.M)


def _location(conf: str, name: str) -> str:
    # optional ^~ modifier (SV-07: API and asset prefixes take precedence over the regex block-list)
    m = re.search(r"location\s+(?:\^~\s+)?" + re.escape(name) + r"\s*\{(.*?)\n\s*\}", conf, re.S)
    assert m, name
    return m.group(1)


def _seconds(block: str, directive: str) -> int:
    return int(re.search(directive + r"\s+(\d+)s;", block).group(1))


def test_reverse_proxy_is_local_only_and_routes_api_to_fastapi():
    conf = _nginx()
    assert conf.count("{") == conf.count("}")
    assert re.findall(r"listen\s+([^;]+);", conf) == [PROXY_ADDRESS]           # never a public interface
    assert "server 127.0.0.1:8000;" in conf
    assert "proxy_pass             http://synergy_api;" in _location(conf, "/api/")
    assert "try_files  $uri /index.html;" in _location(conf, "/")            # client-side routes
    assert "ssl_certificate" not in conf                                     # HTTPS only as a commented template
    assert "/docs" not in conf
    for header in ("X-Forwarded-For", "X-Real-IP", "X-Forwarded-Proto"):
        assert header in _location(conf, "/api/")


def test_reverse_proxy_limits_match_the_application():
    conf = _nginx()
    imports = _location(conf, "/api/imports")
    size = int(re.search(r"client_max_body_size\s+(\d+)m;", imports).group(1)) * 1024 * 1024
    assert size >= MAX_UPLOAD_BYTES
    assert re.search(r"server \{.*?client_max_body_size\s+2m;", conf, re.S)
    frontend_api = (DASHBOARD / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")
    compare_ms = int(re.search(r"compareImages = .*timeout: (\d+)", frontend_api).group(1))
    assert _seconds(_location(conf, "= /api/ai-analysis/compare"), "proxy_read_timeout") * 1000 >= compare_ms
    assert _seconds(_location(conf, "/api/reports/"), "proxy_read_timeout") >= 120
    assert _seconds(imports, "proxy_read_timeout") >= 300


def test_m2_proxy_port_is_consistent_and_8080_is_no_longer_used():
    conf = NGINX.read_text(encoding="utf-8")
    infra = (DASHBOARD / "INFRASTRUCTURE.md").read_text(encoding="utf-8")
    guide = (DASHBOARD / "OPERATOR_GUIDE.md").read_text(encoding="utf-8")
    assert f"listen       {PROXY_ADDRESS};" in conf
    assert f"http://{PROXY_ADDRESS}" in conf                         # header comment
    assert f"**{PROXY_ADDRESS} only**" in infra and f"http://{PROXY_ADDRESS}" in infra
    assert f"http://{PROXY_ADDRESS}" in guide                        # ports table
    for text in (conf, infra, guide):
        assert "127.0.0.1:8080" not in text and "localhost:8080" not in text
    assert not re.search(r"listen\s+(0\.0\.0\.0|\[::\]|\d+;)", _nginx())     # localhost only, never all interfaces


# =========================================================================== CI workflow (static checks)
def test_ci_workflow_runs_tests_and_build_without_secrets_ai_or_deployment():
    text = CI.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert set(wf["jobs"]) == {"backend", "frontend", "dependency-audit"}   # SV-07: + vulnerability audit
    assert wf["permissions"] == {"contents": "read"}
    assert wf["env"]["SYNERGY_AI_PROVIDER"] == "disabled" and wf["env"]["SYNERGY_AI_ALLOW_LIVE"] == "0"
    assert "secrets." not in text and "GEMINI_API_KEY" not in text and "GROQ_API_KEY" not in text
    assert not re.search(r"deploy|docker push|gh-pages|ssh ", text.split("jobs:", 1)[1], re.I)
    backend = [s.get("run", s.get("uses", "")) for s in wf["jobs"]["backend"]["steps"]]
    frontend = [s.get("run", s.get("uses", "")) for s in wf["jobs"]["frontend"]["steps"]]
    assert backend[0].startswith("actions/checkout") and backend[1].startswith("actions/setup-python")
    assert any("requirements-dev.txt" in s for s in backend) and backend[-1].startswith("python -m pytest")
    assert frontend[1].startswith("actions/setup-node")
    assert frontend[2:] == ["npm ci", "npm test", "npm run build"]
    for job in ("backend", "frontend"):
        assert (PROJECT / wf["jobs"][job]["defaults"]["run"]["working-directory"]).is_dir()


# =========================================================================== SV-07: CI supply-chain hardening
def test_sv07_ci_actions_pinned_to_commit_shas_without_persisted_credentials():
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = [s for job in wf["jobs"].values() for s in job["steps"]]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses and all(re.fullmatch(r"actions/[\w-]+@[0-9a-f]{40}", u) for u in uses), uses
    for s in steps:
        if s.get("uses", "").startswith("actions/checkout@"):
            assert s["with"]["persist-credentials"] is False
    assert all("permissions" not in job for job in wf["jobs"].values())      # only the read-only workflow default


def test_sv07_ci_installs_python_locks_with_hashes_and_audits_dependencies():
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    backend = " ".join(s.get("run", "") for s in wf["jobs"]["backend"]["steps"])
    assert "--require-hashes -r requirements-dev.txt" in backend and "pip check" in backend
    audit = " ".join(s.get("run", "") for s in wf["jobs"]["dependency-audit"]["steps"])
    assert "npm audit --omit=dev --audit-level=high" in audit
    for lock in ("dashboard/backend/requirements-dev.txt", "automation/requirements-dev.txt"):
        assert f"pip_audit --require-hashes --disable-pip -r {lock}" in audit
    assert "upgrade pip" not in backend + audit                           # tool versions are pinned


def test_sv07_python_locks_are_exact_and_hashed():
    for folder in (DASHBOARD / "backend", PROJECT / "automation"):
        for name in ("requirements.txt", "requirements-dev.txt"):
            text = (folder / name).read_text(encoding="utf-8")
            reqs = re.findall(r"^([A-Za-z0-9][\w.-]*)(==|>=|<=|~=|>|<)?([^\s\;]*)", text, re.M)
            assert reqs and all(op == "==" for _, op, _ in reqs), (folder, name)
            assert text.count("--hash=sha256:") >= len(reqs)
        assert (folder / "requirements.in").is_file()
    assert "pandas" not in (PROJECT / "automation" / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^httpx==", (DASHBOARD / "backend" / "requirements.txt").read_text(encoding="utf-8"), re.M)
