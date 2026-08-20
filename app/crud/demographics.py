"""Recording optional demographics into aggregate counters, and reading them
back with cell suppression (k-anonymity).

Two responsibilities:

1. ``record`` — fold a voter's optional age/sexe/région into the counters,
   **once**, with no per-voter row and no token. Concurrency-safe via an upsert.
2. ``breakdown`` — build a demographic × choice table for display/export, applying
   whole-table suppression so small cells can never be read or differenced out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
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
    Vote,
    VoteChoice,
)


def _add(db: Session, law_id: int, dimension: str, category: str, choice: str, delta: int) -> None:
    """Atomically move a counter cell by ``delta`` (INSERT … ON CONFLICT DO UPDATE).

    Counters never go below zero: a decrement for a cell that somehow does not
    exist is clamped rather than creating a negative total that would then be
    published.
    """
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
    """The (dimension, category) pairs actually supplied and valid."""
    candidates = {
        DIMENSION_AGE: age_bracket,
        DIMENSION_SEXE: sexe,
        DIMENSION_REGION: region,
    }
    return [(dim, cat) for dim, cat in candidates.items() if cat and cat in VALID_CATEGORIES[dim]]


def record(
    db: Session,
    *,
    vote: Vote,
    choice: VoteChoice,
    age_bracket: str | None = None,
    sexe: str | None = None,
    region: str | None = None,
    previous: dict[str, str] | None = None,
) -> dict[str, str]:
    """Fold this voter's optional demographics into the counters, exactly once.

    Invalid or empty values are ignored. A voter contributes their profile a
    single time; if they later change their vote, the contribution is *moved* to
    the new choice rather than left counted under the old one, so a breakdown
    always describes the votes as they currently stand.

    Moving it needs the categories, which the server does not store — they come
    back from the voter's own browser as ``previous`` (see
    :mod:`app.profile_cookie`). With no cookie, nothing moves and the behaviour is
    the same as before: the contribution stays under the older choice.

    Returns the categories now on file for this voter, for the caller to persist
    on the device. An empty mapping means there is nothing to remember.
    """
    if vote.demographics_recorded:
        return _move(db, vote=vote, choice=choice, previous=previous or {})

    provided = _requested(age_bracket, sexe, region)
    if not provided:
        return {}

    for dimension, category in provided:
        _add(db, vote.law_id, dimension, category, choice.value, +1)
    vote.demographics_recorded = True
    vote.demographics_choice = choice.value
    db.commit()
    return dict(provided)


def _move(
    db: Session, *, vote: Vote, choice: VoteChoice, previous: dict[str, str]
) -> dict[str, str]:
    """Re-file an existing contribution under a changed vote."""
    was = vote.demographics_choice
    if was == choice.value:
        return previous
    if was is None:
        # Written before the choice was tracked: nothing can be moved safely.
        vote.demographics_choice = choice.value
        db.commit()
        return previous
    if not previous:
        # No categories came back from the device, so nothing can be moved. Leave
        # `demographics_choice` pointing at the choice the counters actually sit
        # under: advancing it would make the column claim a contribution had moved
        # when it had not, and a later change with a restored cookie would then
        # decrement the wrong cell.
        return {}

    previous = {
        dim: cat
        for dim, cat in previous.items()
        if dim in VALID_CATEGORIES and cat in VALID_CATEGORIES[dim]
    }
    for dimension, category in previous.items():
        _add(db, vote.law_id, dimension, category, was, -1)
        _add(db, vote.law_id, dimension, category, choice.value, +1)
    vote.demographics_choice = choice.value
    db.commit()
    return previous


@dataclass
class BreakdownRow:
    category: str
    counts: dict[str, int]
    total: int
    percentages: dict[str, float]
    # Representativeness: this category's share of participants who gave a profile
    # for this dimension, vs the approximate national reference share.
    share: float = 0.0
    reference: float | None = None


@dataclass
class Breakdown:
    dimension: str
    label: str
    shown: bool
    threshold: int
    reason: str = ""  # "" | "empty" | "below_threshold"
    rows: list[BreakdownRow] = field(default_factory=list)
    dimension_total: int = 0
    reference_year: int | None = None  # INSEE source year if cached values are used


def all_references(db: Session) -> dict[str, tuple[dict[str, float], int | None]]:
    """Every dimension's reference shares, in one query."""
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
    """Assemble one dimension's table, applying the suppression rule.

    Suppression rule (whole-table gate): if **any** populated cell holds fewer
    than ``k`` votes, the entire breakdown is withheld. Nothing partial is ever
    returned, so a small cell can neither be read directly nor reconstructed by
    differencing against shown cells, totals, or another dimension's table.
    """
    label = DIMENSION_LABELS.get(dimension, dimension)
    populated = [n for cat in cells.values() for n in cat.values() if n > 0]
    if not populated:
        return Breakdown(dimension, label, shown=False, threshold=k, reason="empty")
    if min(populated) < k:
        return Breakdown(dimension, label, shown=False, threshold=k, reason="below_threshold")

    dimension_total = sum(populated)
    ordered = [c for c in DIMENSION_ORDER.get(dimension, sorted(cells)) if c in cells]
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


def _cells_by_dimension(db: Session, law_id: int) -> dict[str, dict[str, dict[str, int]]]:
    """Every counter for a law, in one query, as {dimension: {category: {choice: n}}}."""
    rows = db.execute(
        select(
            DemographicTally.dimension,
            DemographicTally.category,
            DemographicTally.choice,
            DemographicTally.count,
        ).where(DemographicTally.law_id == law_id)
    ).all()
    out: dict[str, dict[str, dict[str, int]]] = {}
    for dimension, category, choice, count in rows:
        by_category = out.setdefault(dimension, {})
        by_category.setdefault(category, {c.value: 0 for c in VoteChoice})[choice] = count
    return out


def all_breakdowns(db: Session, law_id: int, *, min_cell: int | None = None) -> list[Breakdown]:
    """Every dimension's breakdown — two queries in total, not two per dimension."""
    k = min_cell if min_cell is not None else get_settings().min_cell_count
    by_dimension = _cells_by_dimension(db, law_id)
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


def provision_rate(db: Session, law_id: int) -> ProvisionRate:
    """How many voters chose to share a profile — context for reading breakdowns.

    Derived from the existing ``demographics_recorded`` flag on votes; no extra
    storage, and one query rather than two.
    """
    total, with_profile = db.execute(
        select(
            func.count(),
            func.count().filter(Vote.demographics_recorded.is_(True)),
        ).where(Vote.law_id == law_id)
    ).one()
    total = total or 0
    with_profile = with_profile or 0
    pct = round(100 * with_profile / total, 1) if total else 0.0
    return ProvisionRate(total_votes=total, with_profile=with_profile, percentage=pct)
