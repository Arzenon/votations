"""Administration routes: law CRUD, scheduling, live tally, export.

Guarded by HTTP Basic against env-configured credentials for the MVP. This is a
provisional measure; V3 replaces it with FranceConnect-based roles.
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.ai import AiDraftError, LawDraft, SelectedArguments, generate_draft, select_arguments
from app.config import get_settings
from app.crud import imports as imports_crud
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.db import get_db
from app.debates import DebatesFetchError, fetch_speeches
from app.legifrance import LegifranceFetchError, fetch_law_text
from app.models import STATUS_IGNORED, STATUS_IMPORTED, STATUS_NEW
from app.templating import templates


def _origin(url: str) -> tuple[str, str]:
    """(scheme, host:port) — the only part of a URL that identifies a site."""
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def require_same_origin(request: Request) -> None:
    """Reject state-changing admin requests that came from another site.

    HTTP Basic credentials are attached by the browser on cross-site requests
    regardless of SameSite, so a page the admin visits while authenticated could
    otherwise POST here — deleting a law and its votes. Comparing the request's
    own host to Origin (falling back to Referer) closes that without a session
    rewrite. Requests with neither header are not browser form posts (curl, the
    test client) and are left alone.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    stated = request.headers.get("origin") or request.headers.get("referer")
    if not stated:
        return
    # Compare parsed origins, never string prefixes: "http://site.evil.example"
    # starts with "http://site" and would otherwise sail through.
    if _origin(stated) != _origin(str(request.base_url)):
        raise HTTPException(
            status_code=403,
            detail="Requête refusée : elle ne provient pas de ce site.",
        )


# Every admin route inherits the check; it is a no-op on reads.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_same_origin)])

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


def _dt_local(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%dT%H:%M") if value else ""


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
    typed = {k: v for k, v in (typed or {}).items() if v}
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
    # A draft already generated for this law is shown straight away: it has been
    # paid for, and hiding it behind another click helps nobody.
    draft, arguments, notes, eli_url = _cached_draft(prefill)
    return templates.TemplateResponse(
        request,
        "admin/law_form.html",
        {
            "law": None,
            "prefill": prefill,
            "values": _form_values(
                prefill=prefill, draft=draft, arguments=arguments, eli_url=eli_url
            ),
            "drafted_at": prefill.drafted_at if prefill else None,
            "argument_notes": notes,
            "reserves": arguments.reserves if arguments else [],
            "has_draft": bool(prefill and prefill.draft),
            "ai_enabled": bool(get_settings().anthropic_api_key),
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
        request,
        "admin/law_form.html",
        {
            "law": law,
            "prefill": None,
            "values": _form_values(law=law),
            "drafted_at": None,
            "argument_notes": [],
            "reserves": [],
            "has_draft": False,
            "ai_enabled": False,
        },
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


@router.post("/imports/{item_id}/brouillon", response_class=HTMLResponse)
def draft_import(
    request: Request,
    item_id: int,
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
):
    """Draft the presentation from the official text and the sitting records.

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
    }

    def render(draft, arguments, notes, eli_url):
        return templates.TemplateResponse(
            request,
            "admin/law_form.html",
            {
                "law": None,
                "prefill": item,
                "values": _form_values(
                    prefill=item, draft=draft, arguments=arguments,
                    typed=typed, eli_url=eli_url,
                ),
                "drafted_at": item.drafted_at,
                "argument_notes": notes,
                "reserves": arguments.reserves if arguments else [],
                "has_draft": bool(item.draft),
                "ai_enabled": bool(get_settings().anthropic_api_key),
            },
        )

    def failed(message: str):
        # Keep the admin on the form with their work intact, rather than bouncing
        # them back to the list and losing it.
        return render(None, None, [message], "")

    if item.draft and not regenerer:
        return render(*_cached_draft(item))

    if not (item.nor and item.jo_date):
        return failed(f"LOI n° {item.law_code} : référence NOR ou date JO manquante.")
    try:
        law_text = fetch_law_text(item.nor, item.jo_date)
        draft = generate_draft(law_text)
    except (LegifranceFetchError, AiDraftError) as exc:
        return failed(f"LOI n° {item.law_code} : {exc}")

    # Arguments are best-effort: the factual blocks are worth having on their own,
    # so a sitting record that cannot be read must not lose them.
    arguments, notes = None, []
    if not item.seances:
        notes.append(
            "Aucune séance publique enregistrée pour ce texte : les arguments sont "
            "à rédiger et à sourcer à la main."
        )
    else:
        try:
            arguments = select_arguments(law_text, fetch_speeches(item.seances))
            notes = list(arguments.notes)
        except (DebatesFetchError, AiDraftError) as exc:
            notes.append(f"Arguments non proposés : {exc}")

    imports_crud.save_draft(db, item, draft={
        "summary_text": draft.summary_text,
        "practical_changes": draft.practical_changes,
        "arguments_for": arguments.pour if arguments else [],
        "arguments_against": arguments.contre if arguments else [],
        "reserves": arguments.reserves if arguments else [],
        "notes": notes,
        "eli_url": law_text.eli_url or "",
    })
    return render(draft, arguments, notes, law_text.eli_url or "")
