"""Administration of the laws themselves: dashboard, CRUD, archiving."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import imports as imports_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import get_db
from app.models import (
    DIMENSION_AGE,
    DIMENSION_ORDER,
    DIMENSION_REGION,
    DIMENSION_SEXE,
    DRAFT_FAILED,
    STATUS_IMPORTED,
    STATUS_OPEN,
    VoteChoice,
)
from app.routes.admin.context import cached_draft, law_form_context
from app.routes.admin.forms import LawForm, law_form
from app.routes.admin.guard import require_admin
from app.templating import templates

router = APIRouter()


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    page: int = 1,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    items, total, pages = laws_crud.list_all(db, page=page)
    settings = get_settings()
    last_import = imports_crud.last_import_at(db)
    stale_after = settings.an_import_stale_after_hours
    hours_since = None
    if last_import is not None:
        delta = datetime.now(tz=last_import.tzinfo) - last_import
        hours_since = delta.total_seconds() / 3600
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "laws": items,
            "total": total,
            "page": min(max(1, page), pages),
            "pages": pages,
            "tallies": results_crud.tallies_for(db, [law.id for law in items]),
            "pending_imports": imports_crud.count_new(db),
            "archived_count": laws_crud.count_archived(db),
            "last_import": last_import,
            "import_is_stale": hours_since is None or hours_since > stale_after,
            "import_stale_after_hours": stale_after,
        },
    )


@router.get("/archives", response_class=HTMLResponse)
def archived_laws(
    request: Request,
    page: int = 1,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Laws withdrawn from the site. Restorable, or purgeable on purpose."""
    items, total, pages = laws_crud.list_all(db, page=page, archived=True)
    return templates.TemplateResponse(
        request,
        "admin/archives.html",
        {
            "laws": items,
            "total": total,
            "page": min(max(1, page), pages),
            "pages": pages,
            "tallies": results_crud.tallies_for(db, [law.id for law in items]),
        },
    )


