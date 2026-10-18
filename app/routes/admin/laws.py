"""Admin pages for laws: dashboard, create/edit, preview, archiving."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import imports as imports_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import as_utc, get_db, utcnow
from app.models import ImportedLaw, Law
from app.routes.admin.context import cached_draft, law_form_context
from app.routes.admin.forms import InvalidFormValue, LawForm, dt_local, law_form, preview_form
from app.routes.public.exports import at_capacity, csv_response
from app.routes.public.laws import law_page_context
from app.security import is_head
from app.templating import templates

router = APIRouter()


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    page: int = 1,
    db: Session = Depends(get_db),
):
    listing = laws_crud.list_all(db, page=page)
    settings = get_settings()
    last_import = imports_crud.last_import_at(db)
    stale_after = settings.an_import_stale_after_hours
    hours_since = None
    if last_import is not None:
        hours_since = (utcnow() - as_utc(last_import)).total_seconds() / 3600
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "laws": listing.rows,
            "total": listing.total,
            "page": listing.number,
            "pages": listing.count,
            "tallies": results_crud.tallies_for(db, [law.id for law in listing.rows]),
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
    db: Session = Depends(get_db),
):
    """Archived laws, which can be restored or permanently deleted."""
    listing = laws_crud.list_all(db, page=page, archived=True)
    return templates.TemplateResponse(
        request,
        "admin/archives.html",
        {
            "laws": listing.rows,
            "total": listing.total,
            "page": listing.number,
            "pages": listing.count,
            "tallies": results_crud.tallies_for(db, [law.id for law in listing.rows]),
        },
    )


@router.get("/lois/nouvelle", response_class=HTMLResponse)
def new_law_form(
    request: Request,
    import_id: int | None = None,
    db: Session = Depends(get_db),
):
    # A staged law pre-fills the factual fields (title, reference, link).
    prefill = imports_crud.get_by_id(db, import_id) if import_id else None
    if import_id and prefill is None:
        raise HTTPException(404)
    # Show an existing draft right away. A failure banner stays until dismissed
    # (see `dismiss_draft_failure`), not cleared by this GET.
    draft, arguments, notes, eli_url = cached_draft(prefill)
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        law_form_context(
            prefill=prefill, draft=draft, arguments=arguments, notes=notes, eli_url=eli_url
        ),
    )


@router.get("/lois/{law_id}", response_class=HTMLResponse)
def edit_law_form(
    request: Request,
    law_id: int,
    # Load the stored AI draft into the form (saved only on "Enregistrer").
    brouillon: int = 0,
    db: Session = Depends(get_db),
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)

    # The staged row, if any, so drafting is available on the edit form too.
    prefill = imports_crud.get_by_law_id(db, law.id)
    draft, arguments, notes, _eli = cached_draft(prefill) if brouillon else (None, None, [], "")

    # Latest page only; the full history is on the public results page.
    history = laws_crud.revisions(db, law.id)
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        law_form_context(
            law=law,
            prefill=prefill,
            draft=draft,
            arguments=arguments,
            notes=notes,
            revisions=history.rows,
            revision_total=history.total,
        ),
    )


def _preview(request: Request, db: Session, law: Law) -> HTMLResponse:
    """Render a law with the public template and context, without saving.

    The ballot reflects the voting window, with the submit button disabled.
    """
    return templates.TemplateResponse(
        request,
        "law.html",
        law_page_context(
            db,
            law,
            origin=str(request.base_url).rstrip("/"),
            status=law.scheduled_status(),
            preview=True,
        ),
    )


@router.get("/lois/{law_id}/apercu", response_class=HTMLResponse)
def preview_law(request: Request, law_id: int, db: Session = Depends(get_db)):
    """Preview of the saved law (an unpublished law's public URL is a 404)."""
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return _preview(request, db, law)


@router.post("/lois/apercu", response_class=HTMLResponse)
def preview_edits(
    request: Request,
    db: Session = Depends(get_db),
    form: LawForm = Depends(preview_form),
    law_id: int | None = Form(None),
):
    """Preview of the form's current content, without saving.

    The `Law` built here is never added to the session. ``law_id`` lets an
    existing law keep its id, slug, tally and history in the preview.
    """
    law = Law(**form.columns())
    stored = laws_crud.get_by_id(db, law_id) if law_id else None
    if stored is not None:
        # Only the identity of the saved row.
        law.id = stored.id
        law.slug = stored.slug
        law.created_at = stored.created_at
    else:
        # The slug it would probably get on save.
        law.slug = laws_crud.slugify(form.title)
    return _preview(request, db, law)


def _parliament_record(item: ImportedLaw | None) -> dict:
    """The Assemblée vote record, copied from the staged row (not editable)."""
    if item is None:
        return {}
    return {name: getattr(item, name) for name in imports_crud.PARLIAMENT_RECORD}


@router.post("/lois")
def create_law(
    db: Session = Depends(get_db),
    form: LawForm = Depends(law_form),
    import_id: int | None = Form(None),
):
    # Locked until the law is linked: a double click waits here, then gets the 409.
    item = db.get(ImportedLaw, import_id, with_for_update=True) if import_id else None

    # One law per dossier. Otherwise the import's link would move to the new
    # law and the first one would lose its draft.
    if item is not None and item.law_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ce dossier a déjà produit une votation (n° {item.law_id}). "
                "Modifiez-la plutôt que d'en créer une seconde : une seule "
                "votation par dossier, sinon les votes se répartissent entre deux "
                "pages qui portent le même texte."
            ),
        )

    laws_crud.create(db, imported=item, **form.columns(), **_parliament_record(item))
    return RedirectResponse("/admin", status_code=303)


