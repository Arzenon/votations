"""Assembling the context for ``admin/law_form.html``.

The template serves creation, editing and drafting; building its context in one
place is what keeps those three call sites from drifting apart.
"""

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
            "promulgated_on": day_value(law.promulgated_on),
            "opens_at": dt_local(law.opens_at),
            "closes_at": dt_local(law.closes_at),
            "published": law.published,
        }
        # Editing a law and explicitly asking to see the draft: the four written
        # blocks come from the draft, everything else stays the law's own. The
        # draft has nothing to say about a title, a voting window or a
        # publication flag, and overwriting those with blanks would turn "show me
        # the draft" into "lose my schedule".
        if draft is not None:
            base["summary_text"] = draft.summary_text
            base["practical_changes"] = draft.practical_changes
        if arguments is not None:
            base["arguments_for"] = arguments.pour
            base["arguments_against"] = arguments.contre
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
            # The staged row already knows it; nobody should retype what the
            # daily import fetched from the Assemblée nationale.
            "promulgated_on": day_value(prefill.promulgated_on) if prefill else "",
            "opens_at": "",
            "closes_at": "",
            "published": False,
        }
    base.update(typed)
    return base


def draft_state_context(item: ImportedLaw | None) -> dict:
    """What ``admin/_draft_state.html`` needs — for both pages that show it.

    The three booleans are decided here rather than by comparing ``draft_status``
    against string literals in the template. The values are ``DRAFT_PENDING`` and
    its siblings; a template that spells « en_cours » out is a copy of a constant
    that no test reads and no rename would follow, and what breaks when they
    drift is the polling banner — the one piece of the page whose entire job is
    to say what happened.

    ``draft_item`` rather than ``item`` because the law form has a ``prefill``
    and no ``item``: naming the partial's own variable lets both callers spread
    this into their context and include the partial with no ``{% with %}``
    re-deriving anything.

    ``None`` is a real argument here — the creation form renders before any
    dossier is chosen — and reads as "nothing is being drafted".
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
        # True when the fields below are showing the draft rather than what is
        # stored, so the page can say so — nothing is written until « Enregistrer ».
        "draft_loaded": law is not None and draft is not None,
        # Everything the status banner needs, built the same way as on the pages
        # that poll for it. See :func:`draft_state_context`.
        **draft_state_context(prefill),
        "ai_enabled": bool(prefill) and bool(get_settings().anthropic_api_key),
        "revisions": list(revisions or []),
        # How many exist in all, against how many are shown: the form displays
        # only the most recent page, and saying so beats a table that silently
        # stops.
        "revision_total": revision_total,
    }
