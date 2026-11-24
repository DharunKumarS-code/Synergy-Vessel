"""PBKDF2-SHA256 password hashing with a unique random salt per hash.

Stored form: ``pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>``. The iteration count is stored with
each hash, so it can be raised later without invalidating existing accounts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256
MIN_LENGTH = 12
MAX_LENGTH = 256


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str, *, iterations: int | None = None) -> str:
    n = iterations or ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, n)
    return f"{ALGORITHM}${n}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, salt, digest = stored.split("$")
        if algo != ALGORITHM:
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(n))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Burns comparable CPU for unknown usernames, so response timing does not reveal which accounts exist.
_DUMMY = None


def dummy_verify(password: str) -> None:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(16))
    verify_password(password, _DUMMY)


def password_problem(password: str, username: str = "") -> str | None:
    """Return why a new password is not acceptable, or None."""
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        return f"The password must be at least {MIN_LENGTH} characters long."
    if len(password) > MAX_LENGTH:
        return f"The password must be at most {MAX_LENGTH} characters long."
    if password.strip() != password or not password.strip():
        return "The password must not start or end with spaces."
    if username and username.lower() in password.lower():
        return "The password must not contain the username."
    return None
