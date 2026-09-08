"""The provisional administration guard: HTTP Basic, with a real throttle.

Provisional until V3 opens the administration to authenticated users. Until then
it is a single static credential pair, which is precisely the thing a brute force
is good at, so the throttle here has to do actual work.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import ratelimit
from app.config import get_settings

# RFC 7617 makes `realm` mandatory in a Basic challenge. Chrome tolerates its
# absence and prompts anyway; Firefox does not, and shows the bare 401 body
# instead of a login box — so the realm is what makes the prompt appear there.
# Keep it ASCII: header values are latin-1 encoded.
AUTH_REALM = "Votations"
security = HTTPBasic(realm=AUTH_REALM)


def _unauthorised() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Identifiants administrateur invalides",
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
    )


def _too_many() -> HTTPException:
    # `Retry-After`, as on the two public refusals (see
    # `app.routes.public.common`): a 429 without it invites an immediate retry,
    # which is the one thing neither side wants — least of all here, where every
    # retry inside the window extends nothing but does keep the guessing streak
    # alive for whoever shares this address.
    return HTTPException(
        status_code=429,
        detail="Trop de tentatives de connexion. Réessayez dans quelques minutes.",
        headers={"Retry-After": "60"},
    )


def _matches(sent: str, expected: str) -> bool:
    """Constant-time comparison that survives an accented password.

    ``secrets.compare_digest`` raises TypeError on any non-ASCII *str*, so a
    French password — or merely a caller sending an accented username — used to
    become an unhandled 500 rather than a refusal. Comparing the UTF-8 bytes
    keeps the timing guarantee and accepts the whole of Unicode.
    """
    return secrets.compare_digest(sent.encode("utf-8"), expected.encode("utf-8"))


def require_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Check the provisional credentials, refusing an address that is guessing.

    The throttle is checked *before* the comparison, and that ordering is the
    whole point. Recording a failure and then answering 401 anyway — which is
    what this used to do — slowed nothing down: an attacker could guess at
    network speed indefinitely. Worse, refusing only the *successful* login with
    a 429 turned the throttle into an oracle, since a 429 meant "that password
    was right, come back in five minutes".

    Refusing every attempt from an address in a guessing streak fixes both: the
    guessing is actually slowed, and a refused attempt looks identical whether
    the password was right or wrong. Only failures extend the streak, so a
    working administrator is never slowed down.
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
