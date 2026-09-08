"""The authentication seam.

``AuthProvider`` is the single interface the rest of the app talks to. Today the
MVP wires up :class:`~app.auth.anonymous.AnonymousProvider`. Any future provider
implementing this same protocol swaps in here — no change to voting, results, or
templates required.

The seam is kept deliberately even though no second provider is in sight.
FranceConnect is the one we want and cannot have: the arrêté of 8 November 2018
opens it to private bodies only under an explicit statutory duty to verify user
identity, which a civic-expression platform has no way to fall under. That is a
legal condition on who runs the project, not a milestone the code can reach — so
this file promises an interface, never a date. See CLAUDE.md, Authentication.
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
    #: Whether a real login flow exists yet. False ⇒ no connect control is
    #: rendered at all. A disabled button reading "bientôt disponible" used to
    #: stand in its place, and it read as « you need an account and cannot even
    #: make one » — the exact opposite of the truth, on the one page whose job is
    #: to say that voting needs nothing. Transparency is served by the prose
    #: there, not by a control promising an availability nobody controls.
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
