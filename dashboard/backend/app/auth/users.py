"""Account store: dashboard/data/auth/users.json, rewritten atomically (temp file + fsync + os.replace).

The file is re-read before every change, so a change made by the CLI while the server runs is not lost.
Password hashes never leave this module except to the password verifier; ``public()`` is the only
account shape returned by the API.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .passwords import hash_password, password_problem

USERS_FILE = "users.json"
ROLES = ("viewer", "reviewer", "admin")  # SV-02: viewer = read-only
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
USER_ID_RE = re.compile(r"^USR-[0-9a-f]{12}$")


class AccountError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class User:
    user_id: str
    username: str
    display_name: str
    role: str
    active: bool
    password_hash: str
    created_at: str
    updated_at: str
    password_changed_at: str

    def public(self) -> dict:
        d = asdict(self)
        d.pop("password_hash")
        return d

    def actor(self) -> dict:
        return {"user_id": self.user_id, "username": self.username, "role": self.role}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalise_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise AccountError(400, "invalid_username",
                           "Usernames are 3-32 characters: lowercase letters, digits, '.', '_' or '-', starting with a letter or digit.")
    return name


def _display(display_name: Optional[str], username: str) -> str:
    text = " ".join((display_name or "").split()) or username
    if len(text) > 80:
        raise AccountError(400, "invalid_display_name", "The display name must be at most 80 characters.")
    return text


class UserStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / USERS_FILE
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ file
    def _load(self) -> list[User]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [User(**u) for u in data.get("users", [])]

    def _save(self, users: list[User]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{USERS_FILE}.{secrets.token_hex(4)}.tmp")
        payload = json.dumps({"version": 1, "users": [asdict(u) for u in users]}, indent=2).encode("utf-8")
        try:
            with open(tmp, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()

    # ------------------------------------------------------------------ queries
    def all(self) -> list[User]:
        with self._lock:
            return self._load()

    def get(self, user_id: str) -> Optional[User]:
        return next((u for u in self.all() if u.user_id == user_id), None)

    def by_username(self, username: str) -> Optional[User]:
        name = (username or "").strip().lower()
        return next((u for u in self.all() if u.username == name), None)

    # ------------------------------------------------------------------ changes
    def create(self, username: str, password: str, role: str, display_name: Optional[str] = None) -> User:
        name = normalise_username(username)
        if role not in ROLES:
            raise AccountError(400, "invalid_role", "The role must be 'viewer', 'reviewer' or 'admin'.")
        problem = password_problem(password, name)
        if problem:
            raise AccountError(400, "weak_password", problem)
        with self._lock:
            users = self._load()
            if any(u.username == name for u in users):
                raise AccountError(409, "username_taken", "An account with this username already exists.")
            now = _now()
            user = User(user_id=f"USR-{secrets.token_hex(6)}", username=name, display_name=_display(display_name, name),
                        role=role, active=True, password_hash=hash_password(password), created_at=now,
                        updated_at=now, password_changed_at=now)
            self._save(users + [user])
            return user

    def update(self, user_id: str, *, role: Optional[str] = None, active: Optional[bool] = None,
               display_name: Optional[str] = None, password: Optional[str] = None) -> tuple[User, dict]:
        """Apply changes; returns (updated user, {field: (old, new)}) -- password changes are reported as a flag only."""
        with self._lock:
            users = self._load()
            user = next((u for u in users if u.user_id == user_id), None)
            if user is None:
                raise AccountError(404, "unknown_user", "Account not found.")
            changes: dict = {}
            if role is not None and role != user.role:
                if role not in ROLES:
                    raise AccountError(400, "invalid_role", "The role must be 'viewer', 'reviewer' or 'admin'.")
                changes["role"] = (user.role, role)
            if active is not None and active != user.active:
                changes["active"] = (user.active, active)
            if display_name is not None and _display(display_name, user.username) != user.display_name:
                changes["display_name"] = (user.display_name, _display(display_name, user.username))
            if password is not None:
                problem = password_problem(password, user.username)
                if problem:
                    raise AccountError(400, "weak_password", problem)
                changes["password"] = (None, None)
            if not changes:
                return user, {}
            loses_admin = user.role == "admin" and user.active and (
                changes.get("role", (None, "admin"))[1] != "admin" or changes.get("active", (None, True))[1] is False)
            if loses_admin and not any(u.role == "admin" and u.active and u.user_id != user_id for u in users):
                raise AccountError(409, "last_admin",
                                   "This is the last active administrator. Create or enable another administrator first.")
            now = _now()
            if "role" in changes:
                user.role = changes["role"][1]
            if "active" in changes:
                user.active = changes["active"][1]
            if "display_name" in changes:
                user.display_name = changes["display_name"][1]
            if "password" in changes:
                user.password_hash = hash_password(password)
                user.password_changed_at = now
            user.updated_at = now
            self._save(users)
            return user, changes
