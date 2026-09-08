"""What the application decides about a request before it becomes an answer.

Three small things, and they are here together because each is a rule about an
incoming request rather than about any one route.

Both public voting and the administration accept state-changing POSTs from a
browser form. Neither uses a session token a third-party page could not replay:
the admin sends HTTP Basic credentials (attached cross-site regardless of
SameSite) and the voter sends a cookie or none at all. Comparing the request's
own host to the origin it claims to come from closes both without a session
rewrite.

Beside it: whether the request was really a HEAD (see :func:`is_head`), and
whether a stored URL may be rendered as an href at all.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.config import get_settings

# Schemes a stored link may use. Anything else — `javascript:`, `data:` — becomes
# an executable payload the moment it is rendered as an href.
SAFE_URL_SCHEMES = ("http", "https")


def _origin(url: str) -> tuple[str, str]:
    """(scheme, host:port) — the only part of a URL that identifies a site."""
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


#: Said to a caller whose request does not come from this site.
_REFUSED = "Requête refusée : elle ne provient pas de ce site."


def require_same_origin(request: Request) -> None:
    """Reject state-changing requests that came from another site.

    Applied to voting as well as administration: SameSite=Lax stops a forged
    cross-site POST from carrying an existing voter cookie, but not from minting
    a fresh one, so any page on the web could otherwise turn its visitors into
    ballots.

    Comparing the request's own host to Origin (falling back to Referer) closes
    that.

    **A request carrying neither header is refused in production and allowed
    outside it**, which is the one asymmetry here and is worth stating plainly.
    Every browser has sent ``Origin`` on a cross-site form post for years, so
    "neither header" describes a script rather than a page — but the whole point
    of this check is that it must not depend on somebody else's default staying
    what it is today, and the administration is guarded by HTTP Basic, which a
    browser attaches to a cross-site request whether we like it or not. So the
    deployment that faces the internet takes the strict reading.

    Development keeps the lenient one because that is what `curl` and the test
    client send, and because nothing there is worth forging. An operator
    scripting against a live instance sends `Origin: https://<son domaine>`
    explicitly, which is a fair thing to ask of a script that changes state.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    stated = request.headers.get("origin") or request.headers.get("referer")
    if not stated:
        if get_settings().is_production:
            raise HTTPException(status_code=403, detail=_REFUSED)
        return
    # Compare parsed origins, never string prefixes: "http://site.evil.example"
    # starts with "http://site" and would otherwise sail through.
    if _origin(stated) != _origin(str(request.base_url)):
        raise HTTPException(status_code=403, detail=_REFUSED)


#: Where `app.main.HeadAsGet` records that a request arrived as HEAD.
#:
#: Namespaced, because the ASGI scope is a dict several layers share and a bare
#: key like "head" is the sort of thing two of them eventually both use for two
#: different purposes.
HEAD_SCOPE_KEY = "vf.head"


def is_head(request: Request) -> bool:
    """Whether this GET is really the HEAD that `HeadAsGet` rewrote.

    Almost nothing needs to know — the whole point of that middleware is that a
    handler answers both the same way, and the server drops the body. The
    exception is a handler for which *producing* the body is the work: an
    open-data export scans every vote on a votation, and doing that for a
    response whose body is thrown away is the one case where the difference is
    worth a branch.

    False for an ordinary GET, and for a HEAD reaching an application that is not
    behind that middleware — so a caller that forgets the branch is merely slow,
    never wrong.
    """
    return bool(request.scope.get(HEAD_SCOPE_KEY))


def is_safe_url(value: str | None) -> bool:
    """Whether ``value`` is empty or a plain http(s) link.

    Empty counts as safe: an absent URL is a legitimate answer everywhere one is
    accepted. Only a *present* URL with another scheme is a rejection, which is
    the distinction callers need to tell the administrator what was refused
    rather than blanking the field in silence.
    """
    if not value or not value.strip():
        return True
    return urlsplit(value.strip()).scheme.lower() in SAFE_URL_SCHEMES


def safe_url(value: str | None) -> str:
    """Return ``value`` if it is a plain http(s) link, else "".

    Applied to every URL an administrator can type — argument sources and the
    Légifrance link — because those are rendered as hrefs on the public page, and
    the roadmap opens the administration to all authenticated users in V3.

    This is the last line of defence and stays silent. The admin form checks
    :func:`is_safe_url` first so a rejected link is reported rather than dropped;
    anything reaching here has already been through that, or comes from a code
    path where there is nobody to tell.
    """
    if not value:
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    return candidate if is_safe_url(candidate) else ""
