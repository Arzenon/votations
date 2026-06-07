"""MVP auth provider: anonymous, signed browser token.

Collects *no* personal data. A random token is minted on first visit and stored
in a signed cookie; the value persisted in the DB is a salted hash of that token,
so even a database leak cannot reveal the cookie nor link votes to a browser.
This gives best-effort one-vote-per-law without any identity.
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
    # No real login flow yet — FranceConnect arrives in V2.
    login_enabled = False

    def __init__(self) -> None:
        settings = get_settings()
        self._serializer = URLSafeSerializer(settings.secret_key, salt="voter-token")

    def _hash(self, raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

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

    def logout_url(self) -> str:
        return "/connexion"

    def is_authenticated(self, request: Request) -> bool:
        return False
