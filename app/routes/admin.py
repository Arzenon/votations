"""Administration routes: law CRUD, scheduling, live tally, export.

Guarded by HTTP Basic against env-configured credentials for the MVP. This is a
provisional measure; V3 replaces it with FranceConnect-based roles.
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app import ratelimit
from app.ai import LawDraft, SelectedArguments
from app.config import get_settings
from app.crud import imports as imports_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import get_db
from app.drafting import build_draft
from app.models import (
    DRAFT_FAILED,
    DRAFT_PENDING,
    STATUS_IGNORED,
    STATUS_IMPORTED,
    STATUS_NEW,
)
from app.security import require_same_origin, safe_url
from app.templating import templates

# Every admin route inherits the cross-site check; it is a no-op on reads.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_same_origin)])

# RFC 7617 makes `realm` mandatory in a Basic challenge. Chrome tolerates its
# absence and prompts anyway; Firefox does not, and shows the bare 401 body
# instead of a login box — so the realm is what makes the prompt appear there.
# Keep it ASCII: header values are latin-1 encoded.
AUTH_REALM = "Votations"
security = HTTPBasic(realm=AUTH_REALM)


def _unauthorised() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Identifiants administrateur invalides",
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
    )


def require_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Check the provisional HTTP Basic credentials, with a throttle on failures.

    Constant-time comparison stops the password leaking through timing; the rate
    limit stops it being guessed at network speed, which a static credential pair
    with no lockout would otherwise allow. Only *failures* are counted, so a
    working administrator is never slowed down.
    """
    settings = get_settings()
    ok_user = secrets.compare_digest(credentials.username, settings.admin_username)
    ok_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (ok_user and ok_pass):
        ratelimit.too_many(
            request,
            limit=settings.admin_rate_limit,
            window_seconds=settings.admin_rate_window_seconds,
            bucket="admin",
        )
        raise _unauthorised()
    if ratelimit.over_limit(
        request,
        limit=settings.admin_rate_limit,
        window_seconds=settings.admin_rate_window_seconds,
        bucket="admin",
    ):
        # Correct credentials, but this address has just been guessing.
        raise HTTPException(
            status_code=429,
            detail="Trop de tentatives de connexion. Réessayez dans quelques minutes.",
        )
    return credentials.username


class InvalidFormValue(ValueError):
    """A field the administrator typed cannot be read; rendered as a 400 page."""


def _display_tz() -> ZoneInfo:
    """The zone administrators write opening and closing times in."""
    try:
        return ZoneInfo(get_settings().display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_dt(value: str | None) -> datetime | None:
    """Read a `datetime-local` field as civil time in the display zone.

    The column is timestamptz. Storing the naive value straight from the form made
    Postgres read it as UTC, so a vote scheduled to close at 20 h in Paris closed
    at 22 h. Attaching the zone here — and stripping it again in `_dt_local` — is
    what makes the form mean what it says.
    """
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidFormValue(f"date ou heure illisible : « {value} »") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_display_tz())


def _dt_local(value: datetime | None) -> str:
    """Render a stored instant back into the form's local-time field."""
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(_display_tz()).strftime("%Y-%m-%dT%H:%M")


def _parse_arguments(raw: str | None) -> list[dict]:
    """Accept JSON list of {text, source, author}; empty/invalid → [].

    ``source`` is sanitised rather than trusted: it is rendered as an href on the
    public page, and V3 opens this form to every authenticated user.
    """
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [
                {
                    "text": str(d.get("text", "")),
                    "source": safe_url(str(d.get("source", ""))),
                    "author": str(d.get("author", "")),
                }
                for d in data
                if isinstance(d, dict)
            ]
    except json.JSONDecodeError:
        pass
    return []


MOIS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def _french_date(value: date) -> str:
    return f"{value.day} {MOIS[value.month - 1]} {value.year}"


def _official_ref(item) -> str:
    """The canonical citation for a promulgated law, e.g. 'LOI n° 2026-532 du 24 juin 2026'."""
    return f"LOI n° {item.law_code} du {_french_date(item.promulgated_on)}"


