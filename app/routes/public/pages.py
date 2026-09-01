"""The pages that are not about one law: home, search, legal notices, sitemap.

Every route here renders the same bytes for every visitor, which is why they all
opt into :data:`~app.routes.public.common.PUBLIC_PAGE_CACHE`.
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


# A search box is not a place to paste a novel. Long enough for any real query,
# short enough that three unanchored ILIKEs stay cheap.
MAX_QUERY_CHARS = 120


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    bands = laws_crud.published_by_state(db)
    # One query for every card's count, the same way the administration's own
    # listing does it — not one per law. Participation is shown but never sorted
    # on: ordering the page by it would make visibility follow votes and votes
    # follow visibility, which is the loop the charter's anti-manipulation rule
    # exists to refuse.
    #
    # One query is not the same thing as a cheap one: it groups every vote ever
    # cast on up to a hundred and fifty votations, to draw a hundred and fifty
    # cards, on the busiest page of the site. `max_age` lets that answer be
    # reused for as long as this page's own Cache-Control already tells the
    # reader it may be — the same number, deliberately — and a ballot forgets the
    # law it was cast on, so what is reused is never a figure that has moved.
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
        headers=PUBLIC_PAGE_CACHE,
    )


@router.get("/mentions-legales", response_class=HTMLResponse)
def legal_notice(request: Request):
    """The host, and why the publisher is not named — LCEN art. 6-III.

    Only the host is mandatory here. Article 6-III-2 lets a non-professional
    publisher keep their own name and address off the page provided the host
    holds them, so those fields do not exist in the configuration at all: a field
    nobody is obliged to fill is a field that gets filled by accident.
    """
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "mentions_legales.html",
        {
            "contact_email": settings.legal_contact_email,
            "host": settings.legal_host,
            # The host alone decides whether the page is complete.
            "complete": bool(settings.legal_host),
        },
        headers=PUBLIC_PAGE_CACHE,
    )


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request, db: Session = Depends(get_db)):
    """Every public URL, for search engines.

    Built from the same query the home page uses, so an archived or unpublished
    law can never appear here — a sitemap that lists a 404 is worse than none.
    """
    base = str(request.base_url).rstrip("/")
    # `/recherche` is deliberately absent: it carries `noindex` (see the template
    # for why), and a sitemap is a list of pages one is *asking* to have indexed.
    # Listing a page there and refusing it on the page itself is a contradiction
    # search engines report as an error, on the one file whose job is to be a
    # plain statement of what this site holds.
    static_pages = ["/", "/connexion", "/vie-privee", "/mentions-legales"]
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
