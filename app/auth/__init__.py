"""Pluggable authentication layer.

The voting code depends only on the :class:`AuthProvider` protocol, so a future
FranceConnect (OIDC) provider can replace the MVP anonymous provider without any
change to the voting flow. See ``base.py`` for the seam.
"""

from app.auth.anonymous import AnonymousProvider
from app.auth.base import AuthProvider, get_auth_provider

__all__ = ["AuthProvider", "AnonymousProvider", "get_auth_provider"]
