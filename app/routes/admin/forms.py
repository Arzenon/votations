"""Reading and validating the law form.

Shared by the create, update and draft routes through `LawForm`. Invalid
values raise `InvalidFormValue`, shown as a 400 page naming the field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from fastapi import Depends, Form
from sqlalchemy import String

from app.config import display_zone
from app.db import as_utc
from app.models import Law
from app.security import is_safe_url, safe_url

# Limits for the Text columns, which have none in the database. Drafts are
# under 5,000 characters. The whole form must fit in Caddy's 256 kB body limit,
# otherwise the admin gets a bare 413.
_PROSE_LIMITS = {
    "summary_text": 20_000,
    "practical_changes": 20_000,
}

# An argument is a sentence or two from a speech.
MAX_ARGUMENT_CHARS = 5_000

# The model proposes five per side. With this cap the whole form stays under
# 256 kB (40,000 + 100,000 characters).
MAX_ARGUMENTS_PER_SIDE = 10


def _column_limits() -> dict[str, int]:
    """Max length of the String columns the form writes, read from the model."""
    limits = {}
    for name in ("title", "official_ref", "legifrance_url", "debate_summary"):
        column_type = Law.__table__.c[name].type
        if not isinstance(column_type, String) or column_type.length is None:
            # Fail loudly rather than silently drop the check.
            raise TypeError(
                f"laws.{name} n'est plus une colonne String de largeur déclarée ; "
                "adaptez la validation du formulaire"
            )
        limits[name] = column_type.length
    return limits


MAX_LENGTHS = {**_column_limits(), **_PROSE_LIMITS}


class InvalidFormValue(ValueError):
    """An invalid form value; shown as a 400 page."""


def parse_dt(value: str | None) -> datetime | None:
    """Parse a `datetime-local` field as local time in the display zone.

    Without the zone, Postgres would read it as UTC (20:00 Paris would close
    at 22:00).
    """
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidFormValue(f"date ou heure illisible : « {value} »") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=display_zone())


def parse_day(value: str | None) -> date | None:
    """Parse a `date` field (a day, no time or zone)."""
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise InvalidFormValue(f"date illisible : « {value} »") from exc


def day_value(value: date | None) -> str:
    """Format a date for the form's `date` field."""
    return value.isoformat() if value else ""


def dt_local(value: datetime | None) -> str:
    """Format a datetime for the form's local-time field."""
    if not value:
        return ""
    return as_utc(value).astimezone(display_zone()).strftime("%Y-%m-%dT%H:%M")


@dataclass
class ArgumentRows:
    """One side's arguments as posted: three parallel lists.

    Repeated fields arrive in document order, so no index is needed in the
    field names and rows can be added or removed freely.
    """

    text: list[str] = field(default_factory=list)
    author: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return max(len(self.text), len(self.author), len(self.source))

    def row(self, index: int) -> tuple[str, str, str]:
        def at(values: list[str]) -> str:
            return values[index] if index < len(values) else ""

        return at(self.text), at(self.author), at(self.source)


def _read_arguments(rows: ArgumentRows, *, field: str) -> list[dict]:
    """Read one side's arguments.

    Empty rows are skipped. A row with an author or source but no text is
    refused rather than silently dropped. Sources must be http(s). The count
    is checked by `parse_arguments`.
    """
    out: list[dict] = []
    for index in range(len(rows)):
        text, author, source = rows.row(index)
        text, author, source = text.strip(), author.strip(), source.strip()
        if not (text or author or source):
            continue  # empty row (the page always adds a spare one)
        position = len(out) + 1
        if not text:
            raise InvalidFormValue(
                f"« {field} », argument {position} : la citation est vide alors que "
                "l'auteur ou la source sont renseignés. Complétez-la, ou videz "
                "entièrement l'argument pour le supprimer."
            )
        if not is_safe_url(source):
            raise InvalidFormValue(
                f"« {field} », argument {position} : le lien « {source} » n'est pas "
                "une adresse http(s) et ne peut pas être publié"
            )
        if len(text) > MAX_ARGUMENT_CHARS:
            raise InvalidFormValue(
                f"« {field} », argument {position} : {len(text)} caractères ; la "
                f"limite est de {MAX_ARGUMENT_CHARS}. Un argument est une citation, "
                "pas un compte rendu."
            )
        out.append({"text": text, "source": safe_url(source), "author": author})
    return out


