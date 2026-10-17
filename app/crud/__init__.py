"""Database access helpers shared by the paginated listings."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, func
from sqlalchemy.orm import Session


def count_matching(db: Session, stmt: Select) -> int:
    """Count the rows a statement matches.

    ``maintain_column_froms`` keeps the FROM clause: without it, a statement
    with no WHERE became a bare ``SELECT count(*)``, which returns 1.
    """
    counted = stmt.with_only_columns(func.count(), maintain_column_froms=True)
    return db.scalar(counted.order_by(None)) or 0


@dataclass(frozen=True)
class Page[T]:
    """One page of a listing."""

    rows: list[T]
    # Rows matching the query, across all pages.
    total: int
    # The page served, clamped to 1..count.
    number: int
    # Number of pages, at least 1.
    count: int


def paginate(page: int, total: int, page_size: int) -> tuple[int, int, int]:
    """Clamp ``page`` to the last real page. Returns (page, pages, offset)."""
    pages = max(1, -(-total // page_size))  # ceil
    page = min(max(1, page), pages)
    return page, pages, (page - 1) * page_size
