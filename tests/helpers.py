"""Helpers shared by more than one test module.

Three of them existed in three or four near-identical copies, which is how the
administration headers came to differ by whether they carried an ``Origin`` —
harmless until a test moved between files and started exercising a different
path from the one it was written for.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DemographicTally, Law


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
