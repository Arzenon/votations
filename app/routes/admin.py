"""Administration routes: law CRUD, scheduling, live tally, export.

Guarded by HTTP Basic against env-configured credentials for the MVP. This is a
provisional measure; V3 replaces it with FranceConnect-based roles.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import get_db
from app.templating import templates

router = APIRouter(prefix="/admin")
security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    settings = get_settings()
    ok_user = secrets.compare_digest(credentials.username, settings.admin_username)
    ok_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Identifiants administrateur invalides",
            headers={"WWW-Authenticate": "Basic"},
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


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    items = laws_crud.list_all(db)
    tallies = {law.id: results_crud.tally(db, law.id) for law in items}
    return templates.TemplateResponse(
        request, "admin/dashboard.html", {"laws": items, "tallies": tallies}
    )


@router.get("/lois/nouvelle", response_class=HTMLResponse)
def new_law_form(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin/law_form.html", {"law": None})


@router.get("/lois/{law_id}", response_class=HTMLResponse)
def edit_law_form(
    request: Request, law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin/law_form.html", {"law": law})


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
):
    laws_crud.create(
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
