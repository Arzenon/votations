"""Aggregating votes into the deliberately simple, manipulation-resistant stats.

Only three numbers are ever surfaced publicly: % for / % against / % abstain and
the total voter count. The V1 evolution curve buckets votes over time, gated by a
minimum volume to prevent re-identification.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Vote, VoteChoice


@dataclass
class Tally:
    total: int
    counts: dict[str, int]
    percentages: dict[str, float]


def tally(db: Session, law_id: int) -> Tally:
    rows = db.execute(
        select(Vote.choice, func.count())
        .where(Vote.law_id == law_id)
        .group_by(Vote.choice)
    ).all()
    counts = {c.value: 0 for c in VoteChoice}
    for choice, n in rows:
        counts[choice] = n
    total = sum(counts.values())
    pct = {
        k: (round(100 * v / total, 1) if total else 0.0) for k, v in counts.items()
    }
    return Tally(total=total, counts=counts, percentages=pct)


@dataclass
class EvolutionBucket:
    label: str
    counts: dict[str, int] = field(default_factory=dict)


def evolution(db: Session, law_id: int, *, granularity: str = "day") -> list[EvolutionBucket]:
    """Vote counts bucketed over time (V1).

    Returns an empty list until the total reaches ``MIN_BUCKET_VOLUME`` so that
    low-volume votations cannot be used to re-identify or fingerprint voters.
    """
    settings = get_settings()
    votes = list(db.scalars(select(Vote).where(Vote.law_id == law_id)))
    if len(votes) < settings.min_bucket_volume:
        return []

    fmt = "%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d"
    buckets: dict[str, Counter] = {}
    for v in votes:
        created = v.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        key = created.astimezone(UTC).strftime(fmt)
        buckets.setdefault(key, Counter())[v.choice] += 1

    out: list[EvolutionBucket] = []
    for label in sorted(buckets):
        counts = {c.value: buckets[label].get(c.value, 0) for c in VoteChoice}
        out.append(EvolutionBucket(label=label, counts=counts))
    return out


def export_rows(db: Session, law_id: int) -> list[dict]:
    """Raw anonymised rows for open-data export.

    Contains ONLY law_id, choice and created_at. No voter token, no identifier.
    """
    votes = db.scalars(
        select(Vote).where(Vote.law_id == law_id).order_by(Vote.created_at)
    )
    rows = []
    for v in votes:
        created: datetime = v.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        rows.append(
            {
                "law_id": v.law_id,
                "choice": v.choice,
                "created_at": created.astimezone(UTC).isoformat(),
            }
        )
    return rows
