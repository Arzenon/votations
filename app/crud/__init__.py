"""Database access helpers.

Two pieces of plumbing every paginated listing needs, kept here rather than
copied into each module: they were duplicated three times, and each copy was a
place the clamp or the count could be got subtly wrong.
"""

from __future__ import annotations

from sqlalchemy import Select, func
from sqlalchemy.orm import Session


def count_matching(db: Session, stmt: Select) -> int:
    """How many rows a statement matches, without building any of them.

    ``select(count()).select_from(stmt.subquery())`` asks the database for
    ``SELECT count(*) FROM (SELECT laws.* …)`` and trusts the planner to notice
    that none of those columns is wanted. Replacing the column list says it
    outright, so the prose and the JSON blocks are never read to be discarded.

    ``maintain_column_froms`` is what makes that safe. Without it the FROM is
    inferred from the *new* column list, and ``count()`` names no table: a
    statement with no WHERE clause to hold the table in place became a bare
    ``SELECT count(*)``, which is perfectly valid SQL and always answers 1.
    """
    counted = stmt.with_only_columns(func.count(), maintain_column_froms=True)
    return db.scalar(counted.order_by(None)) or 0


def paginate(page: int, total: int, page_size: int) -> tuple[int, int, int]:
    """Clamp ``page`` to the last real page. Returns (page, pages, offset).

    An unbounded page number used to issue an OFFSET the database would walk to
    the end of the table before returning nothing.
    """
    pages = max(1, -(-total // page_size))  # ceil, without importing math
    page = min(max(1, page), pages)
    return page, pages, (page - 1) * page_size
