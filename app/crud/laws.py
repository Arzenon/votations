"""CRUD and queries for laws."""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Law


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)[:255] or "loi"


def unique_slug(db: Session, title: str) -> str:
    base = slugify(title)
    slug = base
    n = 2
    while db.scalar(select(Law).where(Law.slug == slug)):
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

    Returns (rows, total_matching). Status is derived in Python so it stays
    correct against the live clock regardless of DB engine.
    """
    stmt = select(Law).where(Law.published.is_(True))
    if query:
        stmt = _apply_search(stmt, query)
    rows = list(db.scalars(stmt.order_by(Law.created_at.desc())))
    if status:
        rows = [law for law in rows if law.status() == status]
    total = len(rows)
    start = (page - 1) * page_size
    return rows[start : start + page_size], total


def _apply_search(stmt, query: str):
    like = f"%{query}%"
    return stmt.where(
        or_(
            Law.title.ilike(like),
            Law.official_ref.ilike(like),
            Law.summary_text.ilike(like),
        )
    )


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
