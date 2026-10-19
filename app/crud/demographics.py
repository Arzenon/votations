"""Optional demographics: aggregate counters and suppressed breakdowns.

``record`` adds a voter's optional age/sex/region to per-law counters, once,
without storing anything per voter. ``all_breakdowns`` reads them back with
cell suppression, and from a snapshot until the vote is final (see
``refresh_snapshot`` and ``is_final``).

Nothing here commits: the route commits the vote and its counters together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Row, false, func, select, update
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
    STATUS_CLOSED,
    VALID_CATEGORIES,
    DemographicTally,
    InseeReference,
    Law,
    Vote,
    VoteChoice,
)


def _add(db: Session, law_id: int, dimension: str, category: str, choice: str, delta: int) -> None:
    """Atomically add ``delta`` to a counter (upsert). Never goes below zero.

    Callers take cells in (dimension, category, choice) order, the order the
    snapshot locks them in (see `_lock_cells`), so the two can't deadlock.
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

    for dimension, category in sorted(provided):
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

    for dimension, category in sorted(categories.items()):
        for moved, delta in sorted([(previous_choice, -1), (choice.value, +1)]):
            _add(db, vote.law_id, dimension, category, moved, delta)
    return categories


@dataclass
class BreakdownRow:
    category: str
    counts: dict[str, int]
    total: int
    percentages: dict[str, float]
    # Share among the table's newest rows, vs the national reference beside it.
    # None for a row older than the newest (see `_same_date_shares`).
    share: float | None = None
    reference: float | None = None
    # When these figures were copied, or None if read live (see `all_breakdowns`).
    as_of: datetime | None = None
    # Older than the step the page shows: held back by `_held`.
    held: bool = False


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
    # A REASON_* value. "pending" means figures may still come (the vote isn't
    # final, or its categories were never published), not "no data".
    reason: str = REASON_NONE

    @property
    def is_below_threshold(self) -> bool:
        return self.reason == REASON_BELOW_THRESHOLD

    @property
    def is_pending(self) -> bool:
        return self.reason == REASON_PENDING

    rows: list[BreakdownRow] = field(default_factory=list)
    reference_year: int | None = None  # INSEE source year if cached values are used
    # A category with votes is left out: never published, and held back since.
    withheld: bool = False


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
        out_rows.append(
            BreakdownRow(
                category=category,
                counts=counts,
                total=total,
                percentages=pct,
                reference=reference.get(category),
            )
        )

    return Breakdown(
        dimension,
        label,
        shown=True,
        threshold=k,
        rows=out_rows,
        reference_year=reference_year,
    )


# Minimum time between two snapshots. Mostly limits how often the profiled
# votes get counted; the volume rule is what protects voters.
_SNAPSHOT_INTERVAL = timedelta(hours=1)


def _snapshot_due(db: Session, law: Law, now: datetime) -> bool:
    last = law.demographics_snapshot_at
    if last is not None and now - as_utc(last) < _SNAPSHOT_INTERVAL:
        return False
    profiled = results.count_profiled(db, law.id)
    return profiled - law.demographics_snapshot_profiles >= get_settings().min_bucket_volume


def refresh_snapshot(db: Session, law: Law) -> bool:
    """Update the published breakdown if it's due. Returns True if it moved.

    Without a snapshot, comparing the public page before and after a single
    ballot would reveal that voter's choice along with their age, sex and
    region. So until the vote is final the page shows ``published_count``,
    which only moves, while the vote is open, when:

    * at least ``_SNAPSHOT_INTERVAL`` has passed since the last move, and
    * at least ``min_bucket_volume`` profiled votes have arrived since.

    Called during the ballot transaction, before the voter's own contribution
    is added. Does not commit.
    """
    if not law.is_open():
        return False

    now = utcnow()
    if not _snapshot_due(db, law, now):
        return False
    # Lock and re-check: a concurrent ballot's snapshot would otherwise wait on
    # this one's counters and publish a step of exactly this voter. Only taken
    # when due, so ordinary ballots never queue on the law row. NO KEY: each
    # vote insert holds a key share on the law (foreign key), FOR UPDATE would deadlock.
    db.refresh(law, with_for_update={"key_share": True})
    if not _snapshot_due(db, law, now):
        return False

    # Locked, so no ballot can change a counter between the check and the copy.
    _lock_cells(db, law.id)
    counters = _counters(db, law.id)
    # Counted after the locks, which waited for the ballots holding a counter:
    # the copy includes them, so the next step must count from them too.
    profiled = results.count_profiled(db, law.id)
    held = _held(counters)
    db.execute(
        update(DemographicTally)
        .where(
            DemographicTally.id.in_(
                [row.id for row in counters if (row.dimension, row.category) not in held]
            )
        )
        .values(published_count=DemographicTally.count, published_at=now)
    )
    law.demographics_snapshot_at = now
    law.demographics_snapshot_profiles = profiled
    return True


