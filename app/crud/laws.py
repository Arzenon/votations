"""CRUD and queries for laws."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime

from sqlalchemy import Select, and_, func, literal, literal_column, or_, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.orm import Session

from app.db import utcnow
from app.labels import french_datetime
from app.models import (
    SEARCH_VECTOR_QUERY_SQL,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_SCHEDULED,
    Law,
    LawRevision,
)

# Columns whose change is recorded once a law is published. Everything an
# administrator can edit is here: the reader's question is "was this text the one
# I voted on", and any of these changes the answer.
TRACKED_FIELDS = (
    "title",
    "official_ref",
    "legifrance_url",
    "summary_text",
    "practical_changes",
    "arguments_for",
    "arguments_against",
    "opens_at",
    "closes_at",
    "published",
)


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)[:255] or "loi"


def unique_slug(db: Session, title: str) -> str:
    base = slugify(title)
    slug = base
    n = 2
    while db.scalar(select(Law.id).where(Law.slug == slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def get_by_slug(db: Session, slug: str) -> Law | None:
    return db.scalar(select(Law).where(Law.slug == slug))


def get_by_id(db: Session, law_id: int) -> Law | None:
    return db.get(Law, law_id)


def list_published(db: Session, *, limit: int = 50) -> list[Law]:
    """Published, non-archived laws, newest first (home page)."""
    stmt = (
        select(Law)
        .where(Law.published.is_(True), Law.archived_at.is_(None))
        .order_by(Law.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def search(
    db: Session,
    *,
    query: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Law], int, int]:
    """Advanced search (V1): across title/ref/summary, with status filter + pagination.

    Returns (rows, total_matching, pages). Both the filter and the page window are
    applied in SQL, so a large corpus is never loaded into the process to be
    sliced afterwards, and ``page`` is clamped to the last real page — an
    unbounded page number used to issue an OFFSET the database would happily walk.
    """
    stmt = select(Law).where(Law.published.is_(True), Law.archived_at.is_(None))
    if query:
        stmt = _apply_search(stmt, query)
    stmt = _apply_status(stmt, status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, -(-total // page_size))  # ceil, without importing math
    page = min(max(1, page), pages)
    rows = list(
        db.scalars(
            stmt.order_by(Law.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    )
    return rows, total, pages


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards typed by a visitor, so `%` searches for a `%`."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Characters that mean something to `to_tsquery` and nothing to a visitor.
_TSQUERY_NOISE = re.compile(r"[^\w\s-]", flags=re.UNICODE)


def _tsquery(query: str) -> str:
    """Turn what someone typed into a French tsquery, last word as a prefix.

    ``réforme des retraites`` becomes ``réforme & des & retraite:*`` — every word
    required, the last one open-ended so the results narrow as you type rather
    than vanishing on the way to a complete word. Stemming is what makes
    « retraite » find « retraites », and the French dictionary drops the
    stop-words on its own, so « des » costs nothing.

    Punctuation is stripped rather than escaped: a visitor typing an apostrophe
    or a colon means it as text, and `to_tsquery` would read it as syntax and
    fail the whole query.
    """
    words = _TSQUERY_NOISE.sub(" ", query).split()
    if not words:
        return ""
    *rest, last = words
    return " & ".join([*rest, f"{last}:*"])


#: The columns the full-text index covers. `official_ref` is deliberately absent:
#: « LOI n° 2026-214 » is looked up as a string, not as prose, so it keeps a plain
#: substring match alongside.
#:
#: The same expression the index is built on — defined beside the table, so the
#: two cannot drift and quietly leave every search on a sequential scan.
_SEARCH_VECTOR = literal_column(SEARCH_VECTOR_QUERY_SQL)


def _apply_search(stmt: Select, query: str) -> Select:
    """Full-text over the prose, substring over the reference.

    Two mechanisms because there are two kinds of search: people look for a
    *subject* in the title and summary, where stemming helps, and for a *number*
    in the official reference, where it would only get in the way.
    """
    like = f"%{_escape_like(query)}%"
    reference = Law.official_ref.ilike(like, escape="\\")

    terms = _tsquery(query)
    if not terms:
        return stmt.where(reference)
    return stmt.where(
        or_(
            _SEARCH_VECTOR.op("@@")(
                func.to_tsquery(literal("french", type_=REGCONFIG), terms)
            ),
            reference,
        )
    )


def _apply_status(stmt: Select, status: str | None) -> Select:
    """Translate the derived status into window predicates the database can filter on.

    ``Law.status()`` computes the same thing from the live clock; expressing it in
    SQL is what lets the page window be applied before rows are fetched. Only the
    three states a published law can be in are handled — the caller has already
    required ``published``, so "draft" cannot match anything.
    """
    if not status:
        return stmt
    now = utcnow()
    opened = or_(Law.opens_at.is_(None), Law.opens_at <= now)
    not_closed = or_(Law.closes_at.is_(None), Law.closes_at > now)
    if status == STATUS_OPEN:
        return stmt.where(and_(opened, not_closed))
    if status == STATUS_SCHEDULED:
        return stmt.where(Law.opens_at.is_not(None), Law.opens_at > now)
    if status == STATUS_CLOSED:
        return stmt.where(Law.closes_at.is_not(None), Law.closes_at <= now)
    return stmt


def create(db: Session, **fields) -> Law:
    if not fields.get("slug"):
        fields["slug"] = unique_slug(db, fields.get("title", ""))
    law = Law(**fields)
    db.add(law)
    db.commit()
    db.refresh(law)
    return law


def _as_text(value) -> str:
    """Render a column value for the revision record."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, datetime):
        # Readable, not machine-readable: this ends up on a public page.
        return french_datetime(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def update(db: Session, law: Law, **fields) -> Law:
    """Apply an edit, recording what changed if the law was already published.

    The record is what makes the transparency claim checkable: a law whose text
    moves while people vote on it would otherwise leave a published result
    describing something nobody read. Draft edits are not recorded — nobody has
    voted on a draft, and logging the ordinary act of writing the presentation
    would bury the changes that matter.
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


def revisions(db: Session, law_id: int) -> list[LawRevision]:
    """Every recorded change to this law, newest first."""
    return list(
        db.scalars(
            select(LawRevision)
            .where(LawRevision.law_id == law_id)
            .order_by(LawRevision.changed_at.desc(), LawRevision.id.desc())
        )
    )


def count_revisions(db: Session, law_id: int) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(LawRevision).where(LawRevision.law_id == law_id)
        )
        or 0
    )


def archive(db: Session, law: Law) -> Law:
    """Withdraw a law from the site without destroying anything.

    The votes stay. This is reversible, which is the whole point: deleting used to
    cascade away published open data behind a single click.
    """
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
    """Delete a law and every vote on it, for good.

    Deliberately separate from :func:`archive` and reachable only for a law that
    is already archived: reclaiming the disk an old votation occupies is a real
    operational need, but it must be a decision someone took twice.
    """
    db.delete(law)
    db.commit()


def list_all(
    db: Session, *, page: int = 1, page_size: int = 50, archived: bool = False
) -> tuple[list[Law], int, int]:
    """Laws for the administration, paginated. Archived ones are a separate view."""
    stmt = select(Law)
    stmt = stmt.where(Law.archived_at.is_not(None) if archived else Law.archived_at.is_(None))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)
    rows = list(
        db.scalars(
            stmt.order_by(Law.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    )
    return rows, total, pages


def count_archived(db: Session) -> int:
    return (
        db.scalar(select(func.count()).select_from(Law).where(Law.archived_at.is_not(None))) or 0
    )
