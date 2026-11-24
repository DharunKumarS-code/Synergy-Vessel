"""Authentication and account-administration endpoints (Phase 8.2)."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth.deps import MEDIA_COOKIE, MEDIA_COOKIE_PATH, CurrentUser, RequireAdmin, RequireUser, bearer_token, get_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])
admin = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class NewUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: Literal["viewer", "reviewer", "admin"] = "reviewer"
    display_name: Optional[str] = Field(None, max_length=80)


class UserChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Optional[Literal["viewer", "reviewer", "admin"]] = None
    active: Optional[bool] = None
    display_name: Optional[str] = Field(None, max_length=80)
    password: Optional[str] = Field(None, min_length=1, max_length=256)


def _me(user) -> dict:
    return {"user_id": user.user_id, "username": user.username, "display_name": user.display_name, "role": user.role}


# ---------------------------------------------------------------------- SV-02: photo cookie
def _set_media_cookie(request: Request, response: Response, token: str) -> None:
    """Give the browser a photo token for this session: HttpOnly (no JavaScript access), SameSite=Strict, sent only
    to /api/images, a browser-session cookie (no expiry date; the server-side session decides validity). Secure when
    the request arrived over HTTPS or SYNERGY_COOKIE_SECURE=1."""
    media = get_auth(request).issue_media_token(token)
    if media is None:
        return
    settings = getattr(request.app.state, "settings", None)
    secure = request.url.scheme == "https" or bool(getattr(settings, "cookie_secure", False))
    response.set_cookie(MEDIA_COOKIE, media, path=MEDIA_COOKIE_PATH, httponly=True, samesite="strict", secure=secure)


def _clear_media_cookie(request: Request, response: Response) -> None:
    settings = getattr(request.app.state, "settings", None)
    secure = request.url.scheme == "https" or bool(getattr(settings, "cookie_secure", False))
    response.delete_cookie(MEDIA_COOKIE, path=MEDIA_COOKIE_PATH, httponly=True, samesite="strict", secure=secure)


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    auth = get_auth(request)
    token, user = auth.login(body.username, body.password)
    _set_media_cookie(request, response, token)
    return {"token": token, "token_type": "bearer", "idle_timeout_seconds": auth.idle_seconds, "user": _me(user)}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    token = bearer_token(request)
    if token:
        get_auth(request).logout(token)   # ends the session, and with it the photo token
    _clear_media_cookie(request, response)


@router.get("/session")
def session(user: CurrentUser, request: Request, response: Response) -> dict:
    """Public: whether the presented token is a valid session (never 401). SV-02: a valid session also gets a fresh
    photo cookie (e.g. after a page reload, or after another tab signed out and cleared the shared cookie). An
    anonymous check never touches the cookie, so a signed-out tab cannot end another tab's photo access."""
    if user is not None:
        _set_media_cookie(request, response, bearer_token(request))
    return {"authenticated": user is not None, "user": _me(user) if user else None}


@router.get("/me")
def me(user: RequireUser) -> dict:
    return _me(user)


# ---------------------------------------------------------------------- administration
@admin.get("/users")
def list_users(request: Request, _: RequireAdmin) -> dict:
    auth = get_auth(request)
    return {"users": [{**u.public(), "active_sessions": auth.active_sessions(u.user_id)} for u in auth.users.all()]}


@admin.post("/users", status_code=201)
def create_user(body: NewUser, request: Request, actor: RequireAdmin) -> dict:
    return get_auth(request).create_user(actor.actor(), body.username, body.password, body.role, body.display_name).public()


@admin.patch("/users/{user_id}")
def update_user(user_id: str, body: UserChange, request: Request, actor: RequireAdmin) -> dict:
    fields = body.model_dump(exclude_none=True)
    return get_auth(request).update_user(actor, user_id, **fields).public()


@admin.get("/audit")
def audit_entries(request: Request, _: RequireAdmin, limit: int = Query(100, ge=1, le=1000),
                  event: Optional[str] = Query(None, max_length=64)) -> dict:
    return {"entries": get_auth(request).audit.entries(limit=limit, event=event)}


@admin.get("/audit/verify")
def audit_verify(request: Request, _: RequireAdmin) -> dict:
    return get_auth(request).audit.verify().as_dict()
