"""SV-02 -- public read access: authenticated reads, the viewer role, photo cookie, identity restrictions, API docs.

Offline only: isolated temporary folders, the mock AI provider, no network.
"""
from __future__ import annotations

import re
from http.cookies import SimpleCookie

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.ai.providers import MockConditionProvider
from app.auth.deps import MEDIA_COOKIE, require_admin, require_media_user, require_reviewer, require_user
from app.config import Settings, _access_settings
from app.main import create_app
from conftest import TEST_PASSWORD

GUARDS = {require_user, require_admin, require_reviewer, require_media_user}
# The only routes anyone may call without an account (all other routes must carry an explicit guard).
PUBLIC_ROUTES = {("GET", "/api/health"), ("GET", "/api/auth/session"),
                 ("POST", "/api/auth/login"), ("POST", "/api/auth/logout")}


# =========================================================================== fixtures & helpers
@pytest.fixture
def app(data_root, tmp_path):
    s = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses", import_dir=tmp_path / "imports",
                 auth_dir=tmp_path / "auth")
    with TestClient(create_app(s, condition_provider=MockConditionProvider("stable"))) as c:
        yield c


def account(c, username, role):
    if c.app.state.auth.users.by_username(username) is None:
        c.app.state.auth.users.create(username, TEST_PASSWORD, role, f"Test {role.title()}")  # no username inside
    return username


def login(c, username):
    r = c.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return r


def headers(c, role, username=None):
    """Bearer header for a fresh session of a (new) account with this role. Cookies are cleared so the photo
    cookie of this login never leaks into other checks."""
    r = login(c, account(c, username or f"sv-{role}", role))
    c.cookies.clear()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def image_id(c):
    return c.app.state.store.records[0].image_id


def pair(c):
    from test_ai_analysis import rid
    return {"earlier_record_id": rid(c, "W30-0001"), "later_record_id": rid(c, "W34-0001"), "acknowledge_warnings": True}


def analysis(c, reviewer_headers):
    r = c.post("/api/ai-analysis/compare", json=pair(c), headers=reviewer_headers)
    assert r.status_code == 200, r.text
    return r.json()["analysis"]["analysis_id"]


def concrete(c, path):
    """A real URL for a route path (path parameters filled with plausible values)."""
    from test_ai_analysis import rid
    return (path.replace("{image_id}", image_id(c)).replace("{record_id}", rid(c, "W30-0001"))
            .replace("{analysis_id}", "CA-20260101-0000000000").replace("{import_id}", "IMP-20260101-000000-abcdef")
            .replace("{group_id}", "g1").replace("{synonym_id}", "SYN-1").replace("{user_id}", "USR-000000000000")
            .replace("{backup_id}", "B1"))


def all_routes(c):
    """(full path, methods, guards) for every API route, including those inside included routers (this FastAPI
    version wraps them in _IncludedRouter, so app.routes alone does not list them)."""
    def walk(routes, prefix="", include_guards=frozenset()):
        for r in routes:
            if isinstance(r, APIRoute):
                yield prefix + r.path, set(r.methods) - {"HEAD", "OPTIONS"}, guards_of(r.dependant) | include_guards
            elif hasattr(r, "original_router") and hasattr(r, "include_context"):
                ctx = r.include_context
                extra = frozenset(d.dependency for d in (ctx.dependencies or ()) if d.dependency in GUARDS)
                yield from walk(r.original_router.routes, prefix + (ctx.prefix or ""), include_guards | extra)
    rows = list(walk(c.app.routes))
    assert len(rows) >= 40, "route discovery found too few routes -- the walker needs updating"
    return rows


def guards_of(dependant) -> set:
    found, stack = set(), [dependant]
    while stack:
        d = stack.pop()
        if d.call in GUARDS:
            found.add(d.call)
        stack.extend(d.dependencies)
    return found


# =========================================================================== route inventory
def test_every_route_has_an_explicit_guard_or_is_on_the_public_list(app):
    """Fails when a new route is added without access control. Add a guard (RequireUser / RequireReviewer /
    RequireAdmin / RequireMediaUser, or a router-level dependency) -- or, after review, add it to PUBLIC_ROUTES."""
    unguarded = [f"{m} {path}" for path, methods, guards in all_routes(app) for m in methods
                 if (m, path) not in PUBLIC_ROUTES and not guards]
    assert unguarded == []


def test_public_routes_are_exactly_the_reviewed_ones(app):
    public = {(m, path) for path, methods, guards in all_routes(app) for m in methods if not guards}
    assert public == PUBLIC_ROUTES


