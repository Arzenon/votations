"""MVP auth provider: anonymous, signed browser token.

Collects *no* personal data. A random token is minted **when someone first votes**
— not when they read — and stored in a signed cookie; the value persisted in the
DB is the SHA-256 of that token, so a database leak reveals neither the cookie nor
any link between votes and a browser. This gives best-effort one-vote-per-law
without any identity.

On the digest: the token is 256 bits from :mod:`secrets`, so a plain hash is not
guessable and a keyed digest would add nothing an attacker could exploit. It is
left unkeyed deliberately — rotating SECRET_KEY must not silently re-open every
past votation to a second vote from the same person.
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
                # HTTPS-only in production; plain HTTP locally would drop it.
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
