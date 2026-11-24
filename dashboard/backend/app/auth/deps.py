"""Central FastAPI dependencies for authorisation. Routes declare one of:

    CurrentUser      -- optional (public endpoints that can show who is signed in)
    RequireUser      -- any active account (viewer, reviewer or admin) -> 401 when not signed in
    RequireReviewer  -- reviewer or administrator (SV-02)              -> 401 / 403
    RequireAdmin     -- administrator only                              -> 401 / 403
    RequireMediaUser -- any active account, via bearer header or the photo cookie (SV-02, /api/images only)
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, Request

from .service import AuthService, Principal
from .users import AccountError


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def current_user(request: Request) -> Optional[Principal]:
    return get_auth(request).authenticate(bearer_token(request))


def require_user(user: Annotated[Optional[Principal], Depends(current_user)]) -> Principal:
    if user is None:
        raise AccountError(401, "not_authenticated", "Sign in to perform this action. Your session may have expired.")
    return user


def require_admin(user: Annotated[Principal, Depends(require_user)]) -> Principal:
    if not user.is_admin:
        raise AccountError(403, "forbidden", "This action requires an administrator account.")
    return user


def require_reviewer(user: Annotated[Principal, Depends(require_user)]) -> Principal:
    """SV-02: running AI analyses and recording review decisions -- reviewers and administrators, never viewers."""
    if not user.can_review:
        raise AccountError(403, "forbidden", "This action requires a reviewer or administrator account.")
    return user


# SV-02: photos are loaded by <img> tags, which cannot send the Authorization header. At sign-in the browser also
# receives this HttpOnly, SameSite=Strict cookie, scoped to /api/images. It carries a separate photo token bound to
# the session (see AuthService.issue_media_token) -- never the session token -- so it can only ever load photos.
MEDIA_COOKIE = "sv_media"
MEDIA_COOKIE_PATH = "/api/images"


def require_media_user(request: Request) -> Principal:
    """Photo access: a live session through the bearer header (API clients) or the photo cookie (<img> tags).
    Never through the URL."""
    auth = get_auth(request)
    user = auth.authenticate(bearer_token(request)) or auth.authenticate_media(request.cookies.get(MEDIA_COOKIE))
    if user is None:
        raise AccountError(401, "not_authenticated", "Sign in to perform this action. Your session may have expired.")
    return user


CurrentUser = Annotated[Optional[Principal], Depends(current_user)]
RequireUser = Annotated[Principal, Depends(require_user)]
RequireReviewer = Annotated[Principal, Depends(require_reviewer)]
RequireAdmin = Annotated[Principal, Depends(require_admin)]
RequireMediaUser = Annotated[Principal, Depends(require_media_user)]
