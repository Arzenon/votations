"""Public-facing routes: home, law/vote page, results, export, search, legal pages."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy.orm import Session

from app import profile_cookie, ratelimit
from app.auth.base import AuthProvider, get_auth_provider
from app.charts import build_evolution_chart
from app.config import get_settings
from app.crud import demographics as demo_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.crud import votes as votes_crud
from app.db import get_db
from app.models import (
    DIMENSION_AGE,
    DIMENSION_ORDER,
    DIMENSION_REGION,
    DIMENSION_SEXE,
    STATUS_OPEN,
    Law,
    VoteChoice,
)
from app.security import require_same_origin
from app.templating import templates

# Voting is a state change a third-party page must not be able to trigger:
# SameSite=Lax withholds an existing voter cookie from a cross-site POST but does
# not stop a fresh one being minted, so any page could otherwise turn its
# visitors into ballots.
router = APIRouter(dependencies=[Depends(require_same_origin)])

# A search box is not a place to paste a novel. Long enough for any real query,
# short enough that three unanchored ILIKEs stay cheap.
MAX_QUERY_CHARS = 120


def _demographic_form_context() -> dict:
    return {
        "age_brackets": DIMENSION_ORDER[DIMENSION_AGE],
        "sexes": DIMENSION_ORDER[DIMENSION_SEXE],
        "regions": DIMENSION_ORDER[DIMENSION_REGION],
    }


def _carry_cookies(source: Response, target: Response) -> Response:
    """Move any Set-Cookie an auth/profile helper wrote onto the real response."""
    for key, value in source.raw_headers:
        if key.lower() == b"set-cookie":
            target.raw_headers.append((key, value))
    return target


def _visible(db: Session, slug: str) -> Law | None:
    """The law behind this slug, if it may be shown at all.

    Archived laws are withdrawn from the site: they answer 404 exactly as an
    unpublished one does, on every public route including the exports.
    """
    law = laws_crud.get_by_slug(db, slug)
    return law if law is not None and law.is_public else None


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"laws": laws_crud.list_published(db)},
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
    )


@router.get("/loi/{slug}", response_class=HTMLResponse)
def law_page(
    request: Request,
    slug: str,
    db: Session = Depends(get_db),
    auth: AuthProvider = Depends(get_auth_provider),
):
    law = _visible(db, slug)
    if law is None:
        return _not_found(request)

    # Reading a law issues nothing: a voter token is minted when someone votes,
    # not when they visit. Somebody who only reads leaves no identifier behind.
    voter_token = auth.peek_voter_token(request)
    existing = votes_crud.get_existing(db, law.id, voter_token) if voter_token else None
    status = law.status()

    return templates.TemplateResponse(
        request,
        "law.html",
        {
            "law": law,
            "status": status,
            "existing_choice": existing.choice if existing else None,
            # Only the closed page shows a tally; counting the votes to render a
            # ballot nobody sees was wasted work on the busiest page of the site.
            "tally": results_crud.tally(db, law.id) if status != STATUS_OPEN else None,
            "choices": list(VoteChoice),
            "demographics_locked": bool(existing and existing.demographics_recorded),
            "revision_count": laws_crud.count_revisions(db, law.id),
            **_demographic_form_context(),
        },
    )


@router.post("/loi/{slug}/vote")
def cast_vote(
    request: Request,
    slug: str,
    choice: str = Form(...),
    age_bracket: str = Form(""),
    sexe: str = Form(""),
    region: str = Form(""),
    db: Session = Depends(get_db),
    auth: AuthProvider = Depends(get_auth_provider),
):
    law = _visible(db, slug)
    if law is None:
        return _not_found(request)

    # A fresh cookie is a fresh voter, so without this a script can add votes as
    # fast as it can open connections. Counters are held in memory only.
    settings = get_settings()
    if ratelimit.too_many(
        request,
        limit=settings.vote_rate_limit,
        window_seconds=settings.vote_rate_window_seconds,
        bucket="vote",
    ):
        return _too_many_votes(request)

    try:
        vote_choice = VoteChoice(choice)
    except ValueError:
        return RedirectResponse(f"/loi/{slug}", status_code=303)

    carrier = Response()  # collects the cookies the helpers below set
    voter_token = auth.get_voter_token(request, carrier)

    try:
        vote = votes_crud.cast_vote(db, law, voter_token, vote_choice)
    except votes_crud.VotingClosed:
        return RedirectResponse(f"/loi/{slug}", status_code=303)

    # Optional, anonymous: folded once into aggregate counters (no per-voter row).
    # The categories a voter already contributed come back from their own device,
    # keyed on this law, so changing a vote moves this law's counters and no
    # other law's.
    on_file = demo_crud.record(
        db,
        vote=vote,
        choice=vote_choice,
        age_bracket=age_bracket,
        sexe=sexe,
        region=region,
        previous=profile_cookie.read(request, law.id),
    )
    profile_cookie.write(request, carrier, law.id, on_file)

    # HTMX request → return just the ballot fragment; else full-page redirect.
    if request.headers.get("HX-Request"):
        fragment = templates.TemplateResponse(
            request,
            "_ballot.html",
            {
                "law": law,
                "status": law.status(),
                "existing_choice": vote_choice.value,
                "choices": list(VoteChoice),
                "demographics_locked": vote.demographics_recorded,
                **_demographic_form_context(),
            },
        )
        return _carry_cookies(carrier, fragment)

    return _carry_cookies(carrier, RedirectResponse(f"/loi/{slug}", status_code=303))


@router.get("/loi/{slug}/resultats", response_class=HTMLResponse)
def results_page(request: Request, slug: str, db: Session = Depends(get_db)):
    law = _visible(db, slug)
    if law is None:
        return _not_found(request)
    # One count of this law's votes, shared by everything below that needs it.
    # Four separate COUNT(*)s used to answer the same question per render.
    tally = results_crud.tally(db, law.id)
    evolution = results_crud.evolution(db, law.id, total=tally.total)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "law": law,
            "status": law.status(),
            "tally": tally,
            "evolution": evolution,
            "chart": build_evolution_chart(evolution),
            "breakdowns": demo_crud.all_breakdowns(db, law.id),
            "provision": demo_crud.provision_rate(db, law.id),
            "export_note": results_crud.export_note(db, law.id, total=tally.total),
            "revisions": laws_crud.revisions(db, law.id),
        },
    )


def _export_allowed(request: Request) -> bool:
    """Exports get their own, much tighter bucket than voting.

    One download is a full scan of a law's votes. Legitimate use is a handful of
    files; a loop over the JSON endpoint is a cheap way to keep the database busy.
    """
    settings = get_settings()
    return not ratelimit.too_many(
        request,
        limit=settings.export_rate_limit,
        window_seconds=settings.export_rate_window_seconds,
        bucket="export",
    )


@router.get("/loi/{slug}/export.csv")
def export_csv(request: Request, slug: str, db: Session = Depends(get_db)):
    law = _visible(db, slug)
    if law is None:
        return _not_found(request)
    if not _export_allowed(request):
        return _too_many_exports(request)

    def rows() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["law_id", "choice", "created_at"])
        writer.writeheader()
        for row in results_crud.iter_export_rows(db, law.id):
            writer.writerow(row)
            # Hand each chunk over as it is written, so a large votation never
            # holds the whole export in memory twice.
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
        tail = buf.getvalue()
        if tail:
            yield tail

    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="votations-{slug}.csv"'},
    )


@router.get("/loi/{slug}/export.json")
def export_json(request: Request, slug: str, db: Session = Depends(get_db)):
    law = _visible(db, slug)
    if law is None:
        return _not_found(request)
    if not _export_allowed(request):
        return _too_many_exports(request)

    total = results_crud.count_votes(db, law.id)

    # Demographic aggregates are included only where they clear cell suppression.
    demographics = [
        {
            "dimension": bd.dimension,
            "rows": [
                {
                    "category": r.category,
                    "counts": r.counts,
                    "share_pct": r.share,
                    "reference_pct": r.reference,
                }
                for r in bd.rows
            ],
        }
        for bd in demo_crud.all_breakdowns(db, law.id)
        if bd.shown
    ]
    provision = demo_crud.provision_rate(db, law.id)
    envelope = {
        "law": {"id": law.id, "slug": law.slug, "title": law.title},
        "demographics": demographics,
        "profile_provision": {
            "total_votes": provision.total_votes,
            "with_profile": provision.with_profile,
            "percentage": provision.percentage,
        },
        "votes_note": results_crud.export_note(db, law.id, total=total),
        "demographics_note": (
            "Répartitions agrégées uniquement, masquées sous le seuil "
            "d'anonymat. Aucune ligne par votant·e. 'reference_pct' = ordre "
            "de grandeur national INSEE (approx.), jamais utilisé pour "
            "repondérer."
        ),
    }

    def document() -> Iterator[str]:
        """Stream the envelope with the vote rows written one at a time.

        The whole set used to be materialised into a list and then serialised —
        two full copies of every vote in memory at once, on an endpoint anyone
        can request. The rows are the only unbounded part, so they are the part
        that streams.
        """
        head = json.dumps(envelope, ensure_ascii=False)
        # Splice the vote array into the object rather than building a second one.
        yield head[:-1] + ', "votes": ['
        first = True
        for row in results_crud.iter_export_rows(db, law.id, total=total):
            yield ("" if first else ",") + json.dumps(row, ensure_ascii=False)
            first = False
        yield "]}"

    return StreamingResponse(
        document(),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="votations-{slug}.json"'},
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
    urls = [(f"{base}{path}", None) for path in static_pages]
    for law in laws_crud.list_published(db, limit=5000):
        moment = (law.updated_at or law.created_at).date().isoformat()
        urls.append((f"{base}/loi/{law.slug}", moment))
        urls.append((f"{base}/loi/{law.slug}/resultats", moment))

    body = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
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


def _too_many_votes(request: Request) -> HTMLResponse:
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


def _too_many_exports(request: Request) -> HTMLResponse:
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


def _not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
