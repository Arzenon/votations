"""Helpers shared by several test modules."""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import demographics as demo_crud
from app.crud import votes as votes_crud
from app.logging_config import LOGGER_NAME
from app.models import DemographicTally, Law, Vote, VoteChoice


def basic_auth(user: str, password: str) -> dict[str, str]:
    """HTTP Basic header for these credentials."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def admin_headers() -> dict[str, str]:
    """Admin credentials plus the Origin header that POST routes check."""
    settings = get_settings()
    return {
        **basic_auth(settings.admin_username, settings.admin_password),
        "Origin": "http://testserver",
    }


def make_law(db: Session, **kwargs) -> Law:
    """A published, open law. Any field can be overridden."""
    defaults = {
        "slug": "loi-x",
        "title": "Loi X",
        "published": True,
        "opens_at": datetime.now(UTC) - timedelta(hours=1),
        "closes_at": datetime.now(UTC) + timedelta(hours=24),
    }
    law = Law(**{**defaults, **kwargs})
    db.add(law)
    db.commit()
    db.refresh(law)
    return law


def cast_ballot(
    db: Session,
    law: Law,
    token: str,
    choice: VoteChoice,
    *,
    previous_categories: dict[str, str] | None = None,
    **profile: str,
) -> Vote:
    """Cast a ballot the way the vote route does, in one transaction.

    ``previous_categories`` stands in for the profile cookie.
    """
    vote, previous_choice = votes_crud.cast_vote(db, law, token, choice)
    # Same order as the route.
    demo_crud.refresh_snapshot(db, law)
    demo_crud.record(
        db,
        vote=vote,
        choice=choice,
        previous_choice=previous_choice,
        previous_categories=previous_categories,
        **profile,
    )
    db.commit()
    return vote


def breakdown(db: Session, law: Law, dimension: str) -> demo_crud.Breakdown:
    """One dimension's breakdown, as the results page builds it."""
    return next(bd for bd in demo_crud.all_breakdowns(db, law) if bd.dimension == dimension)


def demographic_cells(db: Session, law_id: int) -> dict[tuple[str, str, str], int]:
    """A law's counters as {(dimension, category, choice): n}."""
    rows = db.query(DemographicTally).filter_by(law_id=law_id).all()
    return {(r.dimension, r.category, r.choice): r.count for r in rows}


def publish_laws(db: Session, n: int) -> None:
    """Create ``n`` published, open laws, newest first."""
    base = datetime.now(UTC)
    for i in range(n):
        db.add(
            Law(
                slug=f"loi-{i}",
                title=f"Loi numéro {i}",
                published=True,
                summary_text="texte",
                created_at=base - timedelta(minutes=i),
                opens_at=base - timedelta(hours=1),
            )
        )
    db.commit()


class _Collect(logging.Handler):
    def __init__(self, into: list[str]) -> None:
        super().__init__()
        self.into = into

    def emit(self, record: logging.LogRecord) -> None:
        self.into.append(record.getMessage())


@contextmanager
def captured_logs(name: str = LOGGER_NAME, level: int = logging.WARNING) -> Iterator[list[str]]:
    """Log lines emitted by the app logger inside the block.

    caplog doesn't work: the app logger has propagate=False.
    """
    logger = logging.getLogger(name)
    lines: list[str] = []
    handler = _Collect(lines)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(min(level, previous or level))
    try:
        yield lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
