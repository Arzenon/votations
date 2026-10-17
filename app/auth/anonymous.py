"""Anonymous auth provider: a random token in a signed cookie.

The token is created on the first vote, not on reading. The database stores
its SHA-256, so a leak reveals neither the cookie nor which votes come from
the same browser. The hash is unkeyed on purpose: the token is 256 random
bits, and rotating SECRET_KEY mustn't let everyone vote again.
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_settings

COOKIE_NAME = "vf_voter"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


class AnonymousProvider:
    login_label = "Se connecter"
    login_enabled = False

    def __init__(self) -> None:
        settings = get_settings()
        self._serializer = URLSafeSerializer(settings.signing_keys, salt="voter-token")

    def _hash(self, raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def peek_voter_token(self, request: Request) -> str | None:
        raw = self._read_cookie(request)
        return self._hash(raw) if raw is not None else None

    def get_voter_token(self, request: Request, response: Response) -> str:
        raw = self._read_cookie(request)
        if raw is None:
            raw = secrets.token_urlsafe(32)
            response.set_cookie(
                COOKIE_NAME,
                self._serializer.dumps(raw),
                max_age=_COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=get_settings().cookie_secure,
            )
        return self._hash(raw)

    def _read_cookie(self, request: Request) -> str | None:
        signed = request.cookies.get(COOKIE_NAME)
        if not signed:
            return None
        try:
            return self._serializer.loads(signed)
        except BadSignature:
            return None

    def login_url(self) -> str:
        return "/connexion"
