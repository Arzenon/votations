"""CRUD and queries for laws."""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    STATUS_CLOSED,
    STATUS_DRAFT,
    STATUS_OPEN,
    STATUS_SCHEDULED,
    Law,
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


def list_published(db: Session, *, query: str | None = None, limit: int = 50) -> list[Law]:
    """Published laws, newest first, with optional basic search (MVP)."""
    stmt = select(Law).where(Law.published.is_(True))
    if query:
        stmt = _apply_search(stmt, query)
    stmt = stmt.order_by(Law.created_at.desc()).limit(limit)
    return list(db.scalars(stmt))


def search(
    db: Session,
    *,
    query: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Law], int]:
    """Advanced search (V1): across title/ref/summary, with status filter + pagination.

    Returns (rows, total_matching). Both the filter and the page window are
    applied in SQL, so a large corpus is never loaded into the process to be
    sliced afterwards.
    """
    stmt = select(Law).where(Law.published.is_(True))
    if query:
        stmt = _apply_search(stmt, query)
    stmt = _apply_status(stmt, status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.order_by(Law.created_at.desc())
            .offset(max(0, (page - 1) * page_size))
            .limit(page_size)
        )
    )
    return rows, total


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards typed by a visitor, so `%` searches for a `%`."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_search(stmt: Select, query: str) -> Select:
    like = f"%{_escape_like(query)}%"
    return stmt.where(
        or_(
            Law.title.ilike(like, escape="\\"),
            Law.official_ref.ilike(like, escape="\\"),
            Law.summary_text.ilike(like, escape="\\"),
        )
    )


def _apply_status(stmt: Select, status: str | None) -> Select:
    """Translate the derived status into window predicates the database can filter on.

    ``Law.status()`` computes the same thing from the live clock; expressing it in
    SQL is what lets the page window be applied before rows are fetched.
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
    if status == STATUS_DRAFT:
        # `published` is already required above, so a draft can never match.
        return stmt.where(Law.published.is_(False))
    return stmt


def create(db: Session, **fields) -> Law:
    if not fields.get("slug"):
        fields["slug"] = unique_slug(db, fields.get("title", ""))
    law = Law(**fields)
    db.add(law)
    db.commit()
    db.refresh(law)
    return law


def update(db: Session, law: Law, **fields) -> Law:
    for key, value in fields.items():
        setattr(law, key, value)
    db.commit()
    db.refresh(law)
    return law


def delete(db: Session, law: Law) -> None:
    db.delete(law)
    db.commit()


def list_all(db: Session) -> list[Law]:
    """All laws including drafts (admin)."""
    return list(db.scalars(select(Law).order_by(Law.created_at.desc())))
