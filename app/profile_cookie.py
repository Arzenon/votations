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

**The cookie is keyed by law.** It used to hold one profile for the whole site,
overwritten on every vote, while the counters it moves are per-law. Voting on law
A as 18-24 and then on law B as 65+ left the cookie saying 65+, so changing the
vote on law A decremented a cell that belonged to other voters and stranded the
real contribution under the old choice. Both directions were wrong, and it was
repeatable on purpose to deflate a category.
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

# Laws remembered at once. A browser that votes on more than this forgets its
# oldest entries first, which degrades exactly like clearing cookies: an old
# contribution stays where it was. Also a hard ceiling on the cookie's size —
# browsers drop a cookie over 4 kB outright, which would lose *every* entry.
_MAX_LAWS = 25
# Refuse to emit anything close to the 4 kB limit, whatever the categories are.
_MAX_COOKIE_BYTES = 3000


@lru_cache(maxsize=1)
def _serializer() -> URLSafeSerializer:
    """Built once, not per call.

    Each construction derives a signing key from SECRET_KEY, and a single vote
    goes through this three times — read, read again, write. The key cannot
    change under a running process, so neither can the serializer.
    """
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
    """Keep only ``{law_id: {dimension: category}}`` entries that make sense.

    A cookie written before this was keyed by law is a flat ``{dimension:
    category}`` map. Its values are strings rather than dicts, so every entry is
    dropped here and the browser behaves exactly as if it had no cookie: nothing
    moves, which is the documented safe fallback. Nothing is corrupted by it.
    """
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
    """The decoded cookie: every law this browser has voted on, and how.

    Decoded **once per request**. Casting a vote needs both halves of it — what
    this browser contributed to *this* law, so the counters can be moved, and the
    other laws' entries, so writing one does not drop the rest — and reading it
    twice meant verifying the signature and parsing the JSON twice for an answer
    that cannot have changed in between.

    Holding it as an object rather than caching the decode behind the old
    module-level ``read``/``write`` keeps that visible at the call site: the
    cookie is loaded where the request starts handling a vote, and the same
    object is asked and then told.
    """

    laws: dict[str, dict[str, str]]

    def for_law(self, law_id: int) -> dict[str, str]:
        """The categories this browser previously contributed to this law, or {}."""
        return self.laws.get(str(law_id), {})

    def remember(self, response: Response, law_id: int, profile: dict[str, str]) -> None:
        """Record the categories contributed to this law, keeping the other laws."""
        profile = clean_profile(profile)
        if not profile:
            # Nothing to record for this law. Leave whatever is already stored
            # alone rather than emitting a cookie that says nothing.
            return

        # Re-insert at the end so the most recently used law is the last to be
        # dropped when the store is trimmed.
        self.laws.pop(str(law_id), None)
        self.laws[str(law_id)] = profile

        value = _fit(self.laws)
        if value is None:
            return
        response.set_cookie(
            COOKIE_NAME,
            value,
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=get_settings().cookie_secure,
        )


def load(request: Request) -> ProfileStore:
    """Read and verify the cookie once, for the rest of this request."""
    return ProfileStore(_load(request))


def _fit(store: dict[str, dict[str, str]]) -> str | None:
    """Serialise, dropping the oldest laws until the cookie is a sane size."""
    serializer = _serializer()
    entries = list(store.items())[-_MAX_LAWS:]
    while entries:
        value = serializer.dumps(dict(entries))
        if len(value.encode("utf-8")) <= _MAX_COOKIE_BYTES:
            return value
        entries = entries[1:]
    return None
