"""Recording optional demographics into aggregate counters, and reading them
back with cell suppression (k-anonymity).

Two responsibilities:

1. ``record`` — fold a voter's optional age/sexe into the counters, **once**, with
   no per-voter row and no token. Concurrency-safe via an upsert.
2. ``breakdown`` — build a demographic × choice table for display/export, applying
   whole-table suppression so small cells can never be read or differenced out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DIMENSION_AGE,
    DIMENSION_ORDER,
    DIMENSION_SEXE,
    VALID_CATEGORIES,
    DemographicTally,
    Vote,
    VoteChoice,
)

DIMENSIONS = (DIMENSION_AGE, DIMENSION_SEXE)
DIMENSION_LABELS = {DIMENSION_AGE: "Tranche d'âge", DIMENSION_SEXE: "Sexe"}


def _increment(db: Session, law_id: int, dimension: str, category: str, choice: str) -> None:
    """Atomically add one to a counter cell (INSERT … ON CONFLICT DO UPDATE)."""
    stmt = pg_insert(DemographicTally).values(
        law_id=law_id, dimension=dimension, category=category, choice=choice, count=1
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_demo_cell",
        set_={"count": DemographicTally.count + 1},
    )
    db.execute(stmt)


def record(
    db: Session,
    *,
    vote: Vote,
    choice: VoteChoice,
    age_bracket: str | None,
    sexe: str | None,
) -> None:
    """Fold this voter's optional demographics into the counters, exactly once.

    Invalid or empty values are ignored. Does nothing if this voter already
    contributed (``vote.demographics_recorded``), which keeps a vote *change*
    from double-counting. The vote's choice is captured here and not revised on
    later changes — see module/README notes.
    """
    if vote.demographics_recorded:
        return

    provided: list[tuple[str, str]] = []
    if age_bracket and age_bracket in VALID_CATEGORIES[DIMENSION_AGE]:
        provided.append((DIMENSION_AGE, age_bracket))
    if sexe and sexe in VALID_CATEGORIES[DIMENSION_SEXE]:
        provided.append((DIMENSION_SEXE, sexe))

    if not provided:
        return

    for dimension, category in provided:
        _increment(db, vote.law_id, dimension, category, choice.value)
    vote.demographics_recorded = True
    db.commit()


@dataclass
class BreakdownRow:
    category: str
    counts: dict[str, int]
    total: int
    percentages: dict[str, float]


@dataclass
class Breakdown:
    dimension: str
    label: str
    shown: bool
    threshold: int
    reason: str = ""  # "" | "empty" | "below_threshold"
    rows: list[BreakdownRow] = field(default_factory=list)


def breakdown(
    db: Session, law_id: int, dimension: str, *, min_cell: int | None = None
) -> Breakdown:
    """Return the choice breakdown for one dimension, suppressed if too sparse.

    Suppression rule (whole-table gate): if **any** populated cell holds fewer
    than ``min_cell`` votes, the entire breakdown is withheld. Nothing partial is
    ever returned, so a small cell can neither be read directly nor reconstructed
    by differencing against shown cells, totals, or the other dimension's table.
    """
    k = min_cell if min_cell is not None else get_settings().min_cell_count
    label = DIMENSION_LABELS.get(dimension, dimension)

    rows = db.execute(
        select(DemographicTally.category, DemographicTally.choice, DemographicTally.count)
        .where(DemographicTally.law_id == law_id, DemographicTally.dimension == dimension)
    ).all()

    cells: dict[str, dict[str, int]] = {}
    for category, choice, count in rows:
        cells.setdefault(category, {c.value: 0 for c in VoteChoice})[choice] = count

    populated = [n for cat in cells.values() for n in cat.values() if n > 0]
    if not populated:
        return Breakdown(dimension, label, shown=False, threshold=k, reason="empty")
    if min(populated) < k:
        return Breakdown(dimension, label, shown=False, threshold=k, reason="below_threshold")

    ordered = [c for c in DIMENSION_ORDER.get(dimension, sorted(cells)) if c in cells]
    out_rows: list[BreakdownRow] = []
    for category in ordered:
        counts = cells[category]
        total = sum(counts.values())
        pct = {ch: (round(100 * v / total, 1) if total else 0.0) for ch, v in counts.items()}
        out_rows.append(
            BreakdownRow(category=category, counts=counts, total=total, percentages=pct)
        )

    return Breakdown(dimension, label, shown=True, threshold=k, rows=out_rows)


def all_breakdowns(db: Session, law_id: int) -> list[Breakdown]:
    return [breakdown(db, law_id, dim) for dim in DIMENSIONS]