def _form_values(
    *,
    law=None,
    prefill=None,
    draft: LawDraft | None = None,
    arguments: SelectedArguments | None = None,
    typed: dict | None = None,
    eli_url: str = "",
) -> dict:
    """What each field of the law form should show.

    Precedence is always: what the admin typed, then an AI draft, then the staged
    import, then empty — so regenerating a draft never discards edits in progress.
    """
    typed = {k: v for k, v in (typed or {}).items() if v not in (None, "", [])}
    if law is not None:
        base = {
            "title": law.title,
            "official_ref": law.official_ref or "",
            "legifrance_url": law.legifrance_url or "",
            "summary_text": law.summary_text,
            "practical_changes": law.practical_changes,
            "arguments_for": law.arguments_for,
            "arguments_against": law.arguments_against,
            "opens_at": _dt_local(law.opens_at),
            "closes_at": _dt_local(law.closes_at),
            "published": law.published,
        }
    else:
        base = {
            "title": prefill.title if prefill else "",
            "official_ref": _official_ref(prefill) if prefill else "",
            # The Journal Officiel's ELI link outlives the one the AN publishes.
            "legifrance_url": eli_url or (prefill.legifrance_url if prefill else "") or "",
            "summary_text": draft.summary_text if draft else "",
            "practical_changes": draft.practical_changes if draft else "",
            "arguments_for": arguments.pour if arguments else [],
            "arguments_against": arguments.contre if arguments else [],
            "opens_at": "",
            "closes_at": "",
            "published": False,
        }
    base.update(typed)
    return base


def _cached_draft(item):
    """Rehydrate a stored draft. Returns (draft, arguments, notes, eli_url)."""
    cached = getattr(item, "draft", None) if item else None
    if not cached:
        return None, None, [], ""
    return (
        LawDraft(
            summary_text=cached.get("summary_text", ""),
            practical_changes=cached.get("practical_changes", ""),
        ),
        SelectedArguments(
            pour=cached.get("arguments_for") or [],
            contre=cached.get("arguments_against") or [],
            reserves=cached.get("reserves") or [],
        ),
        list(cached.get("notes") or []),
        cached.get("eli_url") or "",
    )


def _law_form_context(
    *,
    law=None,
    prefill=None,
    draft: LawDraft | None = None,
    arguments: SelectedArguments | None = None,
    notes: list[str] | None = None,
    eli_url: str = "",
    typed: dict | None = None,
) -> dict:
    """Everything ``admin/law_form.html`` needs, from one place.

    The template serves creation, editing and drafting; building its context in a
    single function is what keeps those three call sites from drifting apart.
    """
    return {
        "law": law,
        "prefill": prefill,
        "values": _form_values(
            law=law,
            prefill=prefill,
            draft=draft,
            arguments=arguments,
            typed=typed,
            eli_url=eli_url,
        ),
        "drafted_at": prefill.drafted_at if prefill else None,
        "argument_notes": list(notes or []),
        "reserves": arguments.reserves if arguments else [],
        "has_draft": bool(prefill and prefill.draft),
        "draft_status": prefill.draft_status if prefill else "",
        "draft_error": prefill.draft_error if prefill else "",
        "ai_enabled": bool(prefill) and bool(get_settings().anthropic_api_key),
    }


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    items = laws_crud.list_all(db)
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
            "tallies": results_crud.tallies_for(db, [law.id for law in items]),
            "pending_imports": imports_crud.count_new(db),
            "last_import": last_import,
            "import_is_stale": hours_since is None or hours_since > stale_after,
            "import_stale_after_hours": stale_after,
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
    draft, arguments, notes, eli_url = _cached_draft(prefill)
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        _law_form_context(
            prefill=prefill, draft=draft, arguments=arguments, notes=notes, eli_url=eli_url
        ),
    )


@router.get("/lois/{law_id}", response_class=HTMLResponse)
def edit_law_form(
    request: Request, law_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)
):
    law = laws_crud.get_by_id(db, law_id)
    if law is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin/law_form.html", _law_form_context(law=law))


