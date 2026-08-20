"""Reading, validating and re-rendering the law form.

The three routes that accept this form — create, update, and the draft trigger —
used to declare the same ten ``Form(...)`` parameters each and pass them one by
one into a builder, sixty-odd lines that had to stay identical. They share
:class:`LawForm` now, so a new field is one line in one place.

Validation is the other half. The date fields already refused an unreadable value
with a 400 page; everything else accepted anything and failed later or silently.
A malformed argument list quietly wiped both argument blocks, a rejected URL was
blanked with no message, and an over-long title reached the database as a 500.
All of them raise :class:`InvalidFormValue` here instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, Form

from app.config import get_settings
from app.security import is_safe_url, safe_url

# Column widths from `app.models.law`. Checked here so an over-long paste is a
# message about the field rather than a database error and a 500 page.
MAX_LENGTHS = {
    "title": 500,
    "official_ref": 255,
    "legifrance_url": 1000,
}


class InvalidFormValue(ValueError):
    """A field the administrator typed cannot be read; rendered as a 400 page."""


def _display_tz() -> ZoneInfo:
    """The zone administrators write opening and closing times in.

    No fallback: ``Settings`` validates the zone at boot, so an unknown one can
    no longer reach this and silently shift every scheduled time to UTC.
    """
    return ZoneInfo(get_settings().display_timezone)


def parse_dt(value: str | None) -> datetime | None:
    """Read a `datetime-local` field as civil time in the display zone.

    The column is timestamptz. Storing the naive value straight from the form made
    Postgres read it as UTC, so a vote scheduled to close at 20 h in Paris closed
    at 22 h. Attaching the zone here — and stripping it again in `dt_local` — is
    what makes the form mean what it says.
    """
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidFormValue(f"date ou heure illisible : « {value} »") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_display_tz())


def dt_local(value: datetime | None) -> str:
    """Render a stored instant back into the form's local-time field."""
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(_display_tz()).strftime("%Y-%m-%dT%H:%M")


def parse_arguments(raw: str | None, *, field: str) -> list[dict]:
    """Read a JSON list of {text, source, author}, or refuse it.

    Silently returning ``[]`` on malformed JSON meant one stray comma while
    editing wiped both argument blocks with no message — on a platform whose
    premise is symmetry between the two sides. Sources are checked rather than
    blanked for the same reason: they are rendered as hrefs on the public page,
    and V3 opens this form to every authenticated user.
    """
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidFormValue(
            f"« {field} » : JSON illisible (ligne {exc.lineno}, colonne {exc.colno}) — {exc.msg}"
        ) from exc
    if not isinstance(data, list):
        raise InvalidFormValue(f"« {field} » : une liste JSON est attendue, entre crochets [ ]")

    out = []
    for index, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            raise InvalidFormValue(
                f"« {field} », entrée {index} : un objet {{\"text\": …}} est attendu"
            )
        source = str(entry.get("source", "")).strip()
        if not is_safe_url(source):
            raise InvalidFormValue(
                f"« {field} », entrée {index} : le lien « {source} » n'est pas une "
                "adresse http(s) et ne peut pas être publié"
            )
        out.append(
            {
                "text": str(entry.get("text", "")),
                "source": safe_url(source),
                "author": str(entry.get("author", "")),
            }
        )
    return out


def _checked(value: str, *, field: str, label: str) -> str:
    limit = MAX_LENGTHS[field]
    if len(value) > limit:
        raise InvalidFormValue(
            f"« {label} » fait {len(value)} caractères ; la limite est de {limit}"
        )
    return value


@dataclass
class LawForm:
    """Everything the law form posts, validated once."""

    title: str
    official_ref: str
    legifrance_url: str
    summary_text: str
    practical_changes: str
    arguments_for: list[dict]
    arguments_against: list[dict]
    opens_at: datetime | None
    closes_at: datetime | None
    published: bool
    # Raw strings, kept so the draft route can echo the dates back into the form
    # exactly as they were typed rather than round-tripping them through UTC.
    opens_at_raw: str = ""
    closes_at_raw: str = ""

    def columns(self) -> dict:
        """The validated column values, ready for create/update."""
        return {
            "title": self.title,
            "official_ref": self.official_ref or None,
            "legifrance_url": self.legifrance_url or None,
            "summary_text": self.summary_text,
            "practical_changes": self.practical_changes,
            "arguments_for": self.arguments_for,
            "arguments_against": self.arguments_against,
            "opens_at": self.opens_at,
            "closes_at": self.closes_at,
            "published": self.published,
        }

    def typed(self) -> dict:
        """What the administrator had in the form, for re-rendering it."""
        return {
            "title": self.title,
            "summary_text": self.summary_text,
            "practical_changes": self.practical_changes,
            "arguments_for": self.arguments_for,
            "arguments_against": self.arguments_against,
            "opens_at": self.opens_at_raw,
            "closes_at": self.closes_at_raw,
            "published": self.published,
        }


