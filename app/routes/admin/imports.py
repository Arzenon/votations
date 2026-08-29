"""Administration of the laws staged by the daily Assemblée nationale import."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import imports as imports_crud
from app.db import get_db
from app.drafting import schedule_draft
from app.models import DRAFT_FAILED, STATUS_IGNORED, STATUS_IMPORTED, STATUS_NEW
from app.routes.admin.context import cached_draft, draft_state_context, law_form_context
from app.routes.admin.forms import LawForm, draft_form
from app.templating import templates

router = APIRouter()

#: The status filter, in the order an administrator works through it. « tous » is
#: not a stored status — it is this page's word for "no filter" — which is why it
#: sits here beside the three that are, rather than being derived from them.
_ALL_STATUSES = "tous"
STATUS_CHOICES = (
    (STATUS_NEW, "Nouvelles"),
    (STATUS_IMPORTED, "Importées"),
    (STATUS_IGNORED, "Ignorées"),
    (_ALL_STATUSES, "Tout l'historique"),
)

SORT_CHOICES = (
    (imports_crud.ORDER_BY_DATE, "Date de promulgation"),
    (imports_crud.ORDER_BY_SEANCES, "Séances publiques"),
)


@router.get("/imports", response_class=HTMLResponse)
def imports_list(
    request: Request,
    statut: str = STATUS_NEW,
    # Triage: narrow to one kind of text, and rank by how much it was argued over.
    procedure: str = "",
    tri: str = imports_crud.ORDER_BY_DATE,
    page: int = 1,
    db: Session = Depends(get_db),
):
    """Laws found by the daily Assemblée nationale import, awaiting a decision."""
    wanted = None if statut == _ALL_STATUSES else statut
    staged = imports_crud.list_by_status(
        db, status=wanted, procedure=procedure or None, order=tri, page=page
    )
    return templates.TemplateResponse(
        request,
        "admin/imports.html",
        {
            "items": staged.rows,
            "statut": statut,
            "procedure": procedure,
            "tri": tri,
            "status_choices": STATUS_CHOICES,
            "sort_choices": SORT_CHOICES,
            # The « Séances » column header is a second way to reach one of those
            # sorts, so it needs the value by name rather than by its position in
            # the list above.
            "sort_by_seances": imports_crud.ORDER_BY_SEANCES,
            # Whether « Réinitialiser » has anything to undo. Decided here rather
            # than by a three-clause condition in the template comparing against
            # the defaults: the defaults are this function's, not the page's.
            "filtered": bool(procedure)
            or tri != imports_crud.ORDER_BY_DATE
            or statut != STATUS_NEW,
            # Every procedure present under the current status filter, with counts,
            # so the dropdown says how many rows each choice will show.
            "procedures": imports_crud.procedures(db, status=wanted),
            "seances": imports_crud.seance_counts(db, [item.id for item in staged.rows]),
            "total": staged.total,
            "page": staged.number,
            "pages": staged.count,
            "pending_imports": imports_crud.count_new(db),
            "ai_enabled": bool(get_settings().anthropic_api_key),
        },
    )


@router.post("/imports/{item_id}/ignorer")
def ignore_import(item_id: int, db: Session = Depends(get_db)):
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    imports_crud.set_status(db, item, status=STATUS_IGNORED)
    return RedirectResponse("/admin/imports", status_code=303)


@router.post("/imports/{item_id}/restaurer")
def restore_import(item_id: int, db: Session = Depends(get_db)):
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    imports_crud.set_status(db, item, status=STATUS_NEW)
    # Back to the view the row was restored *from*, which is the one that shows
    # ignored rows at all.
    return RedirectResponse(f"/admin/imports?statut={_ALL_STATUSES}", status_code=303)


@router.post("/imports/{item_id}/brouillon", response_class=HTMLResponse)
def draft_import(
    request: Request,
    item_id: int,
    db: Session = Depends(get_db),
    form: LawForm = Depends(draft_form),
    regenerer: str = Form(""),
):
    """Schedule the presentation draft, or show the one already stored.

    Producing a draft downloads Journal Officiel archives and calls a model twice
    — minutes of work, which no browser or reverse proxy will hold a request open
    for. So this route only *schedules* the job and answers immediately; the form
    polls :func:`draft_state` until the outcome is in.

    Generating is billable, so a draft is produced once and stored on the staged
    row; reopening it costs nothing. Only « Régénérer » calls the model again —
    deliberately, and with a confirmation.

    The browser normally sends this with HTMX, which is what keeps an accidental
    second call from happening: answering a POST with a whole page left the
    browser's current request *being* that POST, so a refresh replayed it — with
    ``regenerer=1`` still in the body — and billed another run nobody asked for.
    With HTMX only the status banner is swapped, the page never navigates, and a
    refresh re-issues the ordinary GET. The full-page reply below is the
    no-JavaScript fallback.
    """
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)

    # A stored draft is shown as it is unless « Régénérer » was pressed. Anything
    # else means a run is wanted — and `mark_draft_pending` is what decides
    # whether *this* request is the one that starts it: the claim is a conditional
    # UPDATE, so a double-click schedules (and bills) exactly one run.
    if not (item.draft and not regenerer) and imports_crud.mark_draft_pending(db, item):
        schedule_draft(item.id)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "admin/_draft_state.html", draft_state_context(item)
        )

    # No JavaScript, and this row has already produced a law: send the
    # administrator back to *its* form. Re-rendering the creation form here
    # would hand them a page whose « Enregistrer » posts to /admin/lois and
    # creates a second copy of a law that already exists.
    if item.law_id:
        return RedirectResponse(f"/admin/lois/{item.law_id}", status_code=303)

    draft, arguments, notes, eli_url = cached_draft(item)
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        law_form_context(
            prefill=item,
            draft=draft,
            arguments=arguments,
            notes=notes,
            eli_url=eli_url,
            typed=form.typed(),
        ),
    )


@router.post("/imports/{item_id}/brouillon/masquer", response_class=HTMLResponse)
def dismiss_draft_failure(
    request: Request,
    item_id: int,
    db: Session = Depends(get_db),
):
    """Acknowledge a failed draft, so its banner stops being shown.

    A failure is news exactly once, and this is where that becomes true. The
    form used to clear it while *rendering* the page, which made an ordinary GET
    destroy state: a link prefetch or a preload consumed the explanation before
    anybody read it, and the administrator was left with a form that had simply
    not been filled in and no reason why.

    Only a failed run is dismissible. A pending one is left strictly alone —
    clearing it would stop the polling banner while the job is still running,
    and the outcome would land on a row nobody is watching.
    """
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    if item.draft_status == DRAFT_FAILED:
        imports_crud.clear_draft_state(db, item)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "admin/_draft_state.html", draft_state_context(item)
        )
    return RedirectResponse(f"/admin/lois/nouvelle?import_id={item.id}", status_code=303)


@router.get("/imports/{item_id}/brouillon/etat", response_class=HTMLResponse)
def draft_state(
    request: Request,
    item_id: int,
    db: Session = Depends(get_db),
):
    """The polling target for a draft in progress (HTMX swaps this fragment)."""
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin/_draft_state.html", draft_state_context(item))
