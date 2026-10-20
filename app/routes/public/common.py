"""Shared by the public routes: visibility, cache headers, error pages."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.crud import laws as laws_crud
from app.models import STATUS_OPEN, Law
from app.templating import templates

# For pages that are identical for every visitor (the default is no-store).
# Also used as the max age of the server-side memo in `app.crud.results`, so
# both stay in sync.
PUBLIC_PAGE_MAX_AGE = 60
PUBLIC_PAGE_CACHE = {"Cache-Control": f"public, max-age={PUBLIC_PAGE_MAX_AGE}"}

# Open votes get a short cache so someone who just voted sees their ballot
# counted. Closed votes don't change anymore.
OPEN_RESULTS_MAX_AGE = 30
CLOSED_RESULTS_MAX_AGE = 300
OPEN_RESULTS_CACHE = {"Cache-Control": f"public, max-age={OPEN_RESULTS_MAX_AGE}"}
CLOSED_RESULTS_CACHE = {"Cache-Control": f"public, max-age={CLOSED_RESULTS_MAX_AGE}"}

# Memo of an open vote's figures: enough to spare the database a recount per
# visitor when a law is shared, short enough to stay a live count.
OPEN_RESULTS_MEMO_AGE = 5.0


def results_cache(status: str) -> dict[str, str]:
    """Cache header for a results page, depending on the vote's status."""
    return OPEN_RESULTS_CACHE if status == STATUS_OPEN else CLOSED_RESULTS_CACHE


def results_memo_age(status: str) -> float:
    """Max age of memoised figures for this page.

    The memo is shared, so on an open vote it could hide a voter's own ballot:
    `cast_vote` forgets its law's figures once the ballot is committed.
    """
    return OPEN_RESULTS_MEMO_AGE if status == STATUS_OPEN else float(CLOSED_RESULTS_MAX_AGE)


def visible(db: Session, slug: str) -> Law | None:
    """The law for this slug if it's public (published and not archived), else None."""
    law = laws_crud.get_by_slug(db, slug)
    return law if law is not None and law.is_public else None


_RETRY_AFTER = {"Retry-After": "60"}

# The limit is per IP address, often shared: the visitor may have done nothing.
_SHARED_CONNECTION = (
    "Cette limite s'applique à une connexion internet, et beaucoup de personnes "
    "en partagent une - c'est le cas de la plupart des connexions mobiles. Il est "
    "donc possible que vous n'y soyez pour rien."
)


def too_many_votes(request: Request) -> HTMLResponse:
    """429 page for votes, without giving the exact threshold."""
    return templates.TemplateResponse(
        request,
        "erreur.html",
        {
            "titre": "Trop de votes depuis cette connexion",
            "message": (
                "Beaucoup de votes ont été envoyés depuis votre connexion ces "
                f"dernières minutes. {_SHARED_CONNECTION} Réessayez dans une "
                "minute : votre vote sera enregistré normalement."
            ),
        },
        status_code=429,
        headers=_RETRY_AFTER,
    )


def too_many_exports(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "erreur.html",
        {
            "titre": "Trop de téléchargements depuis cette connexion",
            "message": (
                "Beaucoup de fichiers ont été téléchargés depuis votre connexion "
                f"ces dernières minutes. {_SHARED_CONNECTION} Les données restent "
                "publiques et complètes : réessayez dans une minute."
            ),
        },
        status_code=429,
        headers=_RETRY_AFTER,
    )


def exports_busy(request: Request) -> HTMLResponse:
    """503 when too many exports are running: not the caller's fault."""
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
