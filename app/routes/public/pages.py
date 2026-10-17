"""Pages not tied to a law: home, search, legal pages, sitemap.

All are the same for every visitor and use ``PUBLIC_PAGE_CACHE``.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app import seo
from app.config import get_settings
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import get_db
from app.routes.public.common import PUBLIC_PAGE_CACHE, PUBLIC_PAGE_MAX_AGE
from app.templating import TAGLINE, templates

router = APIRouter()


# Keeps the tsquery and the ILIKE cheap.
MAX_QUERY_CHARS = 120


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    bands = laws_crud.published_by_state(db)
    # Participation is shown but never used for sorting: sorting by votes would
    # bring more votes to the most visible laws.
    tallies = results_crud.tallies_for(
        db,
        [law.id for _, laws in bands for law in laws],
        max_age=PUBLIC_PAGE_MAX_AGE,
    )
    origin = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "bands": bands,
            "tallies": tallies,
            "excerpt_chars": laws_crud.EXCERPT_CHARS,
            "jsonld": seo.graph(seo.website(origin, description=TAGLINE)),
        },
        headers=PUBLIC_PAGE_CACHE,
    )


@router.get("/recherche", response_class=HTMLResponse)
def search(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    db: Session = Depends(get_db),
):
    q = (q or "")[:MAX_QUERY_CHARS]
    found = laws_crud.search(db, query=q or None, status=status, page=page)
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "laws": found.rows,
            "q": q,
            "status": status or "",
            "page": found.number,
            "pages": found.count,
            "total": found.total,
        },
        headers=PUBLIC_PAGE_CACHE,
    )


@router.get("/vie-privee", response_class=HTMLResponse)
def privacy(request: Request):
    """Privacy page. No consent banner: both cookies are strictly necessary for
    voting, so exempt under article 82 of the loi Informatique et Libertés."""
    return templates.TemplateResponse(
        request,
        "vie_privee.html",
        {"contact_email": get_settings().legal_contact_email},
        headers=PUBLIC_PAGE_CACHE,
    )


@router.get("/mentions-legales", response_class=HTMLResponse)
def legal_notice(request: Request):
    """Mentions légales (LCEN art. 6-III). Only the host is mandatory."""
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "mentions_legales.html",
        {
            "contact_email": settings.legal_contact_email,
            "host": settings.legal_host,
            "complete": bool(settings.legal_host),
        },
        headers=PUBLIC_PAGE_CACHE,
    )


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request, db: Session = Depends(get_db)):
    """All public URLs, for search engines. Only public laws are listed."""
    base = str(request.base_url).rstrip("/")
    # No /recherche: it's noindex.
    static_pages = ["/", "/connexion", "/vie-privee", "/mentions-legales"]
    # (location, last modified)
    urls: list[tuple[str, str | None]] = [(f"{base}{path}", None) for path in static_pages]
    for slug, changed_at in laws_crud.public_urls(db):
        moment = changed_at.date().isoformat()
        urls.append((f"{base}/loi/{slug}", moment))
        urls.append((f"{base}/loi/{slug}/resultats", moment))

    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in urls:
        entry = f"<url><loc>{escape(loc)}</loc>"
        if lastmod:
            entry += f"<lastmod>{lastmod}</lastmod>"
        body.append(entry + "</url>")
    body.append("</urlset>")
    return Response(
        "\n".join(body),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )
