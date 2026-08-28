"""One law: reading it, voting on it, and reading the result.

The three URLs that carry a slug and are about the votation itself. The ballot
lives here too — it is rendered both as part of the law page and on its own, as
the fragment HTMX swaps back after a vote, and the two must tell the same story.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
    STATUS_CLOSED,
    STATUS_OPEN,
    Law,
    VoteChoice,
)
from app.routes.public.common import not_found, results_cache, too_many_votes, visible
from app.templating import templates

router = APIRouter()


def _demographic_form_context() -> dict:
    return {
        "age_brackets": DIMENSION_ORDER[DIMENSION_AGE],
        "sexes": DIMENSION_ORDER[DIMENSION_SEXE],
        "regions": DIMENSION_ORDER[DIMENSION_REGION],
    }


def _back_to(law: Law) -> str:
    """This law's own URL, built from the stored slug rather than the path.

    The two are the same string today — `visible` looked the law up by that very
    parameter, and every slug in the table went through `slugify`, which strips
    anything a header could be split on. Building the `Location` from the column
    means it stays true of code that has not been written yet: a slug is what the
    database holds, not what the caller typed.
    """
    return f"/loi/{law.slug}"


def _carry_cookies(source: Response, target: Response) -> Response:
    """Move any Set-Cookie an auth/profile helper wrote onto the real response."""
    for key, value in source.raw_headers:
        if key.lower() == b"set-cookie":
            target.raw_headers.append((key, value))
    return target


# Every way the vote route can refuse, in the words the voter reads. They exist
# as constants because each one is used twice: once in the full page and once in
# the HTMX fragment, and the two must not drift into telling different stories.
GONE_NOTICE = "Cette votation n'est plus disponible. Rechargez la page pour voir son état actuel."
CLOSED_NOTICE = "Cette votation vient de se clore : votre vote n'a pas pu être enregistré."
NO_CHOICE_NOTICE = "Choisissez « Pour », « Contre » ou « Abstention » avant de valider."
TOO_MANY_NOTICE = (
    "Beaucoup de votes ont été envoyés depuis votre connexion ces dernières "
    "minutes — une connexion internet est souvent partagée entre plusieurs "
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
) -> HTMLResponse:
    """The ballot on its own, for HTMX to swap back into the page.

    Every refusal on the vote route answers with this when the request came from
    HTMX, and with a redirect or a full error page when it did not. The two
    branches are not decoration: htmx follows a 303 and swaps whatever it lands
    on into the ballot's ``<div>``, so a vote on a law that had just closed
    nested the entire site inside the ballot; and htmx discards the body of a
    4xx, so a rate-limited voter watched the button do nothing whatsoever.

    Answering 200 here is deliberate. The refusal is carried by the fragment's
    own text, which is what the voter actually reads; the status code has no
    reader left to inform once the response is a piece of a page.
    """
    return templates.TemplateResponse(
        request,
        "_ballot.html",
        {
            "law": law,
            "status": status,
            "existing_choice": existing_choice,
            "choices": list(VoteChoice),
            "demographics_locked": demographics_locked,
            "notice": notice,
            **_demographic_form_context(),
        },
    )


def _refused_ballot(
    request: Request,
    db: Session,
    law: Law,
    auth: AuthProvider,
    *,
    notice: str,
) -> HTMLResponse:
    """The visitor's own ballot, re-rendered with the reason it was refused.

    Reads the existing vote back so a refusal leaves the radio buttons showing
    what this browser had already chosen, rather than silently clearing it.
    """
    voter_token = auth.peek_voter_token(request)
    existing = votes_crud.get_existing(db, law.id, voter_token) if voter_token else None
    return _ballot_fragment(
        request,
        law,
        status=law.status(),
        existing_choice=existing.choice if existing else None,
        demographics_locked=bool(existing and existing.demographics_recorded),
        notice=notice,
    )


def law_page_context(
    db: Session,
    law: Law,
    *,
    status: str,
    existing_choice: str | None = None,
    demographics_locked: bool = False,
    preview: bool = False,
) -> dict:
    """Everything ``law.html`` needs.

    Shared with the administration's preview, which renders this same template on
    purpose: an approximation would only catch the mistakes somebody thought to
    approximate. Sharing the context is what keeps the preview honest as the page
    grows — a block added here appears in both, or in neither.
    """
    return {
        "law": law,
        "status": status,
        "existing_choice": existing_choice,
        # Only the closed page shows a tally; counting the votes to render a
        # ballot nobody sees was wasted work on the busiest page of the site.
        "tally": results_crud.tally(db, law.id) if status != STATUS_OPEN else None,
        "choices": list(VoteChoice),
        "demographics_locked": demographics_locked,
        "revision_count": laws_crud.count_revisions(db, law.id),
        "preview": preview,
        # For « Signaler une erreur » in the provenance note: the page admits the
        # presentation may be wrong, so it has to say where to report it. Empty
        # when no contact is configured, and the offer simply disappears.
        "contact_email": get_settings().legal_contact_email,
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

    # Reading a law issues nothing: a voter token is minted when someone votes,
    # not when they visit. Somebody who only reads leaves no identifier behind.
    voter_token = auth.peek_voter_token(request)
    existing = votes_crud.get_existing(db, law.id, voter_token) if voter_token else None

    return templates.TemplateResponse(
        request,
        "law.html",
        law_page_context(
            db,
            law,
            status=law.status(),
            existing_choice=existing.choice if existing else None,
            demographics_locked=bool(existing and existing.demographics_recorded),
        ),
    )


@router.post("/loi/{slug}/vote")
def cast_vote(
    request: Request,
    slug: str,
    # Optional rather than required, so an empty ballot is answered by this route
    # instead of by FastAPI's validation error. The radio group carries `required`
    # too, so in a browser this is the fallback for the fallback.
    choice: str = Form(""),
    age_bracket: str = Form(""),
    sexe: str = Form(""),
    region: str = Form(""),
    db: Session = Depends(get_db),
    auth: AuthProvider = Depends(get_auth_provider),
):
    htmx = bool(request.headers.get("HX-Request"))

    law = visible(db, slug)
    if law is None:
        if htmx:
            return _ballot_fragment(request, None, notice=GONE_NOTICE)
        return not_found(request)

    # A fresh cookie is a fresh voter, so without this a script can add votes as
    # fast as it can open connections. Counters are held in memory only.
    settings = get_settings()
    if ratelimit.too_many(
        request,
        limit=settings.vote_rate_limit,
        window_seconds=settings.vote_rate_window_seconds,
        bucket="vote",
    ):
        if htmx:
            return _refused_ballot(request, db, law, auth, notice=TOO_MANY_NOTICE)
        return too_many_votes(request)

    try:
        vote_choice = VoteChoice(choice)
    except ValueError:
        if htmx:
            return _refused_ballot(request, db, law, auth, notice=NO_CHOICE_NOTICE)
        return RedirectResponse(_back_to(law), status_code=303)

    carrier = Response()  # collects the cookies the helpers below set
    voter_token = auth.get_voter_token(request, carrier)

    # One transaction for the whole ballot, from here to the commit below. The
    # vote row is locked by `cast_vote` and stays locked while the counters move,
    # so two ballots arriving from one browser at once cannot both count a
    # profile; and a failure anywhere in between takes the vote down with it
    # rather than leaving a vote whose counters were never moved.
    try:
        vote, previous_choice = votes_crud.cast_vote(db, law, voter_token, vote_choice)
    except votes_crud.VotingClosed:
        if htmx:
            return _refused_ballot(request, db, law, auth, notice=CLOSED_NOTICE)
        return RedirectResponse(_back_to(law), status_code=303)

    # Optional, anonymous: folded once into aggregate counters (no per-voter row).
    # The categories a voter already contributed come back from their own device,
    # keyed on this law, so changing a vote moves this law's counters and no
    # other law's. The cookie is decoded once and both read from and written to.
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
    db.commit()
    # Only once the ballot is safely stored: a cookie promising a contribution
    # that a rolled-back transaction never made would move the wrong cell on the
    # next change.
    profile.remember(carrier, law.id, on_file)

    # HTMX request → return just the ballot fragment; else full-page redirect.
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
    # The page of the modification history, not of the results. Named so it reads
    # as what it pages through, since it is the only paged thing here.
    revisions: int = 1,
    db: Session = Depends(get_db),
):
    law = visible(db, slug)
    if law is None:
        return not_found(request)
    # One pass over this law's votes, shared by everything below that needs it.
    # Four separate COUNT(*)s used to answer the same question per render.
    tally = results_crud.tally(db, law.id)
    evolution = results_crud.evolution(db, law.id, total=tally.total)
    breakdowns = demo_crud.all_breakdowns(db, law.id)
    status = law.status()
    revision_rows, revision_total, revision_pages = laws_crud.revisions(db, law.id, page=revisions)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "law": law,
            "status": status,
            "tally": tally,
            "evolution": evolution,
            "chart": build_evolution_chart(evolution),
            "breakdowns": breakdowns,
            # The suppression threshold is the same for every dimension; passing
            # it explicitly means the template does not have to reach into
            # `breakdowns[0]` and take the list being non-empty on faith.
            "min_cell_count": get_settings().min_cell_count,
            "provision": demo_crud.provision_rate(db, law.id, tally=tally),
            "export_note": results_crud.export_note(db, law.id, total=tally.total),
            "revisions": revision_rows,
            "revision_total": revision_total,
            "revision_page": min(max(1, revisions), revision_pages),
            "revision_pages": revision_pages,
        },
        headers=results_cache(status),
    )
