"""Request-level checks: same-origin for POSTs, HEAD detection, safe URLs.

Neither voting nor the admin uses a CSRF token (the admin uses HTTP Basic, the
voter a cookie), so POSTs are checked against the Origin or Referer header.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.config import get_settings

# Allowed link schemes. `javascript:` or `data:` in an href would be executable.
SAFE_URL_SCHEMES = ("http", "https")


def _origin(url: str) -> tuple[str, str]:
    """(scheme, host:port) of a URL."""
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


_REFUSED = "Requête refusée : elle ne provient pas de ce site."


def require_same_origin(request: Request) -> None:
    """Reject POSTs coming from another site (Origin, or Referer as fallback).

    Also applies to voting: SameSite=Lax doesn't stop another site from making
    its visitors vote with a fresh cookie.

    Requests with neither header are refused in production. Allowed in
    development, for curl and the test client.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    stated = request.headers.get("origin") or request.headers.get("referer")
    if not stated:
        if get_settings().is_production:
            raise HTTPException(status_code=403, detail=_REFUSED)
        return
    # Compare parsed origins, not prefixes ("http://site.evil.example").
    if _origin(stated) != _origin(str(request.base_url)):
        raise HTTPException(status_code=403, detail=_REFUSED)


# Scope key set by `app.main.HeadAsGet` for HEAD requests.
HEAD_SCOPE_KEY = "vf.head"


def is_head(request: Request) -> bool:
    """Whether this GET is a HEAD rewritten by `HeadAsGet`.

    Lets the export skip scanning the votes when the body will be dropped.
    """
    return bool(request.scope.get(HEAD_SCOPE_KEY))


def is_safe_url(value: str | None) -> bool:
    """Whether ``value`` is empty or an http(s) link."""
    if not value or not value.strip():
        return True
    return urlsplit(value.strip()).scheme.lower() in SAFE_URL_SCHEMES


def safe_url(value: str | None) -> str:
    """Return ``value`` if it is an http(s) link, else "".

    Last line of defence for URLs rendered as hrefs. The admin form validates
    with `is_safe_url` first to report errors.
    """
    if not value:
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    return candidate if is_safe_url(candidate) else ""
