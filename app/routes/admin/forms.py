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

from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import Depends, Form
from sqlalchemy import String

from app.config import display_zone
from app.models import Law
from app.security import is_safe_url, safe_url

#: The prose blocks are ``Text`` columns, so the database imposes no width on
#: them and nothing here did either. A whole loi de finances pasted into « Ce que
#: dit la loi » — by accident, or by one of the authenticated users V3 opens this
#: form to — landed as a multi-megabyte row that the home page then read in full
#: to render a 200-character excerpt. These are generous: the charter asks for
#: three to five short paragraphs, and the drafts come in under 5 000 characters.
#:
#: Sized so the whole form stays inside the 256 kB request body Caddy accepts —
#: two prose blocks plus two full argument lists — because a limit the edge
#: refuses first is one the administrator meets as a bare 413 with no field named.
_PROSE_LIMITS = {
    "summary_text": 20_000,
    "practical_changes": 20_000,
}

#: The same for one argument's quoted text. The longest real speech in a sitting
#: record runs to a few thousand characters, and an argument is a sentence or two
#: out of one.
MAX_ARGUMENT_CHARS = 5_000


def _column_limits() -> dict[str, int]:
    """The declared width of each ``String`` column the form writes.

    Read off the model rather than copied from it. The copy was correct and had
    no way to stay that way: widening a column left the form still refusing at
    the old limit, and narrowing one left it accepting values the database would
    reject with a 500.
    """
    limits = {}
    for name in ("title", "official_ref", "legifrance_url"):
        column_type = Law.__table__.c[name].type
        if not isinstance(column_type, String) or column_type.length is None:
            # Turning one of these into an unbounded `Text` is a legitimate
            # decision, but it has to be a decision: silently reading no limit
            # here would drop the check on that field and nobody would notice.
            raise TypeError(
                f"laws.{name} n'est plus une colonne String de largeur déclarée ; "
                "adaptez la validation du formulaire"
            )
        limits[name] = column_type.length
    return limits


# Checked here so an over-long paste is a message about the field rather than a
# database error and a 500 page.
MAX_LENGTHS = {**_column_limits(), **_PROSE_LIMITS}


class InvalidFormValue(ValueError):
    """A field the administrator typed cannot be read; rendered as a 400 page."""


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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=display_zone())


def dt_local(value: datetime | None) -> str:
    """Render a stored instant back into the form's local-time field."""
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(display_zone()).strftime("%Y-%m-%dT%H:%M")


