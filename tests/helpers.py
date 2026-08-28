"""Helpers shared by more than one test module.

Three of them existed in three or four near-identical copies, which is how the
administration headers came to differ by whether they carried an ``Origin`` —
harmless until a test moved between files and started exercising a different
path from the one it was written for.
"""

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
    """An HTTP Basic header for these credentials, right or wrong."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def admin_headers() -> dict[str, str]:
    """What a browser on this site sends to the administration.

    Carries ``Origin`` as well as the credentials, because every state-changing
    route checks it. Harmless on a GET, where the check returns immediately.
    """
    settings = get_settings()
    return {
        **basic_auth(settings.admin_username, settings.admin_password),
        "Origin": "http://testserver",
    }


def make_law(db: Session, **kwargs) -> Law:
    """A published law, open for voting, overridable field by field."""
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
    """One ballot, cast the way the vote route casts it.

    The vote and the counters it moves are one transaction, committed once — so a
    test that reads ``demographics_recorded`` back out of SQL sees what a request
    would have left behind. ``previous_categories`` stands in for the profile
    cookie the browser would send back.
    """
    vote, previous_choice = votes_crud.cast_vote(db, law, token, choice)
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


def demographic_cells(db: Session, law_id: int) -> dict[tuple[str, str, str], int]:
    """Every aggregate counter for a law, as {(dimension, category, choice): n}."""
    rows = db.query(DemographicTally).filter_by(law_id=law_id).all()
    return {(r.dimension, r.category, r.choice): r.count for r in rows}


def publish_laws(db: Session, n: int) -> None:
    """``n`` published, open laws, newest first — for listings and pagination."""
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
    """What the application logger emitted inside the block, as a list of lines.

    Not ``caplog``: :func:`app.logging_config.configure_logging` sets
    ``propagate = False`` on the application logger — deliberately, so its records
    go to one stderr handler and nowhere else — and pytest's fixture listens on
    the root logger, which therefore never sees them. Attaching to the logger
    itself is what makes them assertable.
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
