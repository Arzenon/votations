"""Assembling the context for ``admin/law_form.html``.

The template serves creation, editing and drafting; building its context in one
place is what keeps those three call sites from drifting apart.
"""

from __future__ import annotations

from app.ai import LawDraft, SelectedArguments
from app.config import get_settings
from app.labels import french_date
from app.routes.admin.forms import dt_local


def official_ref(item) -> str:
    """The canonical citation, e.g. 'LOI n° 2026-532 du 24 juin 2026'."""
    return f"LOI n° {item.law_code} du {french_date(item.promulgated_on)}"


def form_values(
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
            "opens_at": dt_local(law.opens_at),
            "closes_at": dt_local(law.closes_at),
            "published": law.published,
        }
    else:
        base = {
            "title": prefill.title if prefill else "",
            "official_ref": official_ref(prefill) if prefill else "",
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


def cached_draft(item):
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


def law_form_context(
    *,
    law=None,
    prefill=None,
    draft: LawDraft | None = None,
    arguments: SelectedArguments | None = None,
    notes: list[str] | None = None,
    eli_url: str = "",
    typed: dict | None = None,
    revisions: list | None = None,
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
        "reserves": arguments.reserves if arguments else [],
        "has_draft": bool(prefill and prefill.draft),
        "draft_status": prefill.draft_status if prefill else "",
        "draft_error": prefill.draft_error if prefill else "",
        "ai_enabled": bool(prefill) and bool(get_settings().anthropic_api_key),
        "revisions": list(revisions or []),
        # How many exist in all, against how many are shown: the form displays
        # only the most recent page, and saying so beats a table that silently
        # stops.
        "revision_total": revision_total,
    }