def test_every_protected_get_route_refuses_anonymous_requests(app):
    checked = 0
    for path, methods, _ in all_routes(app):
        if "GET" not in methods or ("GET", path) in PUBLIC_ROUTES:
            continue
        resp = app.get(concrete(app, path))
        assert resp.status_code == 401, (path, resp.status_code, resp.text[:200])
        assert resp.json()["code"] == "not_authenticated"
        assert "Traceback" not in resp.text
        checked += 1
    assert checked >= 25


VIEWER_READS = ["/api/vessels", "/api/locations", "/api/inspections", "/api/inspections/search?location=Main%20Deck",
                "/api/inspections/dates?location=Main%20Deck",
                "/api/inspections/images?location=Main%20Deck&date=2026-08-02",
                "/api/ai-analysis/status", "/api/ai-analysis/history", "/api/reports/preview",
                "/api/reports/inspections.csv", "/api/auth/me"]
ADMIN_ONLY = ["/api/validation", "/api/imports", "/api/imports/location-ai", "/api/admin/users", "/api/admin/audit",
              "/api/admin/audit/verify", "/api/locations/synonyms", "/api/locations/review"]  # review: SV-02 F1


@pytest.mark.parametrize("role", ["viewer", "reviewer", "admin"])
def test_every_role_can_read_application_data(app, role):
    h = headers(app, role)
    for url in VIEWER_READS:
        assert app.get(url, headers=h).status_code == 200, url
    rid = app.app.state.store.records[0].record_id
    assert app.get(f"/api/inspections/{rid}", headers=h).status_code == 200
    assert app.get(f"/api/images/{image_id(app)}", headers=h).status_code == 200


@pytest.mark.parametrize("role", ["viewer", "reviewer"])
def test_viewers_and_reviewers_cannot_reach_admin_endpoints(app, role):
    h = headers(app, role)
    for url in ADMIN_ONLY:
        r = app.get(url, headers=h)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", url


def test_admin_reaches_admin_endpoints(app):
    h = headers(app, "admin")
    for url in ADMIN_ONLY:
        assert app.get(url, headers=h).status_code == 200, url


# =========================================================================== viewer role
def test_viewer_cannot_run_analyses_or_review(app):
    viewer, reviewer = headers(app, "viewer"), headers(app, "reviewer")
    r = app.post("/api/ai-analysis/compare", json=pair(app), headers=viewer)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert app.app.state.analysis.store.find() == []                       # nothing was run or stored
    aid = analysis(app, reviewer)
    for status in ("APPROVED", "REJECTED", "NEEDS_REASSESSMENT"):
        r = app.post(f"/api/ai-analysis/{aid}/review", json={"status": status, "note": "viewer try"}, headers=viewer)
        assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert app.app.state.analysis.store.get(aid).review.status.value == "PENDING_REVIEW"
    assert app.app.state.analysis.store.get(aid).review.history == []


def test_viewer_role_can_be_created_and_assigned_by_an_admin_only(app):
    admin = headers(app, "admin")
    r = app.post("/api/admin/users", headers=admin,
                 json={"username": "vera", "password": TEST_PASSWORD, "role": "viewer", "display_name": "Vera"})
    assert r.status_code in (200, 201), r.text
    assert r.json()["role"] == "viewer"
    user_id = r.json()["user_id"]
    me = login(app, "vera").json()["user"]
    assert me["role"] == "viewer"
    rev = headers(app, "reviewer")
    assert app.post("/api/admin/users", headers=rev, json={"username": "x-new", "password": TEST_PASSWORD,
                                                             "role": "viewer"}).status_code == 403
    assert app.patch(f"/api/admin/users/{user_id}", headers=admin, json={"role": "reviewer"}).json()["role"] == "reviewer"
    assert app.patch(f"/api/admin/users/{user_id}", headers=admin, json={"role": "viewer"}).json()["role"] == "viewer"
    bad = app.patch(f"/api/admin/users/{user_id}", headers=admin, json={"role": "owner"})
    assert bad.status_code == 422


def test_admin_cannot_demote_themself_to_viewer(app):
    admin = headers(app, "admin", "sv-boss")
    me = app.get("/api/auth/me", headers=admin).json()
    r = app.patch(f"/api/admin/users/{me['user_id']}", headers=admin, json={"role": "viewer"})
    assert r.status_code in (400, 409) and app.app.state.auth.users.get(me["user_id"]).role == "admin"


