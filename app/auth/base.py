"""The authentication seam.

``AuthProvider`` is the single interface the rest of the app talks to. Today the
MVP wires up :class:`~app.auth.anonymous.AnonymousProvider`. In V2 a
``FranceConnectProvider`` implementing this same protocol will be swapped in here
— no change to voting, results, or templates required.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol, runtime_checkable

from fastapi import Request, Response


@runtime_checkable
class AuthProvider(Protocol):
    """Everything the application needs from an authentication backend."""

    #: Human label for the connect button (e.g. "Se connecter", "FranceConnect").
    login_label: str
    #: Whether a real login flow exists yet. False ⇒ the connect button is shown
    #: but disabled with a "bientôt disponible" note (transparency requirement).
    login_enabled: bool

    def get_voter_token(self, request: Request, response: Response) -> str:
        """Return a stable, anonymous, non-PII token identifying this voter.

        Used solely to enforce one-vote-per-law. Must never be derivable from or
        linkable to a real identity. ``response`` lets the provider persist the
        token (e.g. set a signed cookie) when first issued.

        Call this only when the visitor is actually voting: it *creates* an
        identifier if none exists.
        """
        ...

    def peek_voter_token(self, request: Request) -> str | None:
        """The token this browser already carries, or None — issuing nothing.

        Reading a law page must not mint an identifier for someone who only
        reads, so the read path asks with this and accepts "no token" as the
        ordinary answer.
        """
        ...

    def login_url(self) -> str:
        """Where the connect button points."""
        ...


@lru_cache(maxsize=1)
def get_auth_provider() -> AuthProvider:
    """Return the process-wide auth provider (FastAPI dependency).

    Cached rather than assigned to a module global behind an ``if is None``. The
    routes are sync ``def``, so Starlette runs each one on its own thread and two
    requests arriving together could both find the global empty and each build a
    provider — the same race ``app.drafting._executor`` takes a lock against, and
    the reason that one takes a lock is that a second provider built and dropped
    is a bug waiting for the object to start holding something.

    ``lru_cache`` on a no-argument function is the whole fix: it does the
    build-once itself, under its own lock, and « process-wide » becomes something
    the code says rather than something the caller has to arrange.
    """
    from app.auth.anonymous import AnonymousProvider

    return AnonymousProvider()
