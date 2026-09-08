"""Pluggable authentication layer.

The voting code depends only on the :class:`AuthProvider` protocol, so another
provider can replace the MVP anonymous one without any change to the voting flow.
See ``base.py`` for the seam — and for why it names no candidate and no date.
"""

from app.auth.anonymous import AnonymousProvider
from app.auth.base import AuthProvider, get_auth_provider

__all__ = ["AnonymousProvider", "AuthProvider", "get_auth_provider"]