def test_viewer_session_reports_role(app):
    h = headers(app, "viewer")
    body = app.get("/api/auth/session", headers=h).json()
    assert body["authenticated"] is True and body["user"]["role"] == "viewer"


# =========================================================================== public endpoints
def test_health_is_minimal_for_anonymous_and_detailed_when_signed_in(app):
    anon = app.get("/api/health")
    assert anon.status_code == 200 and anon.json() == {"status": "ok"}
    full = app.get("/api/health", headers=headers(app, "viewer")).json()
    assert full["status"] == "ok" and "records" in full


def test_session_check_is_public_and_never_401(app):
    r = app.get("/api/auth/session")
    assert r.status_code == 200 and r.json() == {"authenticated": False, "user": None}
    r = app.get("/api/auth/session", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 200 and r.json()["authenticated"] is False
    assert MEDIA_COOKIE not in r.cookies


def test_validation_never_exposes_the_data_folder(app):
    body = app.get("/api/validation", headers=headers(app, "admin"))
    assert "data_root" not in body.json()
    assert str(app.app.state.store.report.data_root) not in body.text


def test_no_absolute_server_path_in_any_viewer_response(app, data_root):
    h = headers(app, "viewer")
    needles = {str(data_root), str(data_root).replace("\\", "/"), str(data_root).replace("\\", "\\\\")}
    for url in VIEWER_READS:
        text = app.get(url, headers=h).text
        assert not any(n in text for n in needles), url


# =========================================================================== photo cookie
def set_cookie_header(resp) -> str:
    values = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie" and v.startswith(MEDIA_COOKIE + "=")]
    assert len(values) == 1, values
    return values[0]


def test_login_sets_a_hardened_photo_cookie(app):
    r = login(app, account(app, "sv-cookie", "viewer"))
    header = set_cookie_header(r)
    attrs = [a.strip().lower() for a in header.split(";")]
    assert "httponly" in attrs and "samesite=strict" in attrs and "path=/api/images" in attrs
    assert not any(a.startswith(("expires=", "max-age=")) for a in attrs)   # a browser-session cookie only
    assert "secure" not in attrs                                            # plain-HTTP local development
    value = SimpleCookie(header)[MEDIA_COOKIE].value
    assert value and value != r.json()["token"]                              # never the session token
    assert value not in r.text                                               # and never in the JSON body


def test_cookie_is_secure_over_https_or_when_configured(data_root, tmp_path):
    s = Settings(data_root=data_root, analysis_dir=tmp_path / "a", auth_dir=tmp_path / "auth", cookie_secure=True)
    with TestClient(create_app(s)) as c:
        header = set_cookie_header(login(c, account(c, "sv-sec", "viewer")))
    assert "secure" in [a.strip().lower() for a in header.split(";")]
    s2 = Settings(data_root=data_root, analysis_dir=tmp_path / "b", auth_dir=tmp_path / "auth2")
    with TestClient(create_app(s2), base_url="https://testserver") as c:
        header = set_cookie_header(login(c, account(c, "sv-sec", "viewer")))
    assert "secure" in [a.strip().lower() for a in header.split(";")]


def test_images_need_a_session(app):
    url = f"/api/images/{image_id(app)}"
    app.cookies.clear()
    r = app.get(url)
    assert r.status_code == 401 and r.json()["code"] == "not_authenticated"
    assert r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("role", ["viewer", "reviewer", "admin"])
def test_each_role_loads_photos_with_the_cookie_alone(app, role):
    app.cookies.clear()
    login(app, account(app, f"sv-img-{role}", role))                        # the browser keeps the cookie
    r = app.get(f"/api/images/{image_id(app)}")                              # like an <img>: no Authorization header
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    assert "no-cache" in r.headers["cache-control"] and "private" in r.headers["cache-control"]


def test_photo_cookie_cannot_call_the_api(app):
    app.cookies.clear()
    header = set_cookie_header(login(app, account(app, "sv-scope", "viewer")))
    media = SimpleCookie(header)[MEDIA_COOKIE].value
    app.cookies.clear()
    assert app.get("/api/vessels", headers={"Authorization": f"Bearer {media}"}).status_code == 401
    assert app.get("/api/vessels", cookies={MEDIA_COOKIE: media}).status_code == 401
    assert app.get("/api/auth/session", cookies={MEDIA_COOKIE: media}).json()["authenticated"] is False


def test_query_parameters_never_authenticate_photos(app):
    app.cookies.clear()
    r = login(app, account(app, "sv-query", "viewer"))
    token = r.json()["token"]
    media = SimpleCookie(set_cookie_header(r))[MEDIA_COOKIE].value
    app.cookies.clear()
    for q in (f"token={token}", f"access_token={token}", f"{MEDIA_COOKIE}={media}", f"media={media}"):
        assert app.get(f"/api/images/{image_id(app)}?{q}").status_code == 401


def test_logout_clears_the_cookie_and_ends_photo_access(app):
    app.cookies.clear()
    r = login(app, account(app, "sv-out", "viewer"))
    media = SimpleCookie(set_cookie_header(r))[MEDIA_COOKIE].value
    url = f"/api/images/{image_id(app)}"
    assert app.get(url).status_code == 200
    out = app.post("/api/auth/logout", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert out.status_code == 204
    cleared = [a.strip().lower() for a in set_cookie_header(out).split(";")]
    assert any(a.startswith("max-age=0") or a.startswith("expires=") for a in cleared)
    assert "path=/api/images" in cleared
    assert MEDIA_COOKIE not in app.cookies
    assert app.get(url, cookies={MEDIA_COOKIE: media}).status_code == 401   # a copied old cookie is dead too


def test_expired_session_rejects_photos(app):
    app.cookies.clear()
    r = login(app, account(app, "sv-exp", "viewer"))
    url = f"/api/images/{image_id(app)}"
    assert app.get(url).status_code == 200
    auth = app.app.state.auth
    real_clock = auth.clock
    try:
        auth.clock = lambda: real_clock() + auth.idle_seconds + 5
        assert app.get(url).status_code == 401
        assert app.get("/api/vessels", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code == 401
    finally:
        auth.clock = real_clock


def test_photo_views_do_not_extend_the_session(app):
    app.cookies.clear()
    r = login(app, account(app, "sv-idle", "viewer"))
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    auth = app.app.state.auth
    real_clock, start = auth.clock, auth.clock()
    try:
        auth.clock = lambda: start + auth.idle_seconds - 10
        assert app.get(f"/api/images/{image_id(app)}").status_code == 200   # via the cookie only
        auth.clock = lambda: start + auth.idle_seconds + 5
        assert app.get("/api/vessels", headers=h).status_code == 401       # still idle since login
    finally:
        auth.clock = real_clock


def test_deactivated_account_loses_photo_access(app):
    app.cookies.clear()
    login(app, account(app, "sv-off", "viewer"))
    url = f"/api/images/{image_id(app)}"
    assert app.get(url).status_code == 200
    user = app.app.state.auth.users.by_username("sv-off")
    app.app.state.auth.users.update(user.user_id, active=False)
    assert app.get(url).status_code == 401


def test_session_check_refreshes_the_cookie_and_replaces_the_old_one(app):
    app.cookies.clear()
    r = login(app, account(app, "sv-refresh", "viewer"))
    first = SimpleCookie(set_cookie_header(r))[MEDIA_COOKIE].value
    s = app.get("/api/auth/session", headers={"Authorization": f"Bearer {r.json()['token']}"})
    second = SimpleCookie(set_cookie_header(s))[MEDIA_COOKIE].value
    assert first != second
    app.cookies.clear()
    url = f"/api/images/{image_id(app)}"
    assert app.get(url, cookies={MEDIA_COOKIE: second}).status_code == 200
    assert app.get(url, cookies={MEDIA_COOKIE: first}).status_code == 401    # one live photo token per session


def test_session_tokens_and_photo_tokens_are_stored_only_as_digests(app):
    r = login(app, account(app, "sv-digest", "viewer"))
    media = SimpleCookie(set_cookie_header(r))[MEDIA_COOKIE].value
    state = repr(app.app.state.auth._sessions)
    assert r.json()["token"] not in state and media not in state


# =========================================================================== identity restrictions
def test_reviewers_and_viewers_see_no_account_identifiers(app):
    runner = headers(app, "reviewer", "sv-runner")
    other = headers(app, "reviewer", "sv-other")
    aid = analysis(app, runner)
    assert app.post(f"/api/ai-analysis/{aid}/review", json={"status": "APPROVED"}, headers=other).status_code == 200
    stored = app.app.state.analysis.store.get(aid)
    secrets = {stored.requested_by.user_id, stored.review.history[-1].reviewer_id, "sv-runner", "sv-other"}
    for h in (headers(app, "viewer"), other, runner):
        for url in (f"/api/ai-analysis/{aid}", "/api/ai-analysis/history"):
            r = app.get(url, headers=h)
            assert r.status_code == 200
            assert not any(s in r.text for s in secrets), url
        rec = app.get(f"/api/ai-analysis/{aid}", headers=h).json()
        assert rec["requested_by"] is None
        ev = rec["review"]["history"][-1]
        assert (ev["reviewer_id"], ev["reviewer_username"]) == (None, None)
        assert ev["reviewer"] and ev["reviewer_role"] == "reviewer"                # display name and role remain


def test_admins_keep_full_identity(app):
    runner = headers(app, "reviewer", "sv-runner2")
    aid = analysis(app, runner)
    rec = app.get(f"/api/ai-analysis/{aid}", headers=headers(app, "admin")).json()
    assert rec["requested_by"]["username"] == "sv-runner2" and rec["requested_by"]["user_id"].startswith("USR-")
    assert rec["requested_by_me"] is False


def test_requested_by_me_is_computed_per_user(app):
    runner = headers(app, "reviewer", "sv-runner3")
    other = headers(app, "reviewer", "sv-other3")
    aid = analysis(app, runner)
    assert app.get(f"/api/ai-analysis/{aid}", headers=runner).json()["requested_by_me"] is True
    assert app.get(f"/api/ai-analysis/{aid}", headers=other).json()["requested_by_me"] is False
    assert app.get(f"/api/ai-analysis/{aid}", headers=headers(app, "viewer")).json()["requested_by_me"] is False
    items = app.get("/api/ai-analysis/history", headers=runner).json()["items"]
    assert [i["requested_by_me"] for i in items if i["analysis_id"] == aid] == [True]
    # the self-approval rule still applies (SV-03) and is what the flag lets the UI explain
    r = app.post(f"/api/ai-analysis/{aid}/review", json={"status": "APPROVED"}, headers=runner)
    assert r.status_code == 403 and r.json()["code"] == "self_approval_not_allowed"


def test_compare_and_review_responses_are_restricted_too(app):
    runner = headers(app, "reviewer", "sv-runner4")
    r = app.post("/api/ai-analysis/compare", json=pair(app), headers=runner).json()
    assert r["analysis"]["requested_by"] is None and r["analysis"]["requested_by_me"] is True
    other = headers(app, "reviewer", "sv-other4")
    rv = app.post(f"/api/ai-analysis/{r['analysis']['analysis_id']}/review", json={"status": "REJECTED"}, headers=other)
    assert rv.json()["review"]["history"][-1]["reviewer_id"] is None


def test_stored_records_are_never_rewritten_by_reads(app):
    runner = headers(app, "reviewer", "sv-runner5")
    aid = analysis(app, runner)
    path = next(p for p in app.app.state.analysis.store.directory.rglob("*.json") if aid in p.name)
    before = path.read_bytes()
    for h in (headers(app, "viewer"), runner, headers(app, "admin")):
        app.get(f"/api/ai-analysis/{aid}", headers=h)
        app.get("/api/ai-analysis/history", headers=h)
    assert path.read_bytes() == before
    assert b"requested_by_me" not in before


# =========================================================================== API documentation
def test_docs_available_in_local_development(app):
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert app.get(url).status_code == 200, url


def test_docs_disabled_in_production(data_root, tmp_path):
    s = Settings(data_root=data_root, analysis_dir=tmp_path / "a", auth_dir=tmp_path / "auth", api_docs=False)
    with TestClient(create_app(s)) as c:
        for url in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            assert c.get(url).status_code == 404, url
        assert c.get("/api/health").status_code == 200


@pytest.mark.parametrize("env,docs", [({}, True), ({"SYNERGY_ENV": "production"}, False),
                                      ({"SYNERGY_ENV": "Production "}, False),
                                      ({"SYNERGY_ENV": "production", "SYNERGY_API_DOCS": "1"}, True),
                                      ({"SYNERGY_API_DOCS": "0"}, False), ({"SYNERGY_API_DOCS": "yes"}, True)])
def test_docs_setting(env, docs):
    assert _access_settings(env)["api_docs"] is docs


def test_cookie_secure_setting():
    assert _access_settings({})["cookie_secure"] is False
    assert _access_settings({"SYNERGY_COOKIE_SECURE": "1"})["cookie_secure"] is True


def test_media_cookie_name_and_path_are_stable():
    from app.auth.deps import MEDIA_COOKIE_PATH
    assert re.fullmatch(r"[a-z_]+", MEDIA_COOKIE) and MEDIA_COOKIE_PATH == "/api/images"
