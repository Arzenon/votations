"""Context for ``admin/law_form.html`` (creation, editing and drafting)."""

from __future__ import annotations

from app.ai import LawDraft, SelectedArguments
from app.config import get_settings
from app.labels import french_date
from app.models import (
    DRAFT_FAILED,
    DRAFT_IDLE,
    DRAFT_PENDING,
    DRAFT_READY,
    ImportedLaw,
    Law,
    LawRevision,
)
from app.routes.admin.forms import day_value, dt_local


def official_ref(item: ImportedLaw) -> str:
    """The canonical citation, e.g. 'LOI n° 2026-532 du 24 juin 2026'."""
    return f"LOI n° {item.law_code} du {french_date(item.promulgated_on)}"


def form_values(
    *,
    law: Law | None = None,
    prefill: ImportedLaw | None = None,
    draft: LawDraft | None = None,
    arguments: SelectedArguments | None = None,
    typed: dict | None = None,
    eli_url: str = "",
) -> dict:
    """Values for each field of the law form.

    Priority: what the admin typed, then the AI draft, then the staged import.
    Regenerating a draft never discards edits in progress.
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
            "debate_summary": law.debate_summary,
            "promulgated_on": day_value(law.promulgated_on),
            "opens_at": dt_local(law.opens_at),
            "closes_at": dt_local(law.closes_at),
            "published": law.published,
        }
        # Showing the draft on an existing law: only the text blocks change.
        if draft is not None:
            base["summary_text"] = draft.summary_text
            base["practical_changes"] = draft.practical_changes
        if arguments is not None:
            base["arguments_for"] = arguments.pour
            base["arguments_against"] = arguments.contre
            base["debate_summary"] = arguments.sujet_debat
    else:
        base = {
            "title": prefill.title if prefill else "",
            "official_ref": official_ref(prefill) if prefill else "",
            # The JO's ELI link is more durable than the AN's.
            "legifrance_url": eli_url or (prefill.legifrance_url if prefill else "") or "",
            "summary_text": draft.summary_text if draft else "",
            "practical_changes": draft.practical_changes if draft else "",
            "arguments_for": arguments.pour if arguments else [],
            "arguments_against": arguments.contre if arguments else [],
            "debate_summary": arguments.sujet_debat if arguments else "",
            "promulgated_on": day_value(prefill.promulgated_on) if prefill else "",
            "opens_at": "",
            "closes_at": "",
            "published": False,
        }
    base.update(typed)
    return base


def draft_state_context(item: ImportedLaw | None) -> dict:
    """Context for ``admin/_draft_state.html``.

    ``item`` is None on the creation form before a dossier is chosen.
    """
    status = item.draft_status if item is not None else DRAFT_IDLE
    return {
        "draft_item": item,
        "draft_status": status,
        "draft_error": item.draft_error if item is not None else "",
        "draft_pending": status == DRAFT_PENDING,
        "draft_failed": status == DRAFT_FAILED,
        "draft_ready": status == DRAFT_READY,
    }


def cached_draft(
    item: ImportedLaw | None,
) -> tuple[LawDraft | None, SelectedArguments | None, list[str], str]:
    """Rehydrate a stored draft. Returns (draft, arguments, notes, eli_url)."""
    cached = item.draft if item is not None else None
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
            sujet_debat=cached.get("debate_summary") or "",
        ),
        list(cached.get("notes") or []),
        cached.get("eli_url") or "",
    )


def law_form_context(
    *,
    law: Law | None = None,
    prefill: ImportedLaw | None = None,
    draft: LawDraft | None = None,
    arguments: SelectedArguments | None = None,
    notes: list[str] | None = None,
    eli_url: str = "",
    typed: dict | None = None,
    revisions: list[LawRevision] | None = None,
    revision_total: int = 0,
) -> dict:
    """Everything ``admin/law_form.html`` needs, from one place."""
    return {
        "law": law,
        "prefill": prefill,
        "values": form_values(
            law=law,
            prefill=prefill,
            draft=draft,
            arguments=arguments,
            typed=typed,
            eli_url=eli_url,
        ),
        "drafted_at": prefill.drafted_at if prefill else None,
        "argument_notes": list(notes or []),
        "has_draft": bool(prefill and prefill.draft),
        # The fields show the draft, not the saved values.
        "draft_loaded": law is not None and draft is not None,
        **draft_state_context(prefill),
        "ai_enabled": bool(prefill) and bool(get_settings().anthropic_api_key),
        "revisions": list(revisions or []),
        # The form only shows the latest ones.
        "revision_total": revision_total,
    }
