"""Administration routes: law CRUD, scheduling, live tally, export.

Guarded by HTTP Basic against env-configured credentials for the MVP. This is a
provisional measure; V3 replaces it with FranceConnect-based roles.
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import imports as imports_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import get_db
from app.models import STATUS_IGNORED, STATUS_IMPORTED, STATUS_NEW
from app.templating import templates

router = APIRouter(prefix="/admin")

# RFC 7617 makes `realm` mandatory in a Basic challenge. Chrome tolerates its
# absence and prompts anyway; Firefox does not, and shows the bare 401 body
# instead of a login box — so the realm is what makes the prompt appear there.
# Keep it ASCII: header values are latin-1 encoded.
AUTH_REALM = "Votations"
security = HTTPBasic(realm=AUTH_REALM)


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    settings = get_settings()
    ok_user = secrets.compare_digest(credentials.username, settings.admin_username)
    ok_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Identifiants administrateur invalides",
            headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
        )
    return credentials.username


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # HTML datetime-local yields "YYYY-MM-DDTHH:MM".
    return datetime.fromisoformat(value)


def _parse_arguments(raw: str | None) -> list[dict]:
    """Accept JSON list of {text, source, author}; empty/invalid → []."""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [
                {
                    "text": str(d.get("text", "")),
                    "source": str(d.get("source", "")),
                    "author": str(d.get("author", "")),
                }
                for d in data
                if isinstance(d, dict)
            ]
    except json.JSONDecodeError:
        pass
    return []


MOIS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def _french_date(value: date) -> str:
    return f"{value.day} {MOIS[value.month - 1]} {value.year}"


def _official_ref(item) -> str:
    """The canonical citation for a promulgated law, e.g. 'LOI n° 2026-532 du 24 juin 2026'."""
    return f"LOI n° {item.law_code} du {_french_date(item.promulgated_on)}"


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    items = laws_crud.list_all(db)
    tallies = {law.id: results_crud.tally(db, law.id) for law in items}
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {"laws": items, "tallies": tallies, "pending_imports": imports_crud.count_new(db)},
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
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        {
            "law": None,
            "prefill": prefill,
            "prefill_ref": _official_ref(prefill) if prefill else "",
        },
    )


@router.get("/lois/{law_id}", response_class=HTMLResponse)
def edit_law_form(
    request: Request, law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "admin/law_form.html", {"law": law, "prefill": None, "prefill_ref": ""}
    )


@router.post("/lois")
def create_law(
    request: Request,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
    title: str = Form(...),
    official_ref: str = Form(""),
    legifrance_url: str = Form(""),
    summary_text: str = Form(""),
    practical_changes: str = Form(""),
    arguments_for: str = Form(""),
    arguments_against: str = Form(""),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    published: str = Form(""),
    import_id: str = Form(""),
):
    law = laws_crud.create(
        db,
        title=title,
        official_ref=official_ref or None,
        legifrance_url=legifrance_url or None,
        summary_text=summary_text,
        practical_changes=practical_changes,
        arguments_for=_parse_arguments(arguments_for),
        arguments_against=_parse_arguments(arguments_against),
        opens_at=_parse_dt(opens_at),
        closes_at=_parse_dt(closes_at),
        published=bool(published),
    )
    if import_id:
        item = imports_crud.get_by_id(db, int(import_id))
        if item is not None:
            imports_crud.set_status(db, item, status=STATUS_IMPORTED, law_id=law.id)
    return RedirectResponse("/admin", status_code=303)


@router.post("/lois/{law_id}")
def update_law(
    law_id: int,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
    title: str = Form(...),
    official_ref: str = Form(""),
    legifrance_url: str = Form(""),
    summary_text: str = Form(""),
    practical_changes: str = Form(""),
    arguments_for: str = Form(""),
    arguments_against: str = Form(""),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    published: str = Form(""),
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    laws_crud.update(
        db,
        law,
        title=title,
        official_ref=official_ref or None,
        legifrance_url=legifrance_url or None,
        summary_text=summary_text,
        practical_changes=practical_changes,
        arguments_for=_parse_arguments(arguments_for),
        arguments_against=_parse_arguments(arguments_against),
        opens_at=_parse_dt(opens_at),
        closes_at=_parse_dt(closes_at),
        published=bool(published),
    )
    return RedirectResponse("/admin", status_code=303)


@router.post("/lois/{law_id}/supprimer")
def delete_law(law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    law = laws_crud.get_by_id(db, law_id)
    if law is not None:
        laws_crud.delete(db, law)
    return RedirectResponse("/admin", status_code=303)


@router.get("/imports", response_class=HTMLResponse)
def imports_list(
    request: Request,
    statut: str = "nouveau",
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Laws found by the daily Assemblée nationale import, awaiting a decision."""
    wanted = None if statut == "tous" else statut
    items = imports_crud.list_by_status(db, status=wanted)
    return templates.TemplateResponse(
        request,
        "admin/imports.html",
        {
            "items": items,
            "statut": statut,
            "pending_imports": imports_crud.count_new(db),
        },
    )


@router.post("/imports/{item_id}/ignorer")
def ignore_import(
    item_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    imports_crud.set_status(db, item, status=STATUS_IGNORED)
    return RedirectResponse("/admin/imports", status_code=303)


@router.post("/imports/{item_id}/restaurer")
def restore_import(
    item_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    imports_crud.set_status(db, item, status=STATUS_NEW)
    return RedirectResponse("/admin/imports?statut=tous", status_code=303)
