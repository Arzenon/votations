"""Law page, voting and results.

The ballot is rendered both inside the law page and alone, as the fragment
HTMX swaps in after a vote.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app import profile_cookie, ratelimit, seo
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
    STATUS_CLOSED,
    STATUS_OPEN,
    Law,
    VoteChoice,
)
from app.routes.public.common import (
    PUBLIC_PAGE_CACHE,
    not_found,
    results_cache,
    results_memo_age,
    too_many_votes,
    visible,
)
from app.templating import templates
from app.textdiff import full_change

router = APIRouter()


def _demographic_form_context(chosen: dict[str, str] | None = None) -> dict:
    """Options and current selection for the three optional dropdowns.

    ``chosen`` (keyed by form field name) keeps the visitor's selection when
    their ballot is refused.
    """
    return {
        "age_brackets": DIMENSION_ORDER[DIMENSION_AGE],
        "sexes": DIMENSION_ORDER[DIMENSION_SEXE],
        "regions": DIMENSION_ORDER[DIMENSION_REGION],
        "chosen": chosen or {},
    }


def _back_to(law: Law) -> str:
    """The law's URL, built from the stored slug rather than from the request."""
    return f"/loi/{law.slug}"


def _carry_cookies(source: Response, target: Response) -> Response:
    """Copy Set-Cookie headers from ``source`` to ``target``."""
    for key, value in source.raw_headers:
        if key.lower() == b"set-cookie":
            target.raw_headers.append((key, value))
    return target


# Messages shown when a vote is refused.
GONE_NOTICE = "Cette votation n'est plus disponible. Rechargez la page pour voir son état actuel."
CLOSED_NOTICE = "Cette votation vient de se clore : votre vote n'a pas pu être enregistré."
NO_CHOICE_NOTICE = "Choisissez « Pour », « Contre » ou « Abstention » avant de valider."
TOO_MANY_NOTICE = (
    "Beaucoup de votes ont été envoyés depuis votre connexion ces dernières "
    "minutes - une connexion internet est souvent partagée entre plusieurs "
    "personnes, vous n'y êtes peut-être pour rien. Réessayez dans une minute."
)


def _ballot_fragment(
    request: Request,
    law: Law | None,
    *,
    status: str = STATUS_CLOSED,
    existing_choice: str | None = None,
    demographics_locked: bool = False,
    notice: str = "",
    chosen: dict[str, str] | None = None,
) -> HTMLResponse:
    """The ballot fragment, for HTMX.

    Refusals return this with a 200 for HTMX requests: htmx would swap a
    whole redirected page into the ballot, and ignores the body of a 4xx.
    """
    return templates.TemplateResponse(
        request,
        "_ballot.html",
        {
            "law": law,
            "status": status,
            "voting_open": status == STATUS_OPEN,
            "existing_choice": existing_choice,
            "choices": list(VoteChoice),
            "demographics_locked": demographics_locked,
            "notice": notice,
            **_demographic_form_context(chosen),
        },
    )


def _refused_ballot(
    request: Request,
    db: Session,
    law: Law,
    auth: AuthProvider,
    *,
    notice: str,
    chosen: dict[str, str] | None = None,
) -> HTMLResponse:
    """Re-render the visitor's ballot with the refusal message.

    Keeps their existing vote and their profile selection (``chosen``).
    """
    voter_token = auth.peek_voter_token(request, law.id)
    existing = votes_crud.get_existing(db, law.id, voter_token) if voter_token else None
    return _ballot_fragment(
        request,
        law,
        status=law.status(),
        existing_choice=existing.choice if existing else None,
        demographics_locked=bool(existing and existing.demographics_recorded),
        notice=notice,
        chosen=chosen,
    )


