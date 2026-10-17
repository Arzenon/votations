"""Admin pages for the laws staged by the daily Assemblée nationale import."""

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

# Status filter options. "tous" means no filter.
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
            # For the sortable "Séances" column header.
            "sort_by_seances": imports_crud.ORDER_BY_SEANCES,
            # Whether to show the "Réinitialiser" link.
            "filtered": bool(procedure)
            or tri != imports_crud.ORDER_BY_DATE
            or statut != STATUS_NEW,
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
    # Back to the view that shows ignored rows.
    return RedirectResponse(f"/admin/imports?statut={_ALL_STATUSES}", status_code=303)


@router.post("/imports/{item_id}/brouillon", response_class=HTMLResponse)
def draft_import(
    request: Request,
    item_id: int,
    db: Session = Depends(get_db),
    form: LawForm = Depends(draft_form),
    regenerer: str = Form(""),
):
    """Schedule a draft, or show the stored one.

    Drafting takes minutes, so this only schedules the job and the form polls
    `draft_state`. A stored draft is reused unless "Régénérer" is pressed
    (each run costs money).

    Normally called via HTMX, so refreshing the page doesn't resubmit the POST.
    The full page response is the no-JS fallback.
    """
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)

    # `mark_draft_pending` ensures a double click only starts one run.
    if not (item.draft and not regenerer) and imports_crud.mark_draft_pending(db, item):
        schedule_draft(item.id)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "admin/_draft_state.html", draft_state_context(item)
        )

    # Already turned into a law: go to its edit form, not a creation form that
    # would create a duplicate.
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
    """Dismiss a failed draft's banner. A POST, so a prefetch can't clear it.

    Only failed drafts can be dismissed, not running ones.
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
    """Polled by HTMX while a draft is running."""
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin/_draft_state.html", draft_state_context(item))
