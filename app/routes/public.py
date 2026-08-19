"""Public-facing routes: home, law/vote page, results, export, search."""

from __future__ import annotations

import csv
import io
import math

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth.base import AuthProvider, get_auth_provider
from app.crud import demographics as demo_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.crud import votes as votes_crud
from app.db import get_db
from app.models import AgeBracket, Region, Sexe, VoteChoice
from app.templating import templates

router = APIRouter()

AGE_BRACKETS = [b.value for b in AgeBracket]
SEXES = [s.value for s in Sexe]
REGIONS = [r.value for r in Region]


def _demographic_form_context() -> dict:
    return {"age_brackets": AGE_BRACKETS, "sexes": SEXES, "regions": REGIONS}


@router.get("/", response_class=HTMLResponse)
def home(request: Request, q: str | None = None, db: Session = Depends(get_db)):
    items = laws_crud.list_published(db, query=q)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"laws": items, "q": q or ""},
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

    response = HTMLResponse()  # placeholder so the provider can set the cookie
    voter_token = auth.get_voter_token(request, response)
    existing = votes_crud.get_existing(db, law.id, voter_token)
    tally = results_crud.tally(db, law.id)
    evolution = results_crud.evolution(db, law.id)

    rendered = templates.TemplateResponse(
        request,
        "law.html",
        {
            "law": law,
            "status": law.status(),
            "existing_choice": existing.choice if existing else None,
            "tally": tally,
            "evolution": evolution,
            "choices": list(VoteChoice),
            "demographics_locked": bool(existing and existing.demographics_recorded),
            **_demographic_form_context(),
        },
    )
    # Carry over any Set-Cookie the auth provider issued.
    for key, value in response.raw_headers:
        if key.lower() == b"set-cookie":
            rendered.raw_headers.append((key, value))
    return rendered


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

    try:
        vote_choice = VoteChoice(choice)
    except ValueError:
        return RedirectResponse(f"/loi/{slug}", status_code=303)

    response = Response()
    voter_token = auth.get_voter_token(request, response)

    try:
        vote = votes_crud.cast_vote(db, law, voter_token, vote_choice)
    except votes_crud.VotingClosed:
        return RedirectResponse(f"/loi/{slug}", status_code=303)

    # Optional, anonymous: folded once into aggregate counters (no per-voter row).
    demo_crud.record(
        db, vote=vote, choice=vote_choice, age_bracket=age_bracket, sexe=sexe, region=region
    )

    tally = results_crud.tally(db, law.id)

    # HTMX request → return just the ballot/results fragment; else full-page redirect.
    if request.headers.get("HX-Request"):
        fragment = templates.TemplateResponse(
            request,
            "_ballot.html",
            {
                "law": law,
                "status": law.status(),
                "existing_choice": vote_choice.value,
                "tally": tally,
                "choices": list(VoteChoice),
                "demographics_locked": vote.demographics_recorded,
                **_demographic_form_context(),
            },
        )
        for key, value in response.raw_headers:
            if key.lower() == b"set-cookie":
                fragment.raw_headers.append((key, value))
        return fragment

    redirect = RedirectResponse(f"/loi/{slug}", status_code=303)
    for key, value in response.raw_headers:
        if key.lower() == b"set-cookie":
            redirect.raw_headers.append((key, value))
    return redirect


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
        },
    )


@router.get("/loi/{slug}/export.csv")
def export_csv(slug: str, db: Session = Depends(get_db)):
    law = laws_crud.get_by_slug(db, slug)
    if law is None or not law.published:
        return JSONResponse({"detail": "not found"}, status_code=404)
    rows = results_crud.export_rows(db, law.id)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["law_id", "choice", "created_at"])
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        media_type="text/csv",
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
            "demographics_note": (
                "Répartitions agrégées uniquement, masquées sous le seuil "
                "d'anonymat. Aucune ligne par votant·e. 'reference_pct' = ordre "
                "de grandeur national INSEE (approx.), jamais utilisé pour "
                "repondérer."
            ),
        }
    )


def _not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