@dataclass
class RawLawForm:
    """The form exactly as posted. Declared once, read two ways."""

    title: str
    official_ref: str
    legifrance_url: str
    summary_text: str
    practical_changes: str
    arguments_for: str
    arguments_against: str
    opens_at: str
    closes_at: str
    published: str


def raw_law_form(
    title: str = Form(""),
    official_ref: str = Form(""),
    legifrance_url: str = Form(""),
    summary_text: str = Form(""),
    practical_changes: str = Form(""),
    arguments_for: str = Form(""),
    arguments_against: str = Form(""),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    published: str = Form(""),
) -> RawLawForm:
    """The ten fields, declared in one place for every route that takes them."""
    return RawLawForm(
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
    )


def law_form(raw: RawLawForm = Depends(raw_law_form)) -> LawForm:
    """The form, validated for writing. Refuses anything it cannot store."""
    title = _checked(raw.title.strip(), field="title", label="Titre")
    if not title:
        raise InvalidFormValue("« Titre » ne peut pas être vide")

    official_ref = _checked(
        raw.official_ref.strip(), field="official_ref", label="Référence officielle"
    )
    legifrance_url = _checked(
        raw.legifrance_url.strip(), field="legifrance_url", label="URL Légifrance"
    )
    if not is_safe_url(legifrance_url):
        raise InvalidFormValue(
            f"« URL Légifrance » : « {legifrance_url} » n'est pas une adresse http(s)"
        )

    opens = parse_dt(raw.opens_at)
    closes = parse_dt(raw.closes_at)
    if opens and closes and closes <= opens:
        raise InvalidFormValue(
            "la clôture du vote doit être postérieure à son ouverture — telle "
            "qu'elle est saisie, la votation ne serait jamais ouverte"
        )

    return LawForm(
        title=title,
        official_ref=official_ref,
        legifrance_url=safe_url(legifrance_url),
        summary_text=raw.summary_text,
        practical_changes=raw.practical_changes,
        arguments_for=parse_arguments(raw.arguments_for, field="Arguments des partisans"),
        arguments_against=parse_arguments(raw.arguments_against, field="Arguments des opposants"),
        opens_at=opens,
        closes_at=closes,
        published=bool(raw.published),
        opens_at_raw=raw.opens_at,
        closes_at_raw=raw.closes_at,
    )


def draft_form(raw: RawLawForm = Depends(raw_law_form)) -> LawForm:
    """The form as read by the draft trigger, which only ever echoes it back.

    Deliberately lenient. Asking for a draft is the *first* thing done on a freshly
    imported law, when the form is usually empty and often half-typed — refusing
    the request because the title is blank or an argument list is mid-edit would
    block the one action that is meant to help fill the form in. Nothing here
    reaches the database: these values are re-rendered into the form and nothing
    else, and the strict pass runs when the administrator actually saves.
    """

    def lenient(value: str, field: str) -> list[dict]:
        try:
            return parse_arguments(value, field=field)
        except InvalidFormValue:
            return []

    return LawForm(
        title=raw.title.strip()[: MAX_LENGTHS["title"]],
        official_ref=raw.official_ref.strip()[: MAX_LENGTHS["official_ref"]],
        legifrance_url=safe_url(raw.legifrance_url.strip()),
        summary_text=raw.summary_text,
        practical_changes=raw.practical_changes,
        arguments_for=lenient(raw.arguments_for, "Arguments des partisans"),
        arguments_against=lenient(raw.arguments_against, "Arguments des opposants"),
        opens_at=None,
        closes_at=None,
        published=bool(raw.published),
        opens_at_raw=raw.opens_at,
        closes_at_raw=raw.closes_at,
    )
