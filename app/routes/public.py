"""Public-facing routes: home, law/vote page, results, export, search."""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy.orm import Session

from app import profile_cookie, ratelimit
from app.auth.base import AuthProvider, get_auth_provider
from app.config import get_settings
from app.crud import demographics as demo_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.crud import votes as votes_crud
from app.db import get_db
from app.models import DIMENSION_AGE, DIMENSION_ORDER, DIMENSION_REGION, DIMENSION_SEXE, VoteChoice
from app.security import require_same_origin
from app.templating import templates

# Voting is a state change a third-party page must not be able to trigger:
# SameSite=Lax withholds an existing voter cookie from a cross-site POST but does
# not stop a fresh one being minted, so any page could otherwise turn its
# visitors into ballots.
router = APIRouter(dependencies=[Depends(require_same_origin)])


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
    page = max(1, page)
    page_size = 20
    rows, total = laws_crud.search(db, query=q, status=status, page=page, page_size=page_size)
    pages = max(1, math.ceil(total / page_size))
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "laws": rows,
            "q": q or "",
            "status": status or "",
            "page": page,
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
    law = laws_crud.get_by_slug(db, slug)
    if law is None or not law.published:
        return _not_found(request)

    # Reading a law issues nothing: a voter token is minted when someone votes,
    # not when they visit. Somebody who only reads leaves no identifier behind.
    voter_token = auth.peek_voter_token(request)
    existing = votes_crud.get_existing(db, law.id, voter_token) if voter_token else None

    return templates.TemplateResponse(
        request,
        "law.html",
        {
            "law": law,
            "status": law.status(),
            "existing_choice": existing.choice if existing else None,
            "tally": results_crud.tally(db, law.id),
            "choices": list(VoteChoice),
            "demographics_locked": bool(existing and existing.demographics_recorded),
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
    law = laws_crud.get_by_slug(db, slug)
    if law is None or not law.published:
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
    # so changing a vote moves the counters instead of leaving them stale.
    on_file = demo_crud.record(
        db,
        vote=vote,
        choice=vote_choice,
        age_bracket=age_bracket,
        sexe=sexe,
        region=region,
        previous=profile_cookie.read(request),
    )
    profile_cookie.write(carrier, on_file)

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
    law = laws_crud.get_by_slug(db, slug)
    if law is None or not law.published:
        return _not_found(request)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "law": law,
            "status": law.status(),
            "tally": results_crud.tally(db, law.id),
            "evolution": results_crud.evolution(db, law.id),
            "breakdowns": demo_crud.all_breakdowns(db, law.id),
            "provision": demo_crud.provision_rate(db, law.id),
            "export_note": results_crud.export_note(db, law.id),
        },
    )


@router.get("/loi/{slug}/export.csv")
def export_csv(slug: str, db: Session = Depends(get_db)):
    law = laws_crud.get_by_slug(db, slug)
    if law is None or not law.published:
        return JSONResponse({"detail": "not found"}, status_code=404)

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
def export_json(slug: str, db: Session = Depends(get_db)):
    law = laws_crud.get_by_slug(db, slug)
    if law is None or not law.published:
        return JSONResponse({"detail": "not found"}, status_code=404)
    # Demographic aggregates are included only where they clear cell suppression.
    demographics = []
    for bd in demo_crud.all_breakdowns(db, law.id):
        if bd.shown:
            demographics.append(
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
            )
    provision = demo_crud.provision_rate(db, law.id)
    return JSONResponse(
        {
            "law": {"id": law.id, "slug": law.slug, "title": law.title},
            "votes": results_crud.export_rows(db, law.id),
            "demographics": demographics,
            "profile_provision": {
                "total_votes": provision.total_votes,
                "with_profile": provision.with_profile,
                "percentage": provision.percentage,
            },
            "votes_note": results_crud.export_note(db, law.id),
            "demographics_note": (
                "Répartitions agrégées uniquement, masquées sous le seuil "
                "d'anonymat. Aucune ligne par votant·e. 'reference_pct' = ordre "
                "de grandeur national INSEE (approx.), jamais utilisé pour "
                "repondérer."
            ),
        }
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


def _not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