# Every counter a law can have, in `_add`'s order.
_ALL_CELLS = [
    (dimension, category, choice)
    for dimension in sorted(VALID_CATEGORIES)
    for category in sorted(VALID_CATEGORIES[dimension])
    for choice in sorted(c.value for c in VoteChoice)
]


def _lock_cells(db: Session, law_id: int) -> None:
    """Lock all of a law's counters, creating the missing ones, in `_add`'s order.

    One statement takes the cells one by one, so the snapshot never holds a
    cell while waiting for an earlier one. Creating a cell later, for this
    ballot's own profile, would wait on a ballot creating it too while
    holding the cell that ballot needs next.
    """
    stmt = pg_insert(DemographicTally).values(
        [
            {"law_id": law_id, "dimension": d, "category": c, "choice": ch, "count": 0}
            for d, c, ch in _ALL_CELLS
        ]
    )
    # DO UPDATE locks an existing row even when its WHERE is false.
    db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_demo_cell", set_={"count": DemographicTally.count}, where=false()
        )
    )


def _counters(db: Session, law_id: int) -> list[Row]:
    return list(
        db.execute(
            select(
                DemographicTally.id,
                DemographicTally.dimension,
                DemographicTally.category,
                DemographicTally.choice,
                # Not "count": that would hide the tuple's own count() method.
                DemographicTally.count.label("live"),
                DemographicTally.published_count,
                DemographicTally.published_at,
            ).where(DemographicTally.law_id == law_id)
        ).all()
    )


def _held(counters: list[Row]) -> set[tuple[str, str]]:
    """(dimension, category) with a cell that moved by fewer than ``min_cell_count``.

    A step of 50 profiles can still move one cell by 1 ("Corse, Contre"):
    anyone who knows a Corsican voted in that window would learn their vote.
    Such a category waits for a later step; the rest of its table moves on.
    Known limit: see PISTES.md, "Paliers homogènes".
    """
    k = get_settings().min_cell_count
    return {
        (row.dimension, row.category)
        for row in counters
        if 0 < abs(row.live - row.published_count) < k
    }


# After the closing, the figures stay at the last step this long: a ballot that
# passed its last "open" check before the closing can still be committing.
_CLOSING_GRACE = timedelta(minutes=2)


def is_final(law: Law) -> bool:
    """Whether the vote is over for good, so its breakdowns may go live.

    Not just "not open": a vote rescheduled to open later still takes ballots
    afterwards, and its live counters would be compared with the next step.
    """
    if law.status() != STATUS_CLOSED or law.closes_at is None:
        return False
    return utcnow() >= as_utc(law.closes_at) + _CLOSING_GRACE


def _live_categories(
    db: Session, law: Law, counters: list[Row], *, final: bool, profiled: int | None = None
) -> set[tuple[str, str]]:
    """(dimension, category) that may use the live counters instead of the snapshot.

    Closing the vote is one more step and follows the same rules: otherwise a
    single ballot cast after the last snapshot could be recovered by comparing
    the page before and after closing. If no snapshot was ever published there
    is nothing to compare against, so live counters are fine.

    ``final`` is `is_final`, read once per page. ``profiled`` is
    ``Tally.with_profile``, to save a query.
    """
    if not final:
        return set()
    every = {(row.dimension, row.category) for row in counters}
    if law.demographics_snapshot_at is None:
        return every
    if profiled is None:
        profiled = results.count_profiled(db, law.id)
    if profiled - law.demographics_snapshot_profiles < get_settings().min_bucket_volume:
        return set()
    return every - _held(counters)


