"""The authentication seam.

``AuthProvider`` is the single interface the rest of the app talks to. Today the
MVP wires up :class:`~app.auth.anonymous.AnonymousProvider`. In V2 a
``FranceConnectProvider`` implementing this same protocol will be swapped in here
— no change to voting, results, or templates required.
"""

from __future__ import annotations

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
        """
        ...

    def login_url(self) -> str:
        """Where the connect button points."""

    def logout_url(self) -> str:
        ...

    def is_authenticated(self, request: Request) -> bool:
        ...


_provider: AuthProvider | None = None


def get_auth_provider() -> AuthProvider:
    """Return the process-wide auth provider (FastAPI dependency)."""
    global _provider
    if _provider is None:
        from app.auth.anonymous import AnonymousProvider

        _provider = AnonymousProvider()
    return _provider