@dataclass
class ArgumentRows:
    """One side's arguments as the form posts them: three parallel lists.

    The browser submits repeated fields in document order, so the *n*th text goes
    with the *n*th author and the *n*th source. No index is carried in the field
    names, which is what lets the page add and remove rows without renumbering
    anything — a row is added by cloning markup and removed by deleting it.
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


def parse_arguments(rows: ArgumentRows, *, field: str) -> list[dict]:
    """Read one side's arguments from the form's rows, or refuse them.

    This used to read a JSON array typed into a textarea, which asked an
    administrator to be a programmer: one missing bracket and the whole block was
    unreadable, and the error message talked about lines and columns. The fields
    are now ordinary inputs, so most of what could go wrong cannot any more.

    What is left still refuses rather than drops. An empty row is how the page
    says "nothing here" and is skipped in silence; a row with an author or a
    source but *no text* is a half-filled entry, and quietly discarding it would
    lose an argument on a platform whose premise is symmetry between the two
    sides. Sources are checked because they are rendered as hrefs on the public
    page, and V3 opens this form to every authenticated user.
    """
    out: list[dict] = []
    for index in range(len(rows)):
        text, author, source = rows.row(index)
        text, author, source = text.strip(), author.strip(), source.strip()
        if not (text or author or source):
            continue  # an untouched row — the page always offers a spare
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


#: Values a checkbox field may carry that mean "off". A browser omits an
#: unchecked box entirely and sends "on" for a checked one, so `bool(value)` was
#: right for every request a browser makes — and wrong for the ones this form
#: also has to survive: a replayed POST, a curl, and the V3 API-shaped callers the
#: roadmap invites. `published=false` publishing a law is not a failure mode worth
#: keeping for the sake of one character.
_FALSY_CHECKBOX = frozenset({"", "0", "false", "off", "no", "non"})


def _checkbox(value: str) -> bool:
    return value.strip().lower() not in _FALSY_CHECKBOX


@dataclass
class RawLawForm:
    """The form exactly as posted. Declared once, read two ways."""

    title: str
    official_ref: str
    legifrance_url: str
    summary_text: str
    practical_changes: str
    arguments_for: ArgumentRows
    arguments_against: ArgumentRows
    opens_at: str
    closes_at: str
    published: str


def raw_law_form(
    title: str = Form(""),
    official_ref: str = Form(""),
    legifrance_url: str = Form(""),
    summary_text: str = Form(""),
    practical_changes: str = Form(""),
    # Repeated once per argument row, in document order. FastAPI collects every
    # value posted under the same name into the list, which is what keeps the
    # three sides of one argument together without an index in the field name.
    arguments_for_text: list[str] = Form([]),
    arguments_for_author: list[str] = Form([]),
    arguments_for_source: list[str] = Form([]),
    arguments_against_text: list[str] = Form([]),
    arguments_against_author: list[str] = Form([]),
    arguments_against_source: list[str] = Form([]),
    opens_at: str = Form(""),
    closes_at: str = Form(""),
    published: str = Form(""),
) -> RawLawForm:
    """The whole form, declared in one place for every route that takes it."""
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
        summary_text=_checked(raw.summary_text, field="summary_text", label="Ce que dit la loi"),
        practical_changes=_checked(
            raw.practical_changes,
            field="practical_changes",
            label="Ce que ça change en pratique",
        ),
        arguments_for=parse_arguments(raw.arguments_for, field="Arguments des partisans"),
        arguments_against=parse_arguments(raw.arguments_against, field="Arguments des opposants"),
        opens_at=opens,
        closes_at=closes,
        published=_checkbox(raw.published),
        opens_at_raw=raw.opens_at,
        closes_at_raw=raw.closes_at,
    )


def _lenient_arguments(rows: ArgumentRows, field: str) -> list[dict]:
    """A half-filled argument reads as no argument rather than as a refusal."""
    try:
        return parse_arguments(rows, field=field)
    except InvalidFormValue:
        return []


def _lenient_dt(value: str) -> datetime | None:
    """A half-typed date reads as no date rather than as a refusal."""
    try:
        return parse_dt(value)
    except InvalidFormValue:
        return None


def _echoed(raw: RawLawForm, *, opens=None, closes=None) -> LawForm:
    """The form read the forgiving way, for the routes that only ever show it back.

    Everything is truncated rather than refused and nothing reaches the database.
    Shared by the two routes that never write: asking for an AI draft, and asking
    for a preview. Both happen *while* the form is being filled in, which is
    exactly when it is at its most malformed — refusing either because a title is
    blank or a bracket is missing would block the help at the moment it is wanted.
    """
    return LawForm(
        title=raw.title.strip()[: MAX_LENGTHS["title"]],
        official_ref=raw.official_ref.strip()[: MAX_LENGTHS["official_ref"]],
        legifrance_url=safe_url(raw.legifrance_url.strip()),
        summary_text=raw.summary_text[: MAX_LENGTHS["summary_text"]],
        practical_changes=raw.practical_changes[: MAX_LENGTHS["practical_changes"]],
        arguments_for=_lenient_arguments(raw.arguments_for, "Arguments des partisans"),
        arguments_against=_lenient_arguments(raw.arguments_against, "Arguments des opposants"),
        opens_at=opens,
        closes_at=closes,
        published=_checkbox(raw.published),
        opens_at_raw=raw.opens_at,
        closes_at_raw=raw.closes_at,
    )


def draft_form(raw: RawLawForm = Depends(raw_law_form)) -> LawForm:
    """The form as read by the draft trigger, which only ever echoes it back.

    The dates are deliberately dropped: this route re-renders them from
    ``opens_at_raw`` exactly as they were typed, rather than round-tripping them
    through UTC and back.
    """
    return _echoed(raw)


def preview_form(raw: RawLawForm = Depends(raw_law_form)) -> LawForm:
    """The form as read by the preview, which renders it and saves nothing.

    Unlike the draft trigger, the dates *are* parsed: the voting window is what
    decides whether the previewed page shows a ballot, a countdown or a result,
    and a preview that always showed one of the three would be answering a
    different question from the one being asked. A date too mangled to read
    simply goes missing, which is what the page would do anyway.
    """
    return _echoed(raw, opens=_lenient_dt(raw.opens_at), closes=_lenient_dt(raw.closes_at))
