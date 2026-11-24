"""Sessions, login protection and account administration (with audit events).

Sessions are held in memory only, keyed by SHA-256 of the bearer token -- the token itself is never
stored, logged or audited. A server restart therefore signs everyone out (by design). Each request
re-reads the account, so disabling an account or changing its role takes effect immediately.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .audit import AuditLog
from .passwords import dummy_verify, verify_password
from .users import AccountError, User, UserStore, normalise_username

MAX_FAILURES = 5            # failed logins per username within FAILURE_WINDOW ...
FAILURE_WINDOW = 15 * 60
LOCKOUT_SECONDS = 15 * 60   # ... lock that username for this long


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    display_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_review(self) -> bool:
        """SV-02: reviewers and administrators may run analyses and record review decisions; viewers may not."""
        return self.role in ("reviewer", "admin")

    def actor(self) -> dict:
        return {"user_id": self.user_id, "username": self.username, "role": self.role}


@dataclass
class _Session:
    user_id: str
    created: float
    last_seen: float
    # SV-02: SHA-256 of this session's photo token (HttpOnly cookie, path /api/images). It lives inside the session,
    # so logout, idle expiry and revocation -- which all delete the session -- end photo access too.
    media: Optional[str] = None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, users: UserStore, audit: AuditLog, *, idle_seconds: int = 8 * 3600,
                 clock: Callable[[], float] = time.time):
        self.users, self.audit = users, audit
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._sessions: dict[str, _Session] = {}
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ login / logout
    def login(self, username: str, password: str) -> tuple[str, User]:
        name = (username or "").strip().lower()[:64]
        now = self.clock()
        with self._lock:
            until = self._locked_until.get(name, 0)
        if until > now:
            self.audit.record("login_locked", target=name, details={"retry_after_seconds": int(until - now)})
            raise AccountError(429, "account_locked",
                               f"Too many failed sign-in attempts. Try again in {max(1, int((until - now) // 60) + 1)} minute(s).")
        user = self.users.by_username(name)
        if user is None:
            dummy_verify(password or "")
            ok = False
        else:
            ok = verify_password(password or "", user.password_hash)
        if not ok:
            locked = self._fail(name, now)
            self.audit.record("login_failed", target=name, details={"reason": "invalid_credentials", "locked": locked})
            raise AccountError(401, "invalid_credentials", "The username or password is incorrect.")
        if not user.active:
            self.audit.record("login_failed", actor=user.actor(), target=name, details={"reason": "account_disabled"})
            raise AccountError(403, "account_disabled", "This account is disabled. Ask an administrator to enable it.")
        with self._lock:
            self._failures.pop(name, None)
            self._locked_until.pop(name, None)
            token = secrets.token_urlsafe(32)
            self._sessions[_digest(token)] = _Session(user.user_id, now, now)
        self.audit.record("login_succeeded", actor=user.actor(), target=name)
        return token, user

    def _fail(self, name: str, now: float) -> bool:
        with self._lock:
            recent = [t for t in self._failures.get(name, []) if now - t < FAILURE_WINDOW] + [now]
            self._failures[name] = recent
            if len(recent) >= MAX_FAILURES:
                self._locked_until[name] = now + LOCKOUT_SECONDS
                self._failures.pop(name, None)
                return True
            return False

    def logout(self, token: str) -> None:
        with self._lock:
            session = self._sessions.pop(_digest(token), None)
        if session:
            user = self.users.get(session.user_id)
            self.audit.record("logout", actor=user.actor() if user else {"user_id": session.user_id}, target=user.username if user else None)

    # ------------------------------------------------------------------ per-request check
    def authenticate(self, token: Optional[str]) -> Optional[Principal]:
        if not token:
            return None
        key = _digest(token)
        now = self.clock()
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if now - session.last_seen > self.idle_seconds:
                del self._sessions[key]
                return None
        user = self.users.get(session.user_id)
        if user is None or not user.active:
            self.revoke_user(session.user_id)
            return None
        with self._lock:
            session.last_seen = now
        return Principal(user.user_id, user.username, user.display_name, user.role)

    def peek(self, token: Optional[str]) -> Optional[str]:
        """The user id of a live session, without extending or ending it. Used only to choose a
        rate-limit key, so it never reads the account file and never changes session state."""
        if not token:
            return None
        now = self.clock()
        with self._lock:
            session = self._sessions.get(_digest(token))
            if session is None or now - session.last_seen > self.idle_seconds:
                return None
            return session.user_id

    # ------------------------------------------------------------------ SV-02: photo access token
    def issue_media_token(self, token: Optional[str]) -> Optional[str]:
        """A new random photo token for the live session behind ``token`` (replacing any earlier one), or None.
        Only its digest is kept. The token is separate from the session token, so the cookie that carries it can
        load photos but can never call the API. Does not extend the session."""
        if not token:
            return None
        now = self.clock()
        with self._lock:
            session = self._sessions.get(_digest(token))
            if session is None or now - session.last_seen > self.idle_seconds:
                return None
            media = secrets.token_urlsafe(32)
            session.media = _digest(media)
            return media

    def authenticate_media(self, media: Optional[str]) -> Optional[Principal]:
        """The user behind a photo token, if its session is still live and the account active. Never extends the
        session: viewing photos alone does not keep a session open beyond the idle timeout."""
        if not media:
            return None
        key = _digest(media)
        now = self.clock()
        with self._lock:
            session = next((s for s in self._sessions.values() if s.media is not None and secrets.compare_digest(s.media, key)), None)
            if session is None or now - session.last_seen > self.idle_seconds:
                return None
        user = self.users.get(session.user_id)
        if user is None or not user.active:
            return None
        return Principal(user.user_id, user.username, user.display_name, user.role)

    def revoke_user(self, user_id: str) -> int:
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s.user_id == user_id]
            for k in keys:
                del self._sessions[k]
        return len(keys)

    def active_sessions(self, user_id: str) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.user_id == user_id)

    # ------------------------------------------------------------------ administration
    def create_user(self, actor: dict, username: str, password: str, role: str, display_name: Optional[str]) -> User:
        user = self.users.create(username, password, role, display_name)
        self.audit.record("user_created", actor=actor, target=user.user_id,
                          details={"username": user.username, "role": user.role, "display_name": user.display_name})
        return user

    def update_user(self, actor: Principal, user_id: str, **fields) -> User:
        if user_id == actor.user_id and (fields.get("active") is False or fields.get("role") in ("reviewer", "viewer")):
            # Guard against locking yourself out; another administrator must do it.
            raise AccountError(409, "self_change", "You cannot disable or demote your own account.")
        user, changes = self.users.update(user_id, **fields)
        if not changes:
            return user
        details = {k: {"from": v[0], "to": v[1]} for k, v in changes.items() if k != "password"}
        if "password" in changes:
            details["sign_in_reset"] = True  # key avoids audit redaction words
        if "role" in changes:
            self.audit.record("user_role_changed", actor=actor.actor(), target=user_id,
                              details={"username": user.username, **details["role"]})
        if "active" in changes:
            self.audit.record("user_enabled" if user.active else "user_disabled", actor=actor.actor(), target=user_id,
                              details={"username": user.username})
        rest = {k: v for k, v in details.items() if k not in ("role", "active")}
        if rest:
            self.audit.record("user_updated", actor=actor.actor(), target=user_id, details={"username": user.username, **rest})
        if "active" in changes or "role" in changes or "password" in changes:
            self.revoke_user(user_id)  # the user must sign in again with the new permissions / password
        return user


__all__ = ["AccountError", "AuthService", "Principal", "normalise_username"]
