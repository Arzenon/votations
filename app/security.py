"""Cross-site request rejection and URL sanitising.

Both public voting and the administration accept state-changing POSTs from a
browser form. Neither uses a session token a third-party page could not replay:
the admin sends HTTP Basic credentials (attached cross-site regardless of
SameSite) and the voter sends a cookie or none at all. Comparing the request's
own host to the origin it claims to come from closes both without a session
rewrite.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

# Schemes a stored link may use. Anything else — `javascript:`, `data:` — becomes
# an executable payload the moment it is rendered as an href.
SAFE_URL_SCHEMES = ("http", "https")


def _origin(url: str) -> tuple[str, str]:
    """(scheme, host:port) — the only part of a URL that identifies a site."""
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def require_same_origin(request: Request) -> None:
    """Reject state-changing requests that came from another site.

    Applied to voting as well as administration: SameSite=Lax stops a forged
    cross-site POST from carrying an existing voter cookie, but not from minting
    a fresh one, so any page on the web could otherwise turn its visitors into
    ballots.

    Comparing the request's own host to Origin (falling back to Referer) closes
    that. Requests with neither header are not browser form posts (curl, the test
    client) and are left alone.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    stated = request.headers.get("origin") or request.headers.get("referer")
    if not stated:
        return
    # Compare parsed origins, never string prefixes: "http://site.evil.example"
    # starts with "http://site" and would otherwise sail through.
    if _origin(stated) != _origin(str(request.base_url)):
        raise HTTPException(
            status_code=403,
            detail="Requête refusée : elle ne provient pas de ce site.",
        )


def safe_url(value: str | None) -> str:
    """Return ``value`` if it is a plain http(s) link, else "".

    Applied to every URL an administrator can type — argument sources and the
    Légifrance link — because those are rendered as hrefs on the public page, and
    the roadmap opens the administration to all authenticated users in V3.
    """
    if not value:
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    scheme = urlsplit(candidate).scheme.lower()
    return candidate if scheme in SAFE_URL_SCHEMES else ""
