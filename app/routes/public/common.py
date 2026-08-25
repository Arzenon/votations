"""Shared by every public route: visibility, caching policy, refusal pages.

Small on purpose. What lives here is what more than one of the sibling modules
needs — the rule for whether a law may be shown at all, the cache header the
visitor-independent pages opt into, and the four pages this side of the site
answers a refusal with. Everything else belongs to the module that owns its URL.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.crud import laws as laws_crud
from app.models import STATUS_OPEN, Law
from app.templating import templates

# Pages that are byte-for-byte the same for every visitor, and may therefore be
# cached by anything in the path.
#
# The site's default is `private, no-store` (see `app.main.DEFAULT_CACHE_CONTROL`)
# and that is right for the law page, which renders the visitor's own ballot, and
# for the administration. It is wrong for these: they carry nothing about whoever
# is reading, and forbidding every cache means a popular votation recomputes the
# same aggregates — a group-by over every vote cast on it — once per reader.
#
# Note that nothing in the shipped stack caches on the app's behalf: Caddy does
# not, by default. So today this saves a reader's own repeat views, and makes a
# CDN or a caching proxy worth adding the day traffic calls for one.
PUBLIC_PAGE_CACHE = {"Cache-Control": "public, max-age=60"}

# The results of a votation still being voted on are a different trade, because
# one of the readers is somebody who has just voted. A minute of caching would
# let their own browser answer "voir les résultats" from a copy taken before
# their ballot, which reads as the vote not having counted — on a platform whose
# entire premise is that you can check the number yourself.
#
# Half a minute is the compromise: still enough that a link going round absorbs
# its spike in front of the app rather than in it, short enough that a voter
# checking their own vote almost always sees it. Once the vote is closed the
# figures no longer move at all, so that page is cached properly.
OPEN_RESULTS_CACHE = {"Cache-Control": "public, max-age=30"}
CLOSED_RESULTS_CACHE = {"Cache-Control": "public, max-age=300"}


def results_cache(status: str) -> dict[str, str]:
    """The caching a results page may ask for, given the votation's state."""
    return OPEN_RESULTS_CACHE if status == STATUS_OPEN else CLOSED_RESULTS_CACHE


def visible(db: Session, slug: str) -> Law | None:
    """The law behind this slug, if it may be shown at all.

    Archived laws are withdrawn from the site: they answer 404 exactly as an
    unpublished one does, on every public route including the exports.
    """
    law = laws_crud.get_by_slug(db, slug)
    return law if law is not None and law.is_public else None


def too_many_votes(request: Request) -> HTMLResponse:
    """Politely refuse a burst without explaining the exact threshold."""
    return templates.TemplateResponse(
        request,
        "erreur.html",
        {
            "titre": "Trop de votes en peu de temps",
            "message": (
                "Trop de votes ont été envoyés depuis votre connexion en quelques "
                "minutes. Réessayez dans un instant."
            ),
        },
        status_code=429,
    )


def too_many_exports(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "erreur.html",
        {
            "titre": "Trop de téléchargements",
            "message": (
                "Trop de fichiers ont été téléchargés depuis votre connexion en "
                "quelques minutes. Les données restent disponibles : réessayez "
                "dans un instant."
            ),
        },
        status_code=429,
    )


def exports_busy(request: Request) -> HTMLResponse:
    """The server is at capacity — not this caller's fault, and it says so.

    503 rather than 429: nothing about this request was excessive, the queue is
    simply full. ``Retry-After`` gives a well-behaved client something better to
    do than retry immediately.
    """
    return templates.TemplateResponse(
        request,
        "erreur.html",
        {
            "titre": "Téléchargements momentanément saturés",
            "message": (
                "Plusieurs exports sont en cours de téléchargement. Les données "
                "restent publiques et complètes : réessayez dans une minute."
            ),
        },
        status_code=503,
        headers={"Retry-After": "60"},
    )


def not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)

