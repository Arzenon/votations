"""Administration of the laws themselves: dashboard, CRUD, archiving."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import imports as imports_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import as_utc, get_db, utcnow
from app.models import STATUS_IMPORTED, Law
from app.routes.admin.context import cached_draft, law_form_context
from app.routes.admin.forms import LawForm, law_form, preview_form
from app.routes.public.exports import at_capacity, csv_response
from app.routes.public.laws import law_page_context
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
        # Both sides in UTC. Reading the zone off the stored value and asking for
        # "now" in it worked only because the column is timestamptz; the day it
        # handed back a naive value, this would have compared local wall-clock
        # time against a UTC instant and mis-stated the delay by the offset.
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
    """Laws withdrawn from the site. Restorable, or purgeable on purpose."""
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
    # A staged law pre-fills the factual fields only (title, official reference,
    # Légifrance link). The four narrative blocks stay empty on purpose: they are
    # written and validated by a person before anything is published.
    prefill = imports_crud.get_by_id(db, import_id) if import_id else None
    if import_id and prefill is None:
        raise HTTPException(404)
    # A draft already generated for this law is shown straight away: it has been
    # paid for, and hiding it behind another click helps nobody.
    #
    # A failed run leaves its banner up until the administrator dismisses it —
    # see `dismiss_draft_failure`. Clearing it here, while rendering, made a GET
    # destroy the only record of why the draft had not happened: a link
    # prefetch, a browser preload or a stray double-tap consumed the message
    # before anyone read it.
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
    # Show the cached AI draft in the four written fields instead of what is
    # stored. Nothing is written by asking: the draft is loaded into the form and
    # saved only if « Enregistrer » is pressed afterwards.
    brouillon: int = 0,
    db: Session = Depends(get_db),
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)

    # The staged row this law came from, when it came from one. Without it the
    # whole AI drafting feature lived on the *creation* form only and vanished
    # the moment the law was saved — « Régénérer » included, so a presentation
    # could never be redrafted once it existed.
    prefill = imports_crud.get_by_law_id(db, law.id)
    draft, arguments, notes, _eli = cached_draft(prefill) if brouillon else (None, None, [], "")

    # The most recent page only. The full history, paginated, is the public one on
    # the results page — which is where a reader checks it, and where it belongs.
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
    """Render one law through the real public template, saving nothing.

    The public page's own context builder, not a copy of it: a copy drifts, and a
    preview that has drifted from the page is worse than no preview.

    The ballot is shown in the state the voting window implies, with its submit
    button disabled: nothing here can accept a vote, and the vote route would
    refuse one anyway while the law is not public.
    """
    return templates.TemplateResponse(
        request,
        "law.html",
        law_page_context(db, law, status=law.scheduled_status(), preview=True),
    )


@router.get("/lois/{law_id}/apercu", response_class=HTMLResponse)
def preview_law(request: Request, law_id: int, db: Session = Depends(get_db)):
    """The stored law, as the public page will show it once published.

    A plain link, so it can be bookmarked and so the administration's own listing
    has somewhere to point a draft at — a draft's public URL is a 404 by design,
    which used to make its title in the dashboard a dead link.
    """
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
    """The page as the form currently reads, **without saving anything**.

    The preview above can only show what is already stored, so seeing your own
    work meant saving it first — and for a law that does not exist yet, creating
    it. That is the wrong order: the reason to look at the public page is to
    decide whether the text is good enough to keep, and the answer arrived after
    the text had been kept.

    So this one takes the posted form and renders it. The ``Law`` it builds is
    never added to the session and never committed; it exists for the length of
    one render. ``law_id`` identifies the row being edited, which is what lets an
    existing law keep its real tally, its real history and its real URL while the
    prose on the page is the unsaved prose in the textarea.
    """
    law = Law(**form.columns())
    stored = laws_crud.get_by_id(db, law_id) if law_id else None
    if stored is not None:
        # Borrow the identity of the row being edited, and nothing else: the
        # counts and the history on the page below are read by this id.
        law.id = stored.id
        law.slug = stored.slug
        law.created_at = stored.created_at
    else:
        # A law that does not exist yet still has a URL to show — the one it will
        # get on save. Derived, not reserved: nothing is written here.
        law.slug = laws_crud.slugify(form.title)
    return _preview(request, db, law)


@router.post("/lois")
def create_law(
    db: Session = Depends(get_db),
    form: LawForm = Depends(law_form),
    import_id: int | None = Form(None),
):
    item = imports_crud.get_by_id(db, import_id) if import_id else None

    # One dossier, one votation. Creating a second from the same staged row does
    # not just duplicate the law — `set_status` below would re-point the import
    # at the new one, so the original quietly loses the link to its own draft and
    # nothing on either page says a twin exists.
    #
    # This is a guard, not the fix: the way to reach it was a banner pointing at
    # the creation form from a page where a law already existed. But the link was
    # only the way in, and a route that silently re-parents a votation should
    # refuse on its own.
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

    law = laws_crud.create(db, **form.columns())
    if item is not None:
        imports_crud.set_status(db, item, status=STATUS_IMPORTED, law_id=law.id)
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
    laws_crud.update(db, law, **form.columns())
    return RedirectResponse("/admin", status_code=303)


@router.get("/lois/{law_id}/archiver", response_class=HTMLResponse)
def confirm_archive(request: Request, law_id: int, db: Session = Depends(get_db)):
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


@router.get("/lois/{law_id}/export.csv")
def export_law_csv(request: Request, law_id: int, db: Session = Depends(get_db)):
    """The open-data CSV for any law, archived ones included.

    The public route refuses an archived law — that is what archiving means — and
    the page that offers to destroy a votation for good is reached only from the
    archive. So the one link whose job is to say « keep the data first » pointed
    at a 404, on the only screen where that mattered.

    The rows are the same the public export serves, reductions and all: archiving
    withdraws a votation from the site, it does not exempt it from the anonymity
    rules. What this route adds is reach, not detail.

    Held to the same concurrency cap as the public exports, and to no other
    limit. The cap protects the connection pool, and a connection held by an
    administrator starves the public pages exactly as anyone else's would — this
    route used to take one of those slots without ever checking for one, which is
    the one way the cap could be exceeded. The per-address *rate* limit is
    deliberately not applied: it exists to slow a caller looping over downloads,
    and refusing an administrator for downloading several archived votations in a
    sitting would only obstruct the « keep the data before you purge it » step
    this route exists for.
    """
    refused = at_capacity(request)
    if refused is not None:
        return refused
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return csv_response(db, law)


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