def law_page_context(
    db: Session,
    law: Law,
    *,
    origin: str,
    status: str,
    existing_choice: str | None = None,
    demographics_locked: bool = False,
    preview: bool = False,
) -> dict:
    """Context for ``law.html``. Also used by the admin preview."""
    # The tally is only shown once the vote is closed. law.id is None for the
    # preview of an unsaved law.
    if status == STATUS_OPEN:
        tally = None
    elif law.id:
        tally = results_crud.tally(db, law.id, max_age=results_memo_age(law))
    else:
        tally = results_crud.empty_tally()

    return {
        "law": law,
        "status": status,
        "voting_open": status == STATUS_OPEN,
        "existing_choice": existing_choice,
        "tally": tally,
        "choices": list(VoteChoice),
        "demographics_locked": demographics_locked,
        "revision_count": laws_crud.count_revisions(db, law.id) if law.id else 0,
        "preview": preview,
        # For the "Signaler une erreur" link; hidden if empty.
        "contact_email": get_settings().legal_contact_email,
        "jsonld": seo.graph(
            seo.legislation(origin, law),
            seo.breadcrumb(origin, [("Accueil", "/"), (law.title, f"/loi/{law.slug}")]),
        ),
        **_demographic_form_context(),
    }


@router.get("/loi/{slug}", response_class=HTMLResponse)
def law_page(
    request: Request,
    slug: str,
    db: Session = Depends(get_db),
    auth: AuthProvider = Depends(get_auth_provider),
):
    law = visible(db, slug)
    if law is None:
        return not_found(request)

    # Don't create a token for readers, only for voters.
    voter_token = auth.peek_voter_token(request, law.id)
    existing = votes_crud.get_existing(db, law.id, voter_token) if voter_token else None

    return templates.TemplateResponse(
        request,
        "law.html",
        law_page_context(
            db,
            law,
            origin=str(request.base_url).rstrip("/"),
            status=law.status(),
            existing_choice=existing.choice if existing else None,
            demographics_locked=bool(existing and existing.demographics_recorded),
        ),
    )


@router.post("/loi/{slug}/vote")
def cast_vote(
    request: Request,
    slug: str,
    # Optional so an empty ballot gets our message, not a validation error.
    choice: str = Form(""),
    age_bracket: str = Form(""),
    sexe: str = Form(""),
    region: str = Form(""),
    db: Session = Depends(get_db),
    auth: AuthProvider = Depends(get_auth_provider),
):
    htmx = bool(request.headers.get("HX-Request"))
    # Sent back with any refusal so the dropdowns aren't reset.
    chosen = {"age_bracket": age_bracket, "sexe": sexe, "region": region}

    law = visible(db, slug)
    if law is None:
        if htmx:
            return _ballot_fragment(request, None, notice=GONE_NOTICE)
        return not_found(request)

    # Rate limit, since a new cookie means a new voter. Checked first, but only
    # recorded once we know the request is a real ballot (the limit is shared by
    # everyone behind the same IP).
    settings = get_settings()
    throttle = ratelimit.Throttle(
        limit=settings.vote_rate_limit,
        window_seconds=settings.vote_rate_window_seconds,
        bucket="vote",
    )
    if ratelimit.over_limit(request, **throttle):
        if htmx:
            return _refused_ballot(request, db, law, auth, notice=TOO_MANY_NOTICE, chosen=chosen)
        return too_many_votes(request)

    try:
        vote_choice = VoteChoice(choice)
    except ValueError:
        if htmx:
            return _refused_ballot(request, db, law, auth, notice=NO_CHOICE_NOTICE, chosen=chosen)
        return RedirectResponse(_back_to(law), status_code=303)

    # A vote on a just-closed law still counts towards the limit.
    if ratelimit.too_many(request, **throttle):
        if htmx:
            return _refused_ballot(request, db, law, auth, notice=TOO_MANY_NOTICE, chosen=chosen)
        return too_many_votes(request)

    carrier = Response()  # collects cookies set by the helpers
    voter_token = auth.get_voter_token(request, carrier, law.id)

    # One transaction until the commit below. `cast_vote` locks the vote row
    # meanwhile, so concurrent ballots can't count the profile twice.
    try:
        vote, previous_choice = votes_crud.cast_vote(db, law, voter_token, vote_choice)

        # Before adding this ballot's profile, so a voter can't trigger a snapshot
        # that includes themselves.
        demo_crud.refresh_snapshot(db, law)

        # Optional profile, added to the counters once. Previous categories come
        # from the cookie.
        profile = profile_cookie.load(request)
        on_file = demo_crud.record(
            db,
            vote=vote,
            choice=vote_choice,
            previous_choice=previous_choice,
            age_bracket=age_bracket,
            sexe=sexe,
            region=region,
            previous_categories=profile.for_law(law.id),
        )
        # Closed while this ballot was being recorded: refused like a late one.
        # The commit right after stays well inside `_CLOSING_GRACE`, so the
        # final figures (see `app.crud.demographics`) never move after it.
        if not law.is_open():
            raise votes_crud.VotingClosed(law.slug)
    except votes_crud.VotingClosed:
        db.rollback()
        if htmx:
            return _refused_ballot(request, db, law, auth, notice=CLOSED_NOTICE, chosen=chosen)
        return RedirectResponse(_back_to(law), status_code=303)
    db.commit()
    # Only after the commit, so the cookie never refers to a rolled-back vote.
    profile.remember(carrier, law.id, on_file)

    # HTMX gets the fragment, otherwise redirect.
    if htmx:
        fragment = _ballot_fragment(
            request,
            law,
            status=law.status(),
            existing_choice=vote_choice.value,
            demographics_locked=vote.demographics_recorded,
        )
        return _carry_cookies(carrier, fragment)

    return _carry_cookies(carrier, RedirectResponse(_back_to(law), status_code=303))


