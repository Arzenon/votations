"""The pages that are not about one law: home, search, legal notices, sitemap.

Every route here renders the same bytes for every visitor, which is why they all
opt into :data:`~app.routes.public.common.PUBLIC_PAGE_CACHE`.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import laws as laws_crud
from app.db import get_db
from app.routes.public.common import PUBLIC_PAGE_CACHE
from app.templating import templates

router = APIRouter()


# A search box is not a place to paste a novel. Long enough for any real query,
# short enough that three unanchored ILIKEs stay cheap.
MAX_QUERY_CHARS = 120


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"laws": laws_crud.list_published(db)},
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
    rows, total, pages = laws_crud.search(db, query=q or None, status=status, page=page)
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "laws": rows,
            "q": q,
            "status": status or "",
            "page": min(max(1, page), pages),
            "pages": pages,
            "total": total,
        },
        headers=PUBLIC_PAGE_CACHE,
    )


@router.get("/vie-privee", response_class=HTMLResponse)
def privacy(request: Request):
    """What is stored, where, and for how long.

    Kept as an ordinary page rather than a consent banner: both cookies this site
    sets are exempt from consent under article 82 of the loi Informatique et
    Libertés — they are written only when someone votes, and the vote cannot work
    without them — so the obligation here is to inform, not to ask.
    """
    return templates.TemplateResponse(
        request,
        "vie_privee.html",
        {"contact_email": get_settings().legal_contact_email},
    )


@router.get("/mentions-legales", response_class=HTMLResponse)
def legal_notice(request: Request):
    """Publisher, publication director and host — mandatory under LCEN art. 6-III."""
    settings = get_settings()
    fields = {
        "editor": settings.legal_editor,
        "editor_address": settings.legal_editor_address,
        "publication_director": settings.legal_publication_director,
        "contact_email": settings.legal_contact_email,
        "host": settings.legal_host,
    }
    return templates.TemplateResponse(
        request,
        "mentions_legales.html",
        {**fields, "complete": all(fields.values())},
    )


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request, db: Session = Depends(get_db)):
    """Every public URL, for search engines.

    Built from the same query the home page uses, so an archived or unpublished
    law can never appear here — a sitemap that lists a 404 is worse than none.
    """
    base = str(request.base_url).rstrip("/")
    static_pages = ["/", "/recherche", "/connexion", "/vie-privee", "/mentions-legales"]
    # (location, last modified). The static pages have no date; annotated because
    # otherwise the element type is read off the first comprehension alone, and
    # the law entries below stop type-checking against it.
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