def _law_fields(
    *,
    title: str,
    official_ref: str,
    legifrance_url: str,
    summary_text: str,
    practical_changes: str,
    arguments_for: str,
    arguments_against: str,
    opens_at: str,
    closes_at: str,
    published: str,
) -> dict:
    """The validated column values behind the law form, shared by create and update."""
    return {
        "title": title,
        "official_ref": official_ref or None,
        "legifrance_url": safe_url(legifrance_url) or None,
        "summary_text": summary_text,
        "practical_changes": practical_changes,
        "arguments_for": _parse_arguments(arguments_for),
        "arguments_against": _parse_arguments(arguments_against),
        "opens_at": _parse_dt(opens_at),
        "closes_at": _parse_dt(closes_at),
        "published": bool(published),
    }


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
    import_id: int | None = Form(None),
):
    law = laws_crud.create(
        db,
        **_law_fields(
            title=title,
            official_ref=official_ref,
            legifrance_url=legifrance_url,
            summary_text=summary_text,
            practical_changes=practical_changes,
            arguments_for=arguments_for,
            arguments_against=arguments_against,
            opens_at=opens_at,
            closes_at=closes_at,
            published=published,
        ),
    )
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
        **_law_fields(
            title=title,
            official_ref=official_ref,
            legifrance_url=legifrance_url,
            summary_text=summary_text,
            practical_changes=practical_changes,
            arguments_for=arguments_for,
            arguments_against=arguments_against,
            opens_at=opens_at,
            closes_at=closes_at,
            published=published,
        ),
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
    error: str | None = None,
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
            "ai_enabled": bool(get_settings().anthropic_api_key),
            "error": error,
        },
    )


@router.post("/imports/{item_id}/ignorer")
def ignore_import(item_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    imports_crud.set_status(db, item, status=STATUS_IGNORED)
    return RedirectResponse("/admin/imports", status_code=303)


@router.post("/imports/{item_id}/restaurer")
def restore_import(item_id: int, _: str = Depends(require_admin), db: Session = Depends(get_db)):
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    imports_crud.set_status(db, item, status=STATUS_NEW)
    return RedirectResponse("/admin/imports?statut=tous", status_code=303)


@router.post("/imports/{item_id}/brouillon", response_class=HTMLResponse)
def draft_import(
    request: Request,
    item_id: int,
    background: BackgroundTasks,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
    regenerer: str = Form(""),
    title: str = Form(""),
    summary_text: str = Form(""),
    practical_changes: str = Form(""),
    arguments_for: str = Form(""),
    arguments_against: str = Form(""),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    published: str = Form(""),
):
    """Schedule the presentation draft, or show the one already stored.

    Producing a draft downloads Journal Officiel archives and calls a model twice
    — minutes of work, which no browser or reverse proxy will hold a request open
    for. So this route only *schedules* the job and answers immediately; the form
    polls :func:`draft_state` until the outcome is in.

    Generating is billable, so a draft is produced once and stored on the staged
    row; reopening it costs nothing. Only « Régénérer » calls the model again.
    """
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)

    typed = {
        "title": title.strip(),
        "summary_text": summary_text.strip(),
        "practical_changes": practical_changes.strip(),
        "arguments_for": _parse_arguments(arguments_for),
        "arguments_against": _parse_arguments(arguments_against),
        "opens_at": opens_at,
        "closes_at": closes_at,
        "published": bool(published),
    }

    if (item.draft and not regenerer) or item.draft_status == DRAFT_PENDING:
        draft, arguments, notes, eli_url = _cached_draft(item)
    else:
        imports_crud.mark_draft_pending(db, item)
        background.add_task(build_draft, item.id)
        draft, arguments, notes, eli_url = _cached_draft(item)

    db.refresh(item)
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        _law_form_context(
            prefill=item,
            draft=draft,
            arguments=arguments,
            notes=notes,
            eli_url=eli_url,
            typed=typed,
        ),
    )


@router.get("/imports/{item_id}/brouillon/etat", response_class=HTMLResponse)
def draft_state(
    request: Request,
    item_id: int,
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """The polling target for a draft in progress (HTMX swaps this fragment)."""
    item = imports_crud.get_by_id(db, item_id)
    if item is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request,
        "admin/_draft_state.html",
        {
            "item": item,
            "draft_status": item.draft_status,
            "draft_error": item.draft_error,
            "pending": item.draft_status == DRAFT_PENDING,
            "failed": item.draft_status == DRAFT_FAILED,
        },
    )
