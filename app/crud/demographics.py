"""Optional demographics: aggregate counters and suppressed breakdowns.

``record`` adds a voter's optional age/sex/region to per-law counters, once,
without storing anything per voter. ``all_breakdowns`` reads them back with
cell suppression, and from a snapshot while the vote is open (see
``refresh_snapshot``).

Nothing here commits: the route commits the vote and its counters together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crud import results
from app.crud.results import Tally
from app.db import as_utc, utcnow
from app.models import (
    DIMENSION_AGE,
    DIMENSION_LABELS,
    DIMENSION_ORDER,
    DIMENSION_REGION,
    DIMENSION_SEXE,
    DIMENSIONS,
    INSEE_REFERENCE,
    VALID_CATEGORIES,
    DemographicTally,
    InseeReference,
    Law,
    Vote,
    VoteChoice,
)


def _add(db: Session, law_id: int, dimension: str, category: str, choice: str, delta: int) -> None:
    """Atomically add ``delta`` to a counter (upsert). Never goes below zero."""
    stmt = pg_insert(DemographicTally).values(
        law_id=law_id,
        dimension=dimension,
        category=category,
        choice=choice,
        count=max(delta, 0),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_demo_cell",
        set_={"count": func.greatest(DemographicTally.count + delta, 0)},
    )
    db.execute(stmt)


def _requested(age_bracket: str | None, sexe: str | None, region: str | None) -> list[tuple]:
    """The valid (dimension, category) pairs that were supplied."""
    candidates = {
        DIMENSION_AGE: age_bracket,
        DIMENSION_SEXE: sexe,
        DIMENSION_REGION: region,
    }
    return [(dim, cat) for dim, cat in candidates.items() if cat and cat in VALID_CATEGORIES[dim]]


def _known(categories: dict[str, str] | None) -> dict[str, str]:
    """Drop unknown (dimension, category) pairs."""
    return {
        dimension: category
        for dimension, category in (categories or {}).items()
        if dimension in VALID_CATEGORIES and category in VALID_CATEGORIES[dimension]
    }


def record(
    db: Session,
    *,
    vote: Vote,
    choice: VoteChoice,
    previous_choice: str | None = None,
    age_bracket: str | None = None,
    sexe: str | None = None,
    region: str | None = None,
    previous_categories: dict[str, str] | None = None,
) -> dict[str, str]:
    """Add the voter's optional profile to the counters, once.

    If the voter later changes their vote, the contribution moves to the new
    choice. The vote row doesn't store the categories: they come back from the
    voter's cookie as ``previous_categories`` (see `app.profile_cookie`).
    ``previous_choice`` comes from `cast_vote`.

    Returns the categories to store back in the cookie (empty if none).
    """
    if vote.demographics_recorded:
        return _move(
            db,
            vote=vote,
            choice=choice,
            previous_choice=previous_choice,
            categories=_known(previous_categories),
        )

    provided = _requested(age_bracket, sexe, region)
    if not provided:
        return {}

    for dimension, category in provided:
        _add(db, vote.law_id, dimension, category, choice.value, +1)
    vote.demographics_recorded = True
    return dict(provided)


def _move(
    db: Session,
    *,
    vote: Vote,
    choice: VoteChoice,
    previous_choice: str | None,
    categories: dict[str, str],
) -> dict[str, str]:
    """Move an existing contribution to the new choice."""
    if previous_choice is None or previous_choice == choice.value:
        return categories

    if not categories:
        # The cookie is gone, so we can't know which cells to move. The
        # contribution stays under the old choice for good: the profile fields
        # are no longer offered, so it can't be counted twice.
        return {}

    for dimension, category in categories.items():
        _add(db, vote.law_id, dimension, category, previous_choice, -1)
        _add(db, vote.law_id, dimension, category, choice.value, +1)
    return categories


@dataclass
class BreakdownRow:
    category: str
    counts: dict[str, int]
    total: int
    percentages: dict[str, float]
    # Share of this category among profiled voters, vs the national reference.
    share: float = 0.0
    reference: float | None = None


# Why a breakdown is hidden.
REASON_NONE = ""
REASON_EMPTY = "empty"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_PENDING = "pending"


@dataclass
class Breakdown:
    dimension: str
    label: str
    shown: bool
    threshold: int
    # A REASON_* value. "pending" means the vote is open and no snapshot has
    # been published yet, which is different from having no data.
    reason: str = REASON_NONE

    @property
    def is_below_threshold(self) -> bool:
        return self.reason == REASON_BELOW_THRESHOLD

    @property
    def is_pending(self) -> bool:
        return self.reason == REASON_PENDING

    rows: list[BreakdownRow] = field(default_factory=list)
    dimension_total: int = 0
    reference_year: int | None = None  # INSEE source year if cached values are used


def all_references(db: Session) -> dict[str, tuple[dict[str, float], int | None]]:
    """Reference shares for every dimension, in one query."""
    rows = db.execute(
        select(
            InseeReference.dimension,
            InseeReference.category,
            InseeReference.percentage,
            InseeReference.source_year,
        )
    ).all()
    cached: dict[str, dict[str, float]] = {}
    years: dict[str, int] = {}
    for dimension, category, percentage, year in rows:
        cached.setdefault(dimension, {})[category] = percentage
        years.setdefault(dimension, year)

    out: dict[str, tuple[dict[str, float], int | None]] = {}
    for dimension in DIMENSIONS:
        if dimension in cached:
            out[dimension] = (cached[dimension], years.get(dimension))
        else:
            out[dimension] = (INSEE_REFERENCE.get(dimension, {}), None)
    return out


def _build(
    dimension: str,
    cells: dict[str, dict[str, int]],
    *,
    k: int,
    reference: dict[str, float],
    reference_year: int | None,
) -> Breakdown:
    """Build one dimension's table.

    If any non-empty cell has fewer than ``k`` votes, the whole table is
    hidden, so a small cell can't be recovered by subtraction.
    """
    label = DIMENSION_LABELS.get(dimension, dimension)
    populated = [n for cat in cells.values() for n in cat.values() if n > 0]
    if not populated:
        return Breakdown(dimension, label, shown=False, threshold=k, reason=REASON_EMPTY)
    if min(populated) < k:
        return Breakdown(dimension, label, shown=False, threshold=k, reason=REASON_BELOW_THRESHOLD)

    dimension_total = sum(populated)
    # Skip empty categories. A row of zeros would reveal that someone just
    # picked that category, before the snapshot catches up.
    ordered = [
        c
        for c in DIMENSION_ORDER.get(dimension, sorted(cells))
        if c in cells and sum(cells[c].values()) > 0
    ]
    out_rows: list[BreakdownRow] = []
    for category in ordered:
        counts = cells[category]
        total = sum(counts.values())
        pct = {ch: (round(100 * v / total, 1) if total else 0.0) for ch, v in counts.items()}
        share = round(100 * total / dimension_total, 1) if dimension_total else 0.0
        out_rows.append(
            BreakdownRow(
                category=category,
                counts=counts,
                total=total,
                percentages=pct,
                share=share,
                reference=reference.get(category),
            )
        )

    return Breakdown(
        dimension,
        label,
        shown=True,
        threshold=k,
        rows=out_rows,
        dimension_total=dimension_total,
        reference_year=reference_year,
    )


# Minimum time between two snapshots. Mostly limits how often the profiled
# votes get counted; the volume rule is what protects voters.
_SNAPSHOT_INTERVAL = timedelta(hours=1)


def refresh_snapshot(db: Session, law: Law) -> bool:
    """Update the published breakdown if it's due. Returns True if it moved.

    Without a snapshot, comparing the public page before and after a single
    ballot would reveal that voter's choice along with their age, sex and
    region. So while the vote is open the page shows ``published_count``,
    which only moves when:

    * at least ``_SNAPSHOT_INTERVAL`` has passed since the last move, and
    * at least ``min_bucket_volume`` profiled votes have arrived since.

    Called during the ballot transaction, before the voter's own contribution
    is added. Does not commit.
    """
    if not law.is_open():
        return False

    now = utcnow()
    last = law.demographics_snapshot_at
    if last is not None and now - as_utc(last) < _SNAPSHOT_INTERVAL:
        return False

    profiled = results.count_profiled(db, law.id)
    if profiled - law.demographics_snapshot_profiles < get_settings().min_bucket_volume:
        return False

    db.execute(
        update(DemographicTally)
        .where(DemographicTally.law_id == law.id)
        .values(published_count=DemographicTally.count)
    )
    law.demographics_snapshot_at = now
    law.demographics_snapshot_profiles = profiled
    return True


def publishes_live_counters(db: Session, law: Law, *, profiled: int | None = None) -> bool:
    """Whether breakdowns may use the live counters instead of the snapshot.

    Closing the vote is one more step and follows the same volume rule:
    otherwise a single ballot cast after the last snapshot could be recovered
    by comparing the page before and after closing. If no snapshot was ever
    published there is nothing to compare against, so live counters are fine.

    ``profiled`` is ``Tally.with_profile``, to save a query.
    """
    if law.is_open():
        return False
    if law.demographics_snapshot_at is None:
        return True
    if profiled is None:
        profiled = results.count_profiled(db, law.id)
    return profiled - law.demographics_snapshot_profiles >= get_settings().min_bucket_volume


def published_as_of(db: Session, law: Law, *, profiled: int | None = None) -> datetime | None:
    """Date of the snapshot being shown, or None if the figures are live.

    Shared by the results page and the JSON export so they say the same thing.
    """
    if publishes_live_counters(db, law, profiled=profiled):
        return None
    return law.demographics_snapshot_at


def _cells_by_dimension(
    db: Session, law_id: int, *, live: bool
) -> dict[str, dict[str, dict[str, int]]]:
    """All counters for a law as {dimension: {category: {choice: n}}}.

    ``live`` reads ``count``, otherwise the ``published_count`` snapshot.
    """
    counter = DemographicTally.count if live else DemographicTally.published_count
    rows = db.execute(
        select(
            DemographicTally.dimension,
            DemographicTally.category,
            DemographicTally.choice,
            counter,
        ).where(DemographicTally.law_id == law_id)
    ).all()
    out: dict[str, dict[str, dict[str, int]]] = {}
    for dimension, category, choice, count in rows:
        by_category = out.setdefault(dimension, {})
        by_category.setdefault(category, {c.value: 0 for c in VoteChoice})[choice] = count
    return out


def all_breakdowns(
    db: Session, law: Law, *, min_cell: int | None = None, profiled: int | None = None
) -> list[Breakdown]:
    """Breakdowns for every dimension, in two queries."""
    k = min_cell if min_cell is not None else get_settings().min_cell_count
    live = publishes_live_counters(db, law, profiled=profiled)
    if law.demographics_snapshot_at is None and law.is_open():
        # Open with no snapshot yet: everything is "pending", not "empty".
        return [
            Breakdown(
                dim, DIMENSION_LABELS.get(dim, dim), shown=False, threshold=k, reason=REASON_PENDING
            )
            for dim in DIMENSIONS
        ]

    by_dimension = _cells_by_dimension(db, law.id, live=live)
    references = all_references(db)
    out = []
    for dimension in DIMENSIONS:
        reference, year = references.get(dimension, ({}, None))
        out.append(
            _build(
                dimension,
                by_dimension.get(dimension, {}),
                k=k,
                reference=reference,
                reference_year=year,
            )
        )
    return out


@dataclass
class ProvisionRate:
    total_votes: int
    with_profile: int
    percentage: float


def provision_rate(db: Session, law_id: int, *, tally: Tally | None = None) -> ProvisionRate:
    """Share of voters who gave a profile. Pass ``tally`` to avoid a query."""
    if tally is None:
        tally = results.tally(db, law_id)
    pct = round(100 * tally.with_profile / tally.total, 1) if tally.total else 0.0
    return ProvisionRate(total_votes=tally.total, with_profile=tally.with_profile, percentage=pct)
