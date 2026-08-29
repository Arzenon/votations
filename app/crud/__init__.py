"""Database access helpers.

Two pieces of plumbing every paginated listing needs, kept here rather than
copied into each module: they were duplicated three times, and each copy was a
place the clamp or the count could be got subtly wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class Page[T]:
    """One page of a listing: its rows, and where they sit in the whole.

    ``number`` is the page actually served, already clamped — which is the whole
    reason this is a type rather than a tuple. :func:`paginate` computes the
    clamp, every listing threw it away, and each of the five pages that display
    « page n / N » re-derived it with its own copy of
    ``min(max(1, page), pages)``. Five copies of a value one function already
    had is five chances to clamp against the wrong bound.
    """

    rows: list[T]
    #: Rows matching the query, across every page.
    total: int
    #: The page served, clamped into ``1..count``.
    number: int
    #: How many pages there are. At least 1, even when there is nothing to show.
    count: int


def paginate(page: int, total: int, page_size: int) -> tuple[int, int, int]:
    """Clamp ``page`` to the last real page. Returns (page, pages, offset).

    An unbounded page number used to issue an OFFSET the database would walk to
    the end of the table before returning nothing.
    """
    pages = max(1, -(-total // page_size))  # ceil, without importing math
    page = min(max(1, page), pages)
    return page, pages, (page - 1) * page_size
