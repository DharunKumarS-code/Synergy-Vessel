"""Phase 8.2 -- passwords, sessions, roles, reviewer identity, audit chain, account admin, CLI. Offline only."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.ai.providers import MockConditionProvider
from app.auth import passwords
from app.auth.audit import AuditLog, entry_hash
from app.auth.service import LOCKOUT_SECONDS, MAX_FAILURES
from app.cli import create_admin
from app.config import Settings
from app.main import create_app
from conftest import TEST_PASSWORD, sign_in

PW = TEST_PASSWORD


@pytest.fixture
def app_client(data_root, tmp_path):
    s = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses", import_dir=tmp_path / "imports",
                 auth_dir=tmp_path / "auth")
    with TestClient(create_app(s, condition_provider=MockConditionProvider("stable"))) as c:
        yield c


def login(c, username, password=PW):
    return c.post("/api/auth/login", json={"username": username, "password": password})


def bearer(r):
    return {"Authorization": f"Bearer {r.json()['token']}"}


def add_user(c, username, role="reviewer", active=True):
    u = c.app.state.auth.users.create(username, PW, role, None)
    if not active:
        c.app.state.auth.users.update(u.user_id, active=False)
    return u


def pair(c):
    from test_ai_analysis import rid

    return {"earlier_record_id": rid(c, "W30-0001"), "later_record_id": rid(c, "W34-0001"), "acknowledge_warnings": True}


def audit(c):
    return [json.loads(l) for l in (c.app.state.auth.audit.path).read_text(encoding="utf-8").splitlines()]


# ===================================================================== passwords
def test_hashing_uses_unique_salts_and_verifies():
    a, b = passwords.hash_password("long enough password"), passwords.hash_password("long enough password")
    assert a != b and a.startswith("pbkdf2_sha256$") and "long enough" not in a
    assert passwords.verify_password("long enough password", a)
    assert not passwords.verify_password("long enough passworD", a)
    assert not passwords.verify_password("x", "garbage") and not passwords.verify_password("x", "md5$1$a$b")


def test_production_iteration_count_is_strong():
    import importlib

    fresh = importlib.reload(passwords)  # the module constant, not the test override
    try:
        assert fresh.ITERATIONS >= 600_000
    finally:
        fresh.ITERATIONS = 1_000


@pytest.mark.parametrize("pw,ok", [("short", False), ("a" * 12, True), (" padded password ", False), ("alice-is-great-1", False)])
def test_password_policy(pw, ok):
    assert (passwords.password_problem(pw, "alice") is None) == ok


def test_account_file_never_holds_plaintext(app_client):
    add_user(app_client, "alice")
    raw = app_client.app.state.auth.users.path.read_text(encoding="utf-8")
    assert PW not in raw and "pbkdf2_sha256$" in raw


# ===================================================================== authentication & sessions
def test_login_me_logout(app_client):
    add_user(app_client, "alice")
    r = login(app_client, "Alice ")  # usernames are case/space-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["username"] == "alice" and body["idle_timeout_seconds"] == 8 * 3600
    assert "password_hash" not in json.dumps(body)
    h = bearer(r)
    assert app_client.get("/api/auth/me", headers=h).json()["role"] == "reviewer"
    assert app_client.get("/api/auth/session", headers=h).json()["authenticated"] is True
    assert app_client.post("/api/auth/logout", headers=h).status_code == 204
    assert app_client.get("/api/auth/me", headers=h).status_code == 401  # invalidated
    assert app_client.get("/api/auth/session", headers=h).json() == {"authenticated": False, "user": None}


@pytest.mark.parametrize("username,password", [("alice", "wrong password!!"), ("nobody", PW)])
def test_invalid_credentials_share_one_message(app_client, username, password):
    add_user(app_client, "alice")
    r = login(app_client, username, password)
    assert r.status_code == 401 and r.json() == {"detail": "The username or password is incorrect.", "code": "invalid_credentials"}


def test_disabled_account_cannot_sign_in_and_loses_sessions(app_client):
    admin = sign_in(app_client, "admin")
    u = add_user(app_client, "bob")
    h = bearer(login(app_client, "bob"))
    assert app_client.patch(f"/api/admin/users/{u.user_id}", json={"active": False}).status_code == 200
    assert app_client.get("/api/auth/me", headers=h).status_code == 401
    r = login(app_client, "bob")
    assert r.status_code == 403 and r.json()["code"] == "account_disabled"
    assert admin["user"]["role"] == "admin"


def test_lockout_after_repeated_failures(app_client):
    add_user(app_client, "alice")
    auth = app_client.app.state.auth
    t = [1_000_000.0]
    auth.clock = lambda: t[0]
    for _ in range(MAX_FAILURES):
        assert login(app_client, "alice", "wrong password!!").status_code == 401
    r = login(app_client, "alice")  # even the right password is refused while locked
    assert r.status_code == 429 and r.json()["code"] == "account_locked"
    t[0] += LOCKOUT_SECONDS + 1
    assert login(app_client, "alice").status_code == 200
    events = [e["event"] for e in audit(app_client)]
    assert events.count("login_failed") == MAX_FAILURES and "login_locked" in events and events[-1] == "login_succeeded"


def test_idle_expiry_and_activity_refresh(app_client):
    add_user(app_client, "alice")
    auth = app_client.app.state.auth
    t = [1_000_000.0]
    auth.clock = lambda: t[0]
    h = bearer(login(app_client, "alice"))
    t[0] += 8 * 3600 - 10
    assert app_client.get("/api/auth/me", headers=h).status_code == 200  # activity refreshes the idle timer
    t[0] += 8 * 3600 - 10
    assert app_client.get("/api/auth/me", headers=h).status_code == 200
    t[0] += 8 * 3600 + 1
    r = app_client.get("/api/auth/me", headers=h)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer not-a-real-token", "Basic abc", "Bearer ", "token"])
def test_invalid_tokens_are_rejected(app_client, header):
    assert app_client.get("/api/auth/me", headers={"Authorization": header}).status_code == 401


def test_tokens_are_never_stored_or_audited(app_client):
    add_user(app_client, "alice")
    token = login(app_client, "alice").json()["token"]
    app_client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    for p in app_client.app.state.auth.users.dir.iterdir():
        text = p.read_text(encoding="utf-8")
        assert token not in text and PW not in text
    assert token not in repr(app_client.app.state.auth._sessions)


# ===================================================================== authorisation
# SV-02: only health and the session check stay public; every data view needs an account
# (the full matrix is in test_sv02_access.py).
PUBLIC = ["/api/health", "/api/auth/session"]


@pytest.mark.parametrize("url", PUBLIC)
def test_only_health_and_session_stay_public(app_client, url):
    assert app_client.get(url).status_code == 200


@pytest.mark.parametrize("url", ["/api/vessels", "/api/inspections", "/api/ai-analysis/history",
                                 "/api/ai-analysis/status", "/api/imports", "/api/reports/preview"])
def test_data_views_need_sign_in(app_client, url):
    r = app_client.get(url)
    assert r.status_code == 401 and r.json()["code"] == "not_authenticated"


def test_protected_actions_require_sign_in(app_client):
    for method, url, kw in [("post", "/api/ai-analysis/compare", {"json": pair(app_client)}),
                            ("post", "/api/ai-analysis/CA-20260101-0000000000/review", {"json": {"status": "APPROVED"}}),
                            ("post", "/api/imports/preview?filename=a.xlsx", {"content": b"PK"}),
                            ("post", f"/api/imports?filename=a.xlsx&fingerprint={'a' * 64}", {"content": b"PK"}),
                            ("get", "/api/admin/users", {}), ("post", "/api/admin/users", {"json": {}}),
                            ("get", "/api/admin/audit/verify", {}), ("get", "/api/auth/me", {})]:
        r = getattr(app_client, method)(url, **kw)
        assert r.status_code == 401 and r.json()["code"] == "not_authenticated", (url, r.text)
    assert app_client.app.state.analysis.store.all() == []  # nothing was run


def test_reviewer_can_analyse_and_review_but_not_administer(app_client):
    sign_in(app_client, "reviewer")
    r = app_client.post("/api/ai-analysis/compare", json=pair(app_client))
    assert r.status_code == 200
    aid = r.json()["analysis"]["analysis_id"]
    # SV-03: a reviewer may decide on their own analysis, but not approve it (another reviewer/admin must).
    assert app_client.post(f"/api/ai-analysis/{aid}/review", json={"status": "REJECTED"}).status_code == 200
    for method, url, kw in [("get", "/api/admin/users", {}),
                            ("post", "/api/admin/users", {"json": {"username": "eve", "password": PW}}),
                            ("get", "/api/admin/audit", {}),
                            ("post", "/api/imports/preview?filename=a.xlsx", {"content": b"PK"})]:
        r = getattr(app_client, method)(url, **kw)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", url


def test_admin_has_reviewer_permissions(app_client):
    sign_in(app_client, "admin")
    assert app_client.post("/api/ai-analysis/compare", json=pair(app_client)).status_code == 200
    assert app_client.get("/api/admin/users").status_code == 200
    assert app_client.post("/api/imports/preview?filename=a.xlsx", content=b"PK").status_code == 400  # reached the parser


def test_role_change_applies_to_existing_access(app_client):
    sign_in(app_client, "admin")
    u = add_user(app_client, "carol", role="admin")
    h = bearer(login(app_client, "carol"))
    assert app_client.get("/api/admin/users", headers=h).status_code == 200
    app_client.patch(f"/api/admin/users/{u.user_id}", json={"role": "reviewer"})
    assert app_client.get("/api/admin/users", headers=h).status_code == 401  # sessions revoked on role change
    h = bearer(login(app_client, "carol"))
    assert app_client.get("/api/admin/users", headers=h).status_code == 403


# ===================================================================== reviewer identity
def test_review_identity_comes_from_session_not_request(app_client):
    me = sign_in(app_client, "reviewer")["user"]
    aid = app_client.post("/api/ai-analysis/compare", json=pair(app_client)).json()["analysis"]["analysis_id"]
    r = app_client.post(f"/api/ai-analysis/{aid}/review", json={"status": "REJECTED", "reviewer": "Chief Engineer", "note": "n"})
    h = r.json()["review"]["history"][-1]
    assert h["reviewer"] == me["display_name"] != "Chief Engineer"
    # SV-02: a reviewer's response carries no account identifiers; the stored record keeps them.
    assert (h["reviewer_id"], h["reviewer_username"], h["reviewer_role"]) == (None, None, "reviewer")
    stored = app_client.app.state.analysis.store.get(aid).review.history[-1]
    assert (stored.reviewer_id, stored.reviewer_username) == (me["user_id"], me["username"])
    ev = [e for e in audit(app_client) if e["event"] == "analysis_reviewed"][-1]
    assert ev["actor"]["user_id"] == me["user_id"] and ev["details"] == {"decision": "REJECTED", "previous_status": "PENDING_REVIEW",
                                                                "note_provided": True}
    assert any(e["event"] == "ai_analysis_run" and e["target"] == aid for e in audit(app_client))


def test_legacy_review_events_still_load(app_client, tmp_path):
    sign_in(app_client, "reviewer")
    a = app_client.post("/api/ai-analysis/compare", json=pair(app_client)).json()["analysis"]
    path = tmp_path / "analyses" / f"{a['analysis_id']}.json"
    data = json.loads(path.read_text())
    data["review"] = {"status": "APPROVED", "history": [{"status": "APPROVED", "reviewer": "Inspector A", "note": None,
                                                         "at": "2026-11-01T10:00:00+00:00"}]}
    path.write_text(json.dumps(data))
    app_client.app.state.analysis.store.load()
    got = app_client.get(f"/api/ai-analysis/{a['analysis_id']}").json()["review"]["history"][0]
    assert got["reviewer"] == "Inspector A" and got["reviewer_id"] is None


# ===================================================================== user administration
def test_user_creation_validation_and_no_hash_exposure(app_client):
    sign_in(app_client, "admin")
    r = app_client.post("/api/admin/users", json={"username": "Dave", "password": PW, "role": "reviewer", "display_name": "Dave R"})
    assert r.status_code == 201 and r.json()["username"] == "dave" and "password_hash" not in r.json()
    assert app_client.post("/api/admin/users", json={"username": "dave", "password": PW}).json()["code"] == "username_taken"
    assert app_client.post("/api/admin/users", json={"username": "x", "password": PW}).json()["code"] == "invalid_username"
    assert app_client.post("/api/admin/users", json={"username": "erin", "password": "short"}).json()["code"] == "weak_password"
    assert app_client.post("/api/admin/users", json={"username": "erin", "password": PW, "role": "root"}).status_code == 422
    listing = app_client.get("/api/admin/users").json()
    assert "password_hash" not in json.dumps(listing) and "pbkdf2" not in json.dumps(listing)
    ev = [e for e in audit(app_client) if e["event"] == "user_created"][-1]
    assert ev["details"]["username"] == "dave" and PW not in json.dumps(ev)


def test_last_admin_cannot_be_removed_or_demoted(app_client):
    me = sign_in(app_client, "admin")["user"]
    auth = app_client.app.state.auth
    from app.auth.users import AccountError

    with pytest.raises(AccountError) as exc:
        auth.users.update(me["user_id"], role="reviewer")
    assert exc.value.code == "last_admin"
    with pytest.raises(AccountError):
        auth.users.update(me["user_id"], active=False)
    # through the API an admin can never disable/demote themselves
    r = app_client.patch(f"/api/admin/users/{me['user_id']}", json={"active": False})
    assert r.status_code == 409 and r.json()["code"] == "self_change"
    # with a second admin, the first may be demoted by the other
    other = add_user(app_client, "frank", role="admin")
    h = bearer(login(app_client, "frank"))
    assert app_client.patch(f"/api/admin/users/{me['user_id']}", json={"role": "reviewer"}, headers=h).status_code == 200
    r = app_client.patch(f"/api/admin/users/{other.user_id}", json={"active": False}, headers=h)
    assert r.json()["code"] == "self_change"


def test_role_enable_and_password_changes_are_audited(app_client):
    sign_in(app_client, "admin")
    u = add_user(app_client, "gina")
    p = f"/api/admin/users/{u.user_id}"
    assert app_client.patch(p, json={"role": "admin"}).json()["role"] == "admin"
    assert app_client.patch(p, json={"active": False}).json()["active"] is False
    assert app_client.patch(p, json={"active": True}).json()["active"] is True
    assert app_client.patch(p, json={"password": "a brand new password"}).status_code == 200
    assert login(app_client, "gina").status_code == 401 and login(app_client, "gina", "a brand new password").status_code == 200
    events = [e for e in audit(app_client) if e["target"] == u.user_id]
    assert [e["event"] for e in events] == ["user_role_changed", "user_disabled", "user_enabled", "user_updated"]
    assert events[-1]["details"]["sign_in_reset"] is True and "a brand new password" not in json.dumps(events)
    assert app_client.patch("/api/admin/users/USR-000000000000", json={"active": False}).status_code == 404


# ===================================================================== audit chain
def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    log = AuditLog(tmp_path)
    for i in range(5):
        log.record("test_event", actor={"user_id": "u"}, target=str(i), details={"i": i})
    assert log.verify().valid and log.verify().entries == 5
    lines = log.path.read_text(encoding="utf-8").splitlines()

    def check(new_lines, expect):
        log.path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        r = log.verify()
        assert not r.valid and any(expect in p for p in r.problems), r.problems
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    modified = json.loads(lines[2]); modified["details"]["i"] = 99
    check(lines[:2] + [json.dumps(modified)] + lines[3:], "was modified")
    check(lines[:2] + lines[3:], "removed")                              # entry removed from the middle
    check([lines[0], lines[2], lines[1]] + lines[3:], "reordered")        # entries swapped
    check(lines[:-1], "removed from the end")                            # newest entry truncated
    forged = json.loads(lines[2]); forged["details"]["i"] = 99; forged["hash"] = entry_hash(forged)
    check(lines[:2] + [json.dumps(forged)] + lines[3:], "does not chain")  # re-hashed forgery breaks the next link
    assert log.verify().valid


def test_audit_redacts_sensitive_values(tmp_path):
    log = AuditLog(tmp_path)
    e = log.record("x", actor={"user_id": "u", "token": "abc"},
                   details={"password": "hunter2hunter2", "api_key": "AIza-secret", "nested": {"Authorization": "Bearer z"}, "ok": 1})
    text = log.path.read_text(encoding="utf-8")
    for secret in ("hunter2hunter2", "AIza-secret", "Bearer z", "abc"):
        assert secret not in text
    assert e["details"]["ok"] == 1 and e["details"]["password"] == "[redacted]" and log.verify().valid


def test_admin_audit_endpoints(app_client):
    sign_in(app_client, "admin")
    v = app_client.get("/api/admin/audit/verify").json()
    assert v["valid"] and v["entries"] >= 1
    entries = app_client.get("/api/admin/audit", params={"event": "login_succeeded"}).json()["entries"]
    assert entries and all(e["event"] == "login_succeeded" for e in entries)


# ===================================================================== CLI
def answers(*values):
    it = iter(values)
    return lambda prompt="": next(it)


def test_cli_create_admin(tmp_path):
    out = []
    rc = create_admin(tmp_path, ask=answers("Admin.One", "Chief Officer"), ask_secret=answers(PW, PW), out=out.append)
    assert rc == 0
    from app.auth.users import UserStore

    u = UserStore(tmp_path).by_username("admin.one")
    assert u.role == "admin" and u.display_name == "Chief Officer"
    assert PW not in "\n".join(out) and PW not in (tmp_path / "users.json").read_text()
    ev = AuditLog(tmp_path).entries()[0]
    assert ev["event"] == "user_created" and ev["details"]["via"] == "cli"
    # duplicate username: nothing changes
    rc = create_admin(tmp_path, ask=answers("admin.one"), ask_secret=answers(), out=out.append)
    assert rc == 3 and len(UserStore(tmp_path).all()) == 1


def test_cli_rejects_mismatch_and_weak_passwords(tmp_path):
    out = []
    rc = create_admin(tmp_path, ask=answers("chief", ""), ask_secret=answers("short", PW, PW + "x", "another one!!", "nope"),
                      out=out.append)
    assert rc == 4 and not (tmp_path / "users.json").exists()
    assert any("at least" in o for o in out) and any("do not match" in o for o in out)


def test_no_default_accounts_exist(app_client):
    assert app_client.app.state.auth.users.all() == []
    assert login(app_client, "admin", "admin").status_code == 401


def test_cli_cancel_creates_nothing(tmp_path, monkeypatch):
    from app import cli

    monkeypatch.setenv("SYNERGY_AUTH_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert cli.main(["create-admin"]) == 130
    assert not (tmp_path / "users.json").exists()