def parse_arguments(rows: ArgumentRows, *, field: str) -> list[dict]:
    """Read one side's arguments for saving, refusing invalid ones."""
    out = _read_arguments(rows, field=field)
    if len(out) > MAX_ARGUMENTS_PER_SIDE:
        raise InvalidFormValue(
            f"« {field} » : {len(out)} arguments ; la limite est de "
            f"{MAX_ARGUMENTS_PER_SIDE} par camp. Les arguments sont une sélection "
            "de citations, pas le compte rendu du débat : la page en affiche un et "
            "replie les autres."
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
    debate_summary: str
    promulgated_on: date | None
    opens_at: datetime | None
    closes_at: datetime | None
    published: bool
    # Raw values, to show the dates back exactly as typed.
    opens_at_raw: str = ""
    closes_at_raw: str = ""

    def columns(self) -> dict:
        """Validated column values for create/update."""
        return {
            "title": self.title,
            "official_ref": self.official_ref or None,
            "legifrance_url": self.legifrance_url or None,
            "summary_text": self.summary_text,
            "practical_changes": self.practical_changes,
            "arguments_for": self.arguments_for,
            "arguments_against": self.arguments_against,
            "debate_summary": self.debate_summary,
            "promulgated_on": self.promulgated_on,
            "opens_at": self.opens_at,
            "closes_at": self.closes_at,
            "published": self.published,
        }

    def typed(self) -> dict:
        """The form's current values, for re-rendering it."""
        return {
            "title": self.title,
            "summary_text": self.summary_text,
            "practical_changes": self.practical_changes,
            "arguments_for": self.arguments_for,
            "arguments_against": self.arguments_against,
            "debate_summary": self.debate_summary,
            "promulgated_on": day_value(self.promulgated_on),
            "opens_at": self.opens_at_raw,
            "closes_at": self.closes_at_raw,
            "published": self.published,
        }


# Checkbox values meaning "off". Browsers just omit unchecked boxes, but
# `published=false` from curl mustn't publish the law.
_FALSY_CHECKBOX = frozenset({"", "0", "false", "off", "no", "non"})


def _checkbox(value: str) -> bool:
    return value.strip().lower() not in _FALSY_CHECKBOX


@dataclass
class RawLawForm:
    """The form as posted, before validation."""

    title: str
    official_ref: str
    legifrance_url: str
    summary_text: str
    practical_changes: str
    arguments_for: ArgumentRows
    arguments_against: ArgumentRows
    debate_summary: str
    promulgated_on: str
    opens_at: str
    closes_at: str
    published: str


def raw_law_form(
    title: str = Form(""),
    official_ref: str = Form(""),
    legifrance_url: str = Form(""),
    summary_text: str = Form(""),
    practical_changes: str = Form(""),
    # One value per argument row, in document order.
    arguments_for_text: list[str] = Form([]),
    arguments_for_author: list[str] = Form([]),
    arguments_for_source: list[str] = Form([]),
    arguments_against_text: list[str] = Form([]),
    arguments_against_author: list[str] = Form([]),
    arguments_against_source: list[str] = Form([]),
    debate_summary: str = Form(""),
    promulgated_on: str = Form(""),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    published: str = Form(""),
) -> RawLawForm:
    """FastAPI dependency declaring all the form fields."""
    return RawLawForm(
        title=title,
        official_ref=official_ref,
        legifrance_url=legifrance_url,
        summary_text=summary_text,
        practical_changes=practical_changes,
        arguments_for=ArgumentRows(
            text=arguments_for_text,
            author=arguments_for_author,
            source=arguments_for_source,
        ),
        arguments_against=ArgumentRows(
            text=arguments_against_text,
            author=arguments_against_author,
            source=arguments_against_source,
        ),
        debate_summary=debate_summary,
        promulgated_on=promulgated_on,
        opens_at=opens_at,
        closes_at=closes_at,
        published=published,
    )


def law_form(raw: RawLawForm = Depends(raw_law_form)) -> LawForm:
    """The form, validated for saving."""
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

    promulgated_on = parse_day(raw.promulgated_on)
    opens = parse_dt(raw.opens_at)
    closes = parse_dt(raw.closes_at)
    if opens and closes and closes <= opens:
        raise InvalidFormValue(
            "la clôture du vote doit être postérieure à son ouverture - telle "
            "qu'elle est saisie, la votation ne serait jamais ouverte"
        )

    return LawForm(
        title=title,
        official_ref=official_ref,
        legifrance_url=safe_url(legifrance_url),
        summary_text=_checked(raw.summary_text, field="summary_text", label="Ce que dit la loi"),
        practical_changes=_checked(
            raw.practical_changes,
            field="practical_changes",
            label="Ce que ça change en pratique",
        ),
        arguments_for=parse_arguments(raw.arguments_for, field="Arguments des partisans"),
        arguments_against=parse_arguments(raw.arguments_against, field="Arguments des opposants"),
        debate_summary=_checked(
            raw.debate_summary.strip(), field="debate_summary", label="Ce qui a été débattu"
        ),
        promulgated_on=promulgated_on,
        opens_at=opens,
        closes_at=closes,
        published=_checkbox(raw.published),
        opens_at_raw=raw.opens_at,
        closes_at_raw=raw.closes_at,
    )


def _lenient_arguments(rows: ArgumentRows, field: str) -> list[dict]:
    """Like `_read_arguments` but never refuses: invalid gives [], too many is truncated."""
    try:
        return _read_arguments(rows, field=field)[:MAX_ARGUMENTS_PER_SIDE]
    except InvalidFormValue:
        return []


def _lenient_dt(value: str) -> datetime | None:
    """Like `parse_dt` but returns None instead of raising."""
    try:
        return parse_dt(value)
    except InvalidFormValue:
        return None


def _lenient_day(value: str) -> date | None:
    try:
        return parse_day(value)
    except InvalidFormValue:
        return None


def _echoed(raw: RawLawForm, *, opens=None, closes=None) -> LawForm:
    """Lenient reading for routes that don't save (draft, preview).

    Values are truncated rather than refused, since the form is often
    incomplete at that point.
    """
    return LawForm(
        title=raw.title.strip()[: MAX_LENGTHS["title"]],
        official_ref=raw.official_ref.strip()[: MAX_LENGTHS["official_ref"]],
        legifrance_url=safe_url(raw.legifrance_url.strip()),
        summary_text=raw.summary_text[: MAX_LENGTHS["summary_text"]],
        practical_changes=raw.practical_changes[: MAX_LENGTHS["practical_changes"]],
        arguments_for=_lenient_arguments(raw.arguments_for, "Arguments des partisans"),
        arguments_against=_lenient_arguments(raw.arguments_against, "Arguments des opposants"),
        debate_summary=raw.debate_summary.strip()[: MAX_LENGTHS["debate_summary"]],
        promulgated_on=_lenient_day(raw.promulgated_on),
        opens_at=opens,
        closes_at=closes,
        published=_checkbox(raw.published),
        opens_at_raw=raw.opens_at,
        closes_at_raw=raw.closes_at,
    )


def draft_form(raw: RawLawForm = Depends(raw_law_form)) -> LawForm:
    """The form for the draft route. Dates are shown back from the raw values."""
    return _echoed(raw)


def preview_form(raw: RawLawForm = Depends(raw_law_form)) -> LawForm:
    """The form for the preview. Dates are parsed, since the voting window
    decides what the page shows."""
    return _echoed(raw, opens=_lenient_dt(raw.opens_at), closes=_lenient_dt(raw.closes_at))
