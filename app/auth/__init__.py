"""Authentication. The voting code only depends on the `AuthProvider` protocol."""

from app.auth.anonymous import AnonymousProvider
from app.auth.base import AuthProvider, get_auth_provider

__all__ = ["AnonymousProvider", "AuthProvider", "get_auth_provider"]
