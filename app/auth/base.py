"""The authentication interface.

Only `AnonymousProvider` exists today. FranceConnect isn't available to a
private platform like this one (arrêté of 8 November 2018), which is a legal
matter, not a technical one. See CLAUDE.md, Authentication.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol, runtime_checkable

from fastapi import Request, Response


@runtime_checkable
class AuthProvider(Protocol):
    """What the app needs from an authentication backend."""

    # Label of the login button.
    login_label: str
    # When False, no login button is shown at all.
    login_enabled: bool

    def get_voter_token(self, request: Request, response: Response, law_id: int) -> str:
        """Return this voter's anonymous token for this law, creating it if needed.

        Only used to enforce one vote per law; must not be linkable to an
        identity, nor across laws. ``response`` is used to set the cookie. Only
        call when actually voting.
        """
        ...

    def peek_voter_token(self, request: Request, law_id: int) -> str | None:
        """The existing token, or None. Never creates one (for read-only pages)."""
        ...

    def login_url(self) -> str:
        """Target of the login button."""
        ...


@lru_cache(maxsize=1)
def get_auth_provider() -> AuthProvider:
    """The process-wide auth provider (FastAPI dependency).

    lru_cache rather than a lazily set global, which two request threads could
    both initialise.
    """
    from app.auth.anonymous import AnonymousProvider

    return AnonymousProvider()