@router.post("/lois/{law_id}")
def update_law(
    law_id: int,
    db: Session = Depends(get_db),
    form: LawForm = Depends(law_form),
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    columns = form.columns()
    # A time the form shows unchanged keeps its stored value: the form drops the
    # seconds, and reads the repeated autumn hour as its first occurrence.
    for name in ("opens_at", "closes_at"):
        if dt_local(columns[name]) == dt_local(getattr(law, name)):
            columns[name] = getattr(law, name)
    columns["closes_at"] = laws_crud.closing_from_now(db, law, columns["closes_at"])
    if laws_crud.moves_final_closing(db, law, columns["closes_at"]):
        raise InvalidFormValue(
            "« Clôture du vote » ne peut plus changer : cette votation est close et a "
            "reçu des votes. La rouvrir permettrait de comparer ses résultats de "
            "clôture aux suivants, et d'en isoler un vote"
        )
    laws_crud.update(db, law, **columns)
    return RedirectResponse("/admin", status_code=303)


@router.get("/lois/{law_id}/archiver", response_class=HTMLResponse)
def confirm_archive(request: Request, law_id: int, db: Session = Depends(get_db)):
    """Confirmation page before archiving (works without JavaScript)."""
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


@router.get("/lois/{law_id}/export.csv")
def export_law_csv(request: Request, law_id: int, db: Session = Depends(get_db)):
    """CSV export for any law, including archived ones (to save data before a purge).

    Same rows and anonymity rules as the public export. Subject to the
    concurrency cap but not the rate limit.
    """
    refused = at_capacity(request)
    if refused is not None:
        return refused
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return csv_response(db, law, head=is_head(request))


# Missing law: GETs and `update_law` return a 404. Archive, restore and purge
# are idempotent and redirect as on success, so resubmitting the form after
# going back doesn't show an error.


@router.post("/lois/{law_id}/archiver")
def archive_law(law_id: int, db: Session = Depends(get_db)):
    law = laws_crud.get_by_id(db, law_id)
    if law is not None:
        laws_crud.archive(db, law)
    return RedirectResponse("/admin", status_code=303)


@router.post("/lois/{law_id}/restaurer")
def restore_law(law_id: int, db: Session = Depends(get_db)):
    law = laws_crud.get_by_id(db, law_id)
    if law is not None:
        laws_crud.restore(db, law)
    return RedirectResponse("/admin/archives", status_code=303)


@router.get("/lois/{law_id}/supprimer", response_class=HTMLResponse)
def confirm_purge(request: Request, law_id: int, db: Session = Depends(get_db)):
    """Confirmation page before permanent deletion."""
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    if not law.is_archived:
        # Only archived laws can be deleted.
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
def purge_law(law_id: int, db: Session = Depends(get_db)):
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