@router.get("/loi/{slug}/resultats", response_class=HTMLResponse)
def results_page(
    request: Request,
    slug: str,
    # Page number of the revision history.
    revisions: int = 1,
    db: Session = Depends(get_db),
):
    law = visible(db, slug)
    if law is None:
        return not_found(request)
    # Memoised briefly while open, longer once closed (see `results_memo_age`).
    status = law.status()
    memo_age = results_memo_age(law)
    tally = results_crud.tally(db, law.id, max_age=memo_age)
    evolution = results_crud.evolution(db, law.id, total=tally.total, max_age=memo_age)
    # Pass with_profile to save a query (see `_live_categories`).
    breakdowns = demo_crud.all_breakdowns(db, law, profiled=tally.with_profile)
    breakdowns_as_of = demo_crud.published_as_of(law, breakdowns)
    history = laws_crud.revisions(db, law.id, page=revisions)
    settings = get_settings()
    origin = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "law": law,
            "status": status,
            "voting_open": status == STATUS_OPEN,
            # The dataset, plus a minimal reference to the law it's `about`.
            "jsonld": seo.graph(
                seo.legislation_ref(origin, law),
                seo.votation_dataset(origin, law),
                seo.breadcrumb(
                    origin,
                    [
                        ("Accueil", "/"),
                        (law.title, f"/loi/{law.slug}"),
                        ("Résultats", f"/loi/{law.slug}/resultats"),
                    ],
                ),
            ),
            "tally": tally,
            "evolution": evolution,
            "chart": build_evolution_chart(evolution),
            "breakdowns": breakdowns,
            # Over for good (see `demo_crud.is_final`).
            "final": demo_crud.is_final(law),
            "min_cell_count": settings.min_cell_count,
            # Snapshot date, shown on the page. None when the figures are live.
            "breakdowns_as_of": breakdowns_as_of,
            "min_bucket_volume": settings.min_bucket_volume,
            "provision": demo_crud.provision_rate(db, law.id, tally=tally),
            "export_note": results_crud.export_note(db, law.id, total=tally.total),
            "revisions": history.rows,
            "revision_total": history.total,
            "revision_page": history.number,
            "revision_pages": history.count,
        },
        headers=results_cache(status),
    )


# A stored id: digits without a leading zero, within Postgres's integer. Anything
# else is a 404.
_REVISION_ID = re.compile(r"[1-9][0-9]{0,8}")


@router.get("/loi/{slug}/modifications/{revision_id}", response_class=HTMLResponse)
def revision_page(request: Request, slug: str, revision_id: str, db: Session = Depends(get_db)):
    """One change in full, for those too large to diff on the results page."""
    law = visible(db, slug)
    revision = (
        laws_crud.get_revision(db, law.id, int(revision_id))
        if law is not None and _REVISION_ID.fullmatch(revision_id)
        else None
    )
    if law is None or revision is None:
        return not_found(request)
    return templates.TemplateResponse(
        request,
        "revision.html",
        {
            "law": law,
            "rev": revision,
            "change": full_change(revision.old_value, revision.new_value),
        },
        headers=PUBLIC_PAGE_CACHE,
    )
