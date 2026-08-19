"""The optional demographic profile, kept in the voter's own browser.

The counters in ``demographic_tallies`` are keyed on (law, dimension, category,
choice). When a voter changes their vote, their earlier contribution has to move
from the old choice to the new one, otherwise the breakdowns drift away from the
headline tally. Moving it needs to know *which* categories they contributed to —
and that is precisely what the server refuses to store.

So it is stored where it does not create a record: in a signed cookie on the
voter's own device. The server reads it for the length of one request and writes
nothing. Three consequences worth stating plainly, since the charter demands it:

* the server still holds no profile, and no row anywhere links a person to both
  their demographics and their vote;
* the cookie holds the categories only — never the vote — so local access to the
  browser reveals what someone typed about themselves, not how they voted;
* clearing cookies simply means an old contribution stays where it was, exactly
  as before this existed.
"""

from __future__ import annotations

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_settings
from app.models import DIMENSIONS, VALID_CATEGORIES

COOKIE_NAME = "vf_profil"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year, like the voter token


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="voter-profile")


def clean(profile: dict | None) -> dict[str, str]:
    """Keep only known dimensions with known categories."""
    if not isinstance(profile, dict):
        return {}
    return {
        dim: value
        for dim, value in profile.items()
        if dim in DIMENSIONS and isinstance(value, str) and value in VALID_CATEGORIES[dim]
    }


def read(request: Request) -> dict[str, str]:
    """The categories this browser previously contributed, or {}."""
    signed = request.cookies.get(COOKIE_NAME)
    if not signed:
        return {}
    try:
        return clean(_serializer().loads(signed))
    except BadSignature:
        return {}


def write(response: Response, profile: dict[str, str]) -> None:
    """Remember the contributed categories on this device."""
    profile = clean(profile)
    if not profile:
        return
    response.set_cookie(
        COOKIE_NAME,
        _serializer().dumps(profile),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )
