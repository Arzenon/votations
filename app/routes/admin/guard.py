"""Provisional admin guard: HTTP Basic with brute-force throttling."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import ratelimit
from app.config import get_settings

# Required for Firefox to show the login prompt. ASCII only (latin-1 header).
AUTH_REALM = "Votations"
security = HTTPBasic(realm=AUTH_REALM)


def _unauthorised() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Identifiants administrateur invalides",
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
    )


def _too_many() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Trop de tentatives de connexion. Réessayez dans quelques minutes.",
        headers={"Retry-After": "60"},
    )


def _matches(sent: str, expected: str) -> bool:
    """Constant-time comparison on bytes (compare_digest rejects non-ASCII str)."""
    return secrets.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))


def require_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Check the admin credentials.

    The throttle is checked before the password, so a 429 doesn't reveal
    whether the password was right. Only failures count towards the limit.
    """
    settings = get_settings()
    throttle = ratelimit.Throttle(
        limit=settings.admin_rate_limit,
        window_seconds=settings.admin_rate_window_seconds,
        bucket="admin",
    )
    if ratelimit.over_limit(request, **throttle):
        raise _too_many()

    ok_user = _matches(credentials.username, settings.admin_username)
    ok_pass = _matches(credentials.password, settings.admin_password)
    if not (ok_user and ok_pass):
        ratelimit.too_many(request, **throttle)
        raise _unauthorised()
    return credentials.username