@router.get("/lois/nouvelle", response_class=HTMLResponse)
def new_law_form(
    request: Request,
    import_id: int | None = None,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # A staged law pre-fills the factual fields only (title, official reference,
    # Légifrance link). The four narrative blocks stay empty on purpose: they are
    # written and validated by a person before anything is published.
    prefill = imports_crud.get_by_id(db, import_id) if import_id else None
    if import_id and prefill is None:
        raise HTTPException(404)
    # A draft already generated for this law is shown straight away: it has been
    # paid for, and hiding it behind another click helps nobody.
    draft, arguments, notes, eli_url = cached_draft(prefill)
    context = law_form_context(
        prefill=prefill, draft=draft, arguments=arguments, notes=notes, eli_url=eli_url
    )

    # A failure is news exactly once. Showing it on this render and clearing it
    # afterwards means the administrator sees why the draft did not happen, and
    # is not told again on every later visit. The context above already holds a
    # snapshot of the status, so clearing now does not affect what is rendered.
    #
    # A *pending* run is left strictly alone: clearing it would stop the polling
    # banner while the job is still going, and the outcome would land on a row
    # nobody is watching.
    if prefill is not None and prefill.draft_status == DRAFT_FAILED:
        imports_crud.clear_draft_state(db, prefill)

    return templates.TemplateResponse(request, "admin/law_form.html", context)


@router.get("/lois/{law_id}", response_class=HTMLResponse)
def edit_law_form(
    request: Request, law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        law_form_context(law=law, revisions=laws_crud.revisions(db, law.id)),
    )


@router.post("/lois")
def create_law(
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
    form: LawForm = Depends(law_form),
    import_id: int | None = Form(None),
):
    law = laws_crud.create(db, **form.columns())
    if import_id:
        item = imports_crud.get_by_id(db, import_id)
        if item is not None:
            imports_crud.set_status(db, item, status=STATUS_IMPORTED, law_id=law.id)
    return RedirectResponse("/admin", status_code=303)


@router.post("/lois/{law_id}")
def update_law(
    law_id: int,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
    form: LawForm = Depends(law_form),
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    laws_crud.update(db, law, **form.columns())
    return RedirectResponse("/admin", status_code=303)


@router.get("/lois/{law_id}/apercu", response_class=HTMLResponse)
def preview_law(
    request: Request, law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    """The public page of a law, as it will look once published.

    Renders the real public template rather than an approximation, because the
    whole point is to catch what the real one does — a summary that reads badly
    in paragraphs, an argument block left empty, a title that wraps awkwardly.
    Publishing to see your own work was the only way to check it before this.

    The ballot is shown in the state the voting window implies, with its submit
    button disabled: nothing here can accept a vote, and the vote route would
    refuse one anyway since the law is not public.
    """
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    status = law.scheduled_status()
    return templates.TemplateResponse(
        request,
        "law.html",
        {
            "law": law,
            "status": status,
            "existing_choice": None,
            "tally": results_crud.tally(db, law.id) if status != STATUS_OPEN else None,
            "choices": list(VoteChoice),
            "demographics_locked": False,
            "revision_count": laws_crud.count_revisions(db, law.id),
            "preview": True,
            "age_brackets": DIMENSION_ORDER[DIMENSION_AGE],
            "sexes": DIMENSION_ORDER[DIMENSION_SEXE],
            "regions": DIMENSION_ORDER[DIMENSION_REGION],
        },
    )


@router.get("/lois/{law_id}/archiver", response_class=HTMLResponse)
def confirm_archive(
    request: Request, law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    """Ask before withdrawing a law, on a page rather than in a JS dialog.

    The confirmation used to be a ``data-confirm`` attribute handled by
    ``app.js``. With JavaScript off — which the rest of the site is built to
    survive — one click destroyed a votation and every vote on it.
    """
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "admin/confirm.html",
        {
            "law": law,
            "tally": results_crud.tally(db, law.id),
            "action": f"/admin/lois/{law.id}/archiver",
            "titre": "Archiver cette votation ?",
            "explication": (
                "La votation quittera le site : elle ne sera plus consultable, ni "
                "votable, ni exportable. Les votes déjà exprimés sont conservés et "
                "l'archivage peut être annulé à tout moment."
            ),
            "bouton": "Archiver",
            "danger": False,
        },
    )


@router.post("/lois/{law_id}/archiver")
def archive_law(law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    law = laws_crud.get_by_id(db, law_id)
    if law is not None:
        laws_crud.archive(db, law)
    return RedirectResponse("/admin", status_code=303)


@router.post("/lois/{law_id}/restaurer")
def restore_law(law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    law = laws_crud.get_by_id(db, law_id)
    if law is not None:
        laws_crud.restore(db, law)
    return RedirectResponse("/admin/archives", status_code=303)


@router.get("/lois/{law_id}/supprimer", response_class=HTMLResponse)
def confirm_purge(
    request: Request, law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    """The second of the two decisions that deleting a votation now takes."""
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    if not law.is_archived:
        # Purging is reachable only from the archive, so that destroying data is
        # never one click away from the everyday dashboard.
        raise HTTPException(
            status_code=409,
            detail="Cette votation doit d'abord être archivée avant de pouvoir être supprimée.",
        )
    return templates.TemplateResponse(
        request,
        "admin/confirm.html",
        {
            "law": law,
            "tally": results_crud.tally(db, law.id),
            "action": f"/admin/lois/{law.id}/supprimer",
            "titre": "Supprimer définitivement cette votation ?",
            "explication": (
                "La votation et tous ses votes seront effacés de la base. Les "
                "données ouvertes déjà publiées ne pourront plus être regénérées, "
                "et cette opération est irréversible. À réserver à la libération "
                "d'espace disque, une fois les exports archivés hors ligne."
            ),
            "bouton": "Supprimer définitivement",
            "danger": True,
        },
    )


@router.post("/lois/{law_id}/supprimer")
def purge_law(law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        return RedirectResponse("/admin/archives", status_code=303)
    if not law.is_archived:
        raise HTTPException(
            status_code=409,
            detail="Cette votation doit d'abord être archivée avant de pouvoir être supprimée.",
        )
    laws_crud.purge(db, law)
    return RedirectResponse("/admin/archives", status_code=303)
