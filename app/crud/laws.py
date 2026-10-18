"""CRUD and queries for laws."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime

from sqlalchemy import (
    ColumnClause,
    Select,
    and_,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer, with_expression

from app.crud import Page, count_matching, paginate
from app.crud import results as results_crud
from app.db import as_utc, utcnow
from app.labels import french_date, french_datetime
from app.models import (
    SEARCH_VECTOR_QUERY_SQL,
    STATUS_CLOSED,
    STATUS_IMPORTED,
    STATUS_OPEN,
    STATUS_SCHEDULED,
    ImportedLaw,
    Law,
    LawRevision,
)

# Fields whose changes are recorded once a law is published.
TRACKED_FIELDS = (
    "title",
    "official_ref",
    "legifrance_url",
    "summary_text",
    "practical_changes",
    "arguments_for",
    "arguments_against",
    "debate_summary",
    "promulgated_on",
    "opens_at",
    "closes_at",
    "published",
)


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)[:255] or "loi"


# Room left in the 255-char column for a "-n" suffix.
_SLUG_SUFFIX_ROOM = 8


def unique_slug(db: Session, title: str) -> str:
    """The first free slug derived from ``title``, found in a single query."""
    base = slugify(title)[: 255 - _SLUG_SUFFIX_ROOM]
    taken = set(
        db.scalars(
            select(Law.slug).where(
                or_(Law.slug == base, Law.slug.like(f"{_escape_like(base)}-%", escape="\\"))
            )
        )
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# Heavy columns that listings never display. Deferred rather than excluded, so
# accessing one still works (at the cost of a query).
_LIST_DEFERRED = (
    defer(Law.practical_changes),
    defer(Law.arguments_for),
    defer(Law.arguments_against),
)

# Listings don't show full summaries either; the home page uses `_EXCERPT`.
_TITLES_ONLY_DEFERRED = (*_LIST_DEFERRED, defer(Law.summary_text))

EXCERPT_CHARS = 200

# One extra character so the template knows whether to add an ellipsis.
_EXCERPT = with_expression(Law.excerpt, func.left(Law.summary_text, EXCERPT_CHARS + 1))


def get_by_slug(db: Session, slug: str) -> Law | None:
    return db.scalar(select(Law).where(Law.slug == slug))


def get_by_id(db: Session, law_id: int) -> Law | None:
    return db.get(Law, law_id)


def _published(stmt: Select) -> Select:
    """Restrict to published, non-archived laws.

    Keep the bare column rather than ``.is_(True)``: Postgres can't match the
    latter against the partial index ``ix_laws_home`` and falls back to a full
    scan (0.3 ms vs 26.8 ms at 50,000 laws).
    """
    return stmt.where(Law.published, Law.archived_at.is_(None))


# Public listing order: most recently promulgated first. created_at only reflects
# when the law was entered. NULLS LAST because Postgres puts nulls first in a
# descending sort, and hand-written laws have no promulgation date.
_NEWEST_LAW_FIRST = (Law.promulgated_on.desc().nullslast(), Law.created_at.desc())


def list_published(
    db: Session, *, status: str | None = None, limit: int = 50, now: datetime | None = None
) -> list[Law]:
    """Published laws, newest first, with an excerpt instead of the full summary.

    ``status`` filters on a derived state in SQL (see `_apply_status`).
    """
    stmt = (
        _apply_status(_published(select(Law)), status, now=now)
        .options(*_TITLES_ONLY_DEFERRED, _EXCERPT)
        .order_by(*_NEWEST_LAW_FIRST)
        .limit(limit)
    )
    return list(db.scalars(stmt))


# Home page sections, in display order.
HOME_BANDS = (STATUS_OPEN, STATUS_SCHEDULED, STATUS_CLOSED)


def published_by_state(db: Session, *, limit: int = 50) -> list[tuple[str, list[Law]]]:
    """Home page listing grouped by state. Empty groups are dropped.

    One query per group so that ``limit`` applies per group: with a global
    limit, closed laws could push open ones off the page. All queries share the
    same ``now`` so a law closing in between can't show up twice.
    """
    now = utcnow()
    bands = [
        (state, list_published(db, status=state, limit=limit, now=now)) for state in HOME_BANDS
    ]
    return [(state, laws) for state, laws in bands if laws]


def public_urls(db: Session, *, limit: int = 5000) -> list[tuple[str, datetime]]:
    """(slug, last modified) for every public law, for the sitemap."""
    rows = db.execute(
        _published(select(Law.slug, Law.updated_at, Law.created_at))
        .order_by(Law.created_at.desc())
        .limit(limit)
    ).all()
    return [(slug, updated or created) for slug, updated, created in rows]


def search(
    db: Session,
    *,
    query: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> Page[Law]:
    """Search title, reference and summary, with a status filter and pagination.

    Filtering and paging happen in SQL. Same order as the home page.
    """
    stmt = _published(select(Law))
    if query:
        stmt = _apply_search(stmt, query)
    stmt = _apply_status(stmt, status)

    total = count_matching(db, stmt)
    number, pages, offset = paginate(page, total, page_size)
    rows = list(
        db.scalars(
            stmt.options(*_TITLES_ONLY_DEFERRED)
            .order_by(*_NEWEST_LAW_FIRST)
            .offset(offset)
            .limit(page_size)
        )
    )
    return Page(rows=rows, total=total, number=number, count=pages)


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Characters that are syntax to `to_tsquery`.
_TSQUERY_NOISE = re.compile(r"[^\w\s-]", flags=re.UNICODE)


def _tsquery(query: str) -> str:
    """Build a French tsquery: all words required, the last one as a prefix.

    ``réforme des retraites`` gives ``réforme & des & retraites:*``.
    Punctuation is removed because `to_tsquery` would fail on it.
    """
    words = _TSQUERY_NOISE.sub(" ", query).split()
    if not words:
        return ""
    *rest, last = words
    return " & ".join([*rest, f"{last}:*"])


# Same expression as the full-text index, defined next to the table so they
# can't drift apart. official_ref isn't included: it's matched as a substring.
_SEARCH_VECTOR: ColumnClause[str] = literal_column(SEARCH_VECTOR_QUERY_SQL)


def _apply_search(stmt: Select, query: str) -> Select:
    """Full-text search on the text, substring match on the official reference."""
    like = f"%{_escape_like(query)}%"
    reference = Law.official_ref.ilike(like, escape="\\")

    terms = _tsquery(query)
    if not terms:
        return stmt.where(reference)
    return stmt.where(
        or_(
            _SEARCH_VECTOR.op("@@")(func.to_tsquery(literal("french", type_=REGCONFIG), terms)),
            reference,
        )
    )


def _apply_status(stmt: Select, status: str | None, *, now: datetime | None = None) -> Select:
    """SQL version of ``Law.status()``, for the three states of a published law.

    Pass ``now`` when querying several states so they agree on the same instant.
    """
    if not status:
        return stmt
    now = now or utcnow()
    opened = or_(Law.opens_at.is_(None), Law.opens_at <= now)
    not_closed = or_(Law.closes_at.is_(None), Law.closes_at > now)
    if status == STATUS_OPEN:
        return stmt.where(and_(opened, not_closed))
    if status == STATUS_SCHEDULED:
        return stmt.where(Law.opens_at.is_not(None), Law.opens_at > now)
    if status == STATUS_CLOSED:
        return stmt.where(Law.closes_at.is_not(None), Law.closes_at <= now)
    return stmt


_SLUG_ATTEMPTS = 5


def create(db: Session, *, imported: ImportedLaw | None = None, **fields) -> Law:
    """Insert a law, deriving a unique slug from the title if none is given.

    Two concurrent creations can pick the same slug. The unique constraint
    rejects one of them, which then retries with a fresh slug. An explicit slug
    is never retried.

    ``imported`` is the staged row the law comes from, linked in the same
    transaction. Retries use a savepoint, so the caller's lock on it holds.
    """
    explicit = bool(fields.get("slug"))
    for attempt in range(1, _SLUG_ATTEMPTS + 1):
        if not explicit:
            fields["slug"] = unique_slug(db, fields.get("title", ""))
        law = Law(**fields)
        try:
            with db.begin_nested():
                db.add(law)
            break
        except IntegrityError:
            if explicit or attempt == _SLUG_ATTEMPTS:
                raise
    if imported is not None:
        imported.status = STATUS_IMPORTED
        imported.law_id = law.id
    db.commit()
    db.refresh(law)
    return law


def _as_text(value) -> str:
    """Format a value for the (public) revision log."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, datetime):
        return french_datetime(value)
    if isinstance(value, date):
        # After the datetime check, since datetime subclasses date.
        return french_date(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def update(db: Session, law: Law, **fields) -> Law:
    """Apply an edit, logging changes if the law is already published.

    Draft edits aren't logged: nobody has voted on a draft.
    """
    record = law.published
    changed_at = utcnow()
    for key, value in fields.items():
        before = getattr(law, key, None)
        if before == value:
            continue
        if record and key in TRACKED_FIELDS:
            db.add(
                LawRevision(
                    law_id=law.id,
                    changed_at=changed_at,
                    field=key,
                    old_value=_as_text(before),
                    new_value=_as_text(value),
                )
            )
        setattr(law, key, value)
    db.commit()
    db.refresh(law)
    return law


def closing_from_now(db: Session, law: Law, closes_at: datetime | None) -> datetime | None:
    """``closes_at``, moved up to now if it would end an ongoing vote in the past.

    Ballots already under way still see the old closing and commit after the
    new one. Closing now leaves them the grace before the figures go final
    (see `app.crud.demographics.is_final`).
    """
    now = utcnow()
    if closes_at is None or as_utc(closes_at) >= now:
        return closes_at
    if law.closes_at is not None and as_utc(law.closes_at) <= now:
        return closes_at  # already closed: see `moves_final_closing`
    return now if results_crud.count_votes(db, law.id) else closes_at


def moves_final_closing(db: Session, law: Law, closes_at: datetime | None) -> bool:
    """Whether ``closes_at`` would move the closing of a closed vote that has votes.

    Its closing page showed live counters (see `app.crud.demographics`).
    Reopening it would let the next snapshot step be compared with that page,
    which can isolate one ballot.
    """
    if law.closes_at is None or as_utc(law.closes_at) > utcnow():
        return False
    if closes_at is not None and as_utc(closes_at) == as_utc(law.closes_at):
        return False
    return results_crud.count_votes(db, law.id) > 0


# Each revision holds a field's full old and new values, so pages stay short.
REVISIONS_PAGE_SIZE = 20


def revisions(
    db: Session, law_id: int, *, page: int = 1, page_size: int = REVISIONS_PAGE_SIZE
) -> Page[LawRevision]:
    """Recorded changes to this law, newest first, paginated."""
    stmt = select(LawRevision).where(LawRevision.law_id == law_id)
    total = count_matching(db, stmt)
    number, pages, offset = paginate(page, total, page_size)
    rows = list(
        db.scalars(
            stmt.order_by(LawRevision.changed_at.desc(), LawRevision.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    )
    return Page(rows=rows, total=total, number=number, count=pages)


def count_revisions(db: Session, law_id: int) -> int:
    return (
        db.scalar(select(func.count()).select_from(LawRevision).where(LawRevision.law_id == law_id))
        or 0
    )


def archive(db: Session, law: Law) -> Law:
    """Hide a law from the site, keeping its votes. Reversible and idempotent.

    A second call must not move ``archived_at``.
    """
    if law.archived_at is None:
        law.archived_at = utcnow()
        db.commit()
        db.refresh(law)
    return law


def restore(db: Session, law: Law) -> Law:
    law.archived_at = None
    db.commit()
    db.refresh(law)
    return law


def purge(db: Session, law: Law) -> None:
    """Permanently delete a law and its votes. Only offered for archived laws."""
    db.delete(law)
    db.commit()


def list_all(
    db: Session, *, page: int = 1, page_size: int = 50, archived: bool = False
) -> Page[Law]:
    """Laws for the admin, paginated, most recently created first.

    Archived laws are listed separately.
    """
    stmt = select(Law).where(
        Law.archived_at.is_not(None) if archived else Law.archived_at.is_(None)
    )

    total = count_matching(db, stmt)
    number, pages, offset = paginate(page, total, page_size)
    rows = list(
        db.scalars(
            stmt.options(*_TITLES_ONLY_DEFERRED)
            .order_by(Law.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
    )
    return Page(rows=rows, total=total, number=number, count=pages)


def count_archived(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Law).where(Law.archived_at.is_not(None))) or 0
