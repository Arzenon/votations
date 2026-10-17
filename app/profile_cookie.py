"""Optional demographic profile, stored in the voter's browser.

When a voter changes their vote, their profile contribution must move to the
new choice, which requires knowing their categories. The server doesn't store
them, so they live in a signed cookie, keyed by law. The cookie holds the
categories only, never the vote. If it's cleared, the old contribution simply
stays where it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_settings
from app.models import DIMENSIONS, VALID_CATEGORIES

COOKIE_NAME = "vf_profil"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year, like the voter token

# Max laws kept; the oldest are dropped first. Browsers reject cookies over 4 kB.
_MAX_LAWS = 25
_MAX_COOKIE_BYTES = 3000


@lru_cache(maxsize=1)
def _serializer() -> URLSafeSerializer:
    """Cached: building one derives a key from SECRET_KEY."""
    return URLSafeSerializer(get_settings().signing_keys, salt="voter-profile")


def clean_profile(profile: object) -> dict[str, str]:
    """Keep only known dimensions with known categories."""
    if not isinstance(profile, dict):
        return {}
    return {
        dim: value
        for dim, value in profile.items()
        if dim in DIMENSIONS and isinstance(value, str) and value in VALID_CATEGORIES[dim]
    }


def _clean_store(raw: object) -> dict[str, dict[str, str]]:
    """Keep only valid ``{law_id: {dimension: category}}`` entries."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for law_id, profile in raw.items():
        if not isinstance(law_id, str) or not law_id.isdigit():
            continue
        cleaned = clean_profile(profile)
        if cleaned:
            out[law_id] = cleaned
    return out


def _load(request: Request) -> dict[str, dict[str, str]]:
    signed = request.cookies.get(COOKIE_NAME)
    if not signed:
        return {}
    try:
        return _clean_store(_serializer().loads(signed))
    except BadSignature:
        return {}


@dataclass
class ProfileStore:
    """The decoded cookie (law id to profile), decoded once per request."""

    laws: dict[str, dict[str, str]]

    def for_law(self, law_id: int) -> dict[str, str]:
        """Categories previously given for this law, or {}."""
        return self.laws.get(str(law_id), {})

    def remember(self, response: Response, law_id: int, profile: dict[str, str]) -> None:
        """Store the categories for this law, keeping the other laws."""
        profile = clean_profile(profile)
        if not profile:
            return

        # Move to the end so it's the last to be trimmed.
        self.laws.pop(str(law_id), None)
        self.laws[str(law_id)] = profile

        fitted = _fit(self.laws)
        if fitted is None:
            return
        # `_fit` may have dropped old laws; keep the store in sync.
        value, self.laws = fitted
        response.set_cookie(
            COOKIE_NAME,
            value,
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=get_settings().cookie_secure,
        )


def load(request: Request) -> ProfileStore:
    """Read and verify the cookie."""
    return ProfileStore(_load(request))


def _fit(store: dict[str, dict[str, str]]) -> tuple[str, dict[str, dict[str, str]]] | None:
    """Serialize, dropping the oldest laws until it fits.

    Returns the cookie value and the entries it contains.
    """
    serializer = _serializer()
    entries = list(store.items())[-_MAX_LAWS:]
    while entries:
        kept = dict(entries)
        value = serializer.dumps(kept)
        if len(value.encode("utf-8")) <= _MAX_COOKIE_BYTES:
            return value, kept
        entries = entries[1:]
    return None