def published_as_of(law: Law, breakdowns: list[Breakdown]) -> datetime | None:
    """Date of the step being shown, or None if every figure is live.

    Closing is dated by ``closes_at`` when it releases some categories only.
    A category held at an earlier step carries its own date (`BreakdownRow.as_of`).
    Shared by the results page and the JSON export so they say the same thing.
    """
    if not is_final(law):
        return law.demographics_snapshot_at
    live = [row.as_of is None for bd in breakdowns for row in bd.rows]
    live += [False for bd in breakdowns if bd.withheld]
    if all(live):
        return None
    return law.closes_at if any(live) else law.demographics_snapshot_at


_Cells = dict[str, dict[str, dict[str, int]]]


def _cells_by_dimension(counters: list[Row]) -> tuple[_Cells, _Cells]:
    """Counters as {dimension: {category: {choice: n}}}: live, then published."""
    counted: _Cells = {}
    published: _Cells = {}
    for row in counters:
        for out, n in ((counted, row.live), (published, row.published_count)):
            by_category = out.setdefault(row.dimension, {})
            by_category.setdefault(row.category, {c.value: 0 for c in VoteChoice})[row.choice] = n
    return counted, published


def all_breakdowns(db: Session, law: Law, *, profiled: int | None = None) -> list[Breakdown]:
    """Breakdowns for every dimension, each category from its live counters or its last step."""
    k = get_settings().min_cell_count
    final = is_final(law)
    if law.demographics_snapshot_at is None and not final:
        # Not over and no snapshot yet: everything is "pending", not "empty".
        return [
            Breakdown(
                dim, DIMENSION_LABELS.get(dim, dim), shown=False, threshold=k, reason=REASON_PENDING
            )
            for dim in DIMENSIONS
        ]

    counters = _counters(db, law.id)
    live = _live_categories(db, law, counters, final=final, profiled=profiled)
    counted, published = _cells_by_dimension(counters)
    # A step copies all of a category's cells at once, so they share a date.
    copied = {(r.dimension, r.category): r.published_at for r in counters if r.published_at}
    references = all_references(db)
    out = []
    for dimension in DIMENSIONS:
        reference, year = references.get(dimension, ({}, None))
        cells = {
            category: (counted if (dimension, category) in live else published)[dimension][category]
            for category in counted.get(dimension, {})
        }
        breakdown = _build(dimension, cells, k=k, reference=reference, reference_year=year)
        for shown in breakdown.rows:
            key = (dimension, shown.category)
            shown.as_of = None if key in live else copied.get(key)
        breakdown.withheld = any(
            (dimension, category) not in live
            and sum(cell.values()) > 0
            and not sum(published[dimension][category].values())
            for category, cell in counted.get(dimension, {}).items()
        )
        has_votes = any(n for cell in counted.get(dimension, {}).values() for n in cell.values())
        # Before the end, "empty" would flip to "pending" with the first voter to
        # fill this field in, and tell which field they filled in.
        if breakdown.reason == REASON_EMPTY and (not final or has_votes):
            breakdown.reason = REASON_PENDING
        out.append(breakdown)

    as_of = published_as_of(law, out)
    for breakdown in out:
        for shown in breakdown.rows:
            shown.held = as_of is not None and shown.as_of is not None and shown.as_of < as_of
        _same_date_shares(breakdown)
    return out


def _same_date_shares(breakdown: Breakdown) -> None:
    """Keep shares, and the national reference beside them, to the same categories.

    Shares are among the table's newest rows: a row from an older step against
    newer ones would be neither date's share. Live rows count as the newest.
    The reference is rescaled to those categories too, so one never published
    or never picked doesn't skew the comparison. It only uses what the table
    shows, which moves at steps, never with a single ballot.
    """
    rows = breakdown.rows
    if not rows:
        return
    dates = [shown.as_of for shown in rows]
    newest = None if None in dates else max(d for d in dates if d is not None)
    current = [shown for shown in rows if shown.as_of == newest]
    # Shown rows always have votes, so the total isn't zero.
    total = sum(shown.total for shown in current)
    # One category alone would read 100%, which compares nothing.
    known = [shown.reference for shown in current if shown.reference is not None]
    # A category with no national figure ("autre") still counts among our voters:
    # scale to the share of voters the others cover, so both compare the same people.
    covered = sum(shown.total for shown in current if shown.reference is not None) / total
    for shown in rows:
        if shown.as_of != newest:
            shown.share = shown.reference = None
            continue
        shown.share = round(100 * shown.total / total, 1)
        if shown.reference is None or len(known) < 2:
            shown.reference = None
        else:
            shown.reference = round(100 * covered * shown.reference / sum(known), 1)


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
