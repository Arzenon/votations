"""Recording optional demographics into aggregate counters, and reading them
back with cell suppression (k-anonymity).

Two responsibilities:

1. ``record`` — fold a voter's optional age/sexe/région into the counters,
   **once**, with no per-voter row and no token. Concurrency-safe: each cell moves
   by an upsert, and the vote row is locked by :func:`app.crud.votes.cast_vote`
   for the whole ballot, so two simultaneous ballots from one browser cannot both
   believe they are the first.
2. ``all_breakdowns`` — build a demographic × choice table for display/export,
   applying whole-table suppression so small cells can never be read or
   differenced out, and — while the vote is open — reading a *snapshot* of the
   counters rather than the counters themselves. ``refresh_snapshot`` is what
   moves that snapshot forward; see it for what the two together defend against.

Nothing here commits. The ballot is one transaction, opened by ``cast_vote`` and
closed by the route: a vote and the counters it moves are written together or
neither is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

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


def _known(categories: dict[str, str] | None) -> dict[str, str]:
    """Keep only the (dimension, category) pairs this application recognises."""
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
    """Fold this voter's optional demographics into the counters, exactly once.

    Invalid or empty values are ignored. A voter contributes their profile a
    single time; if they later change their vote, the contribution is *moved* to
    the new choice rather than left counted under the old one, so a breakdown
    always describes the votes as they currently stand.

    Moving it needs two things the vote row deliberately does not hold:

    * **which categories** — they come back from the voter's own browser as
      ``previous_categories`` (see :mod:`app.profile_cookie`);
    * **under which choice they were filed** — that is ``previous_choice``, the
      value the vote held a moment ago, read under the lock ``cast_vote`` takes.
      It used to be a column on the vote, which meant a row could sit there
      stating both the choice somebody had made *and* the one they made before —
      a record of a changed mind that nothing on this platform needs.

    Returns the categories now on file for this voter, for the caller to persist
    on the device. An empty mapping means there is nothing to remember.
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
    """Re-file an existing contribution under a changed vote."""
    if previous_choice is None or previous_choice == choice.value:
        # The ballot did not change the choice, so the contribution is already
        # filed where it belongs.
        return categories

    if not categories:
        # The device no longer remembers what it gave, so nothing can be moved:
        # the contribution stays under the earlier choice. It stays there for
        # good, and that is what makes it safe to keep no record of where it
        # sits. The ballot stops offering the profile fields once a contribution
        # exists, and the cookie is only ever written back from categories it
        # already held — so a browser that has forgotten a law can never learn it
        # again, and no later change can decrement a cell chosen by guesswork.
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
    # Representativeness: this category's share of participants who gave a profile
    # for this dimension, vs the approximate national reference share.
    share: float = 0.0
    reference: float | None = None


#: Why a breakdown is withheld. Named, because the results page branches on all
#: three and was doing it by writing the strings out again — a copy of a value
#: whose only definition was here, that no rename would have followed and whose
#: drifting would have shown up as the page quietly taking the last branch.
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
    #: One of the REASON_* values above.
    #:
    #: « pending » is the one that is not about the data: the votation is open and
    #: has not yet reached its first published snapshot, so there may well be
    #: figures — they are simply not publishable yet. Telling it apart from
    #: « empty » matters, because the page would otherwise say « aucune donnée
    #: renseignée » about a law people have been filling the fields in on.
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
        return Breakdown(dimension, label, shown=False, threshold=k, reason=REASON_EMPTY)
    if min(populated) < k:
        return Breakdown(dimension, label, shown=False, threshold=k, reason=REASON_BELOW_THRESHOLD)

    dimension_total = sum(populated)
    # Categories that hold nothing are dropped, not drawn as a row of zeros.
    #
    # A cell row is created the moment somebody votes in that category, and its
    # `published_count` starts at zero — so a category nobody had used before
    # appeared on the page as « 65+ · 0 · 0 · 0 » the instant one person chose it.
    # That is the leak the snapshot exists to close, wearing a different hat: the
    # *arrival* of a row says as much as a number moving in it. Filtering on the
    # published total closes both, and a row of zeros was never worth drawing.
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


#: The shortest interval between two moves of a law's published breakdown.
#:
#: Not the guarantee — that is the volume rule below, and this is what keeps its
#: cost down: the check that enforces it counts rows, and this is what stops that
#: count happening on every single ballot. An hour is short enough that a busy
#: votation's figures stay recognisably current and long enough that the count
#: runs once per law per hour whatever the traffic.
_SNAPSHOT_INTERVAL = timedelta(hours=1)


def refresh_snapshot(db: Session, law: Law) -> bool:
    """Move this law's published breakdown forward if it is due. True if it moved.

    **What this exists to prevent.** The counters this module keeps are marginal
    and never joint, and :func:`_build` withholds a whole dimension unless every
    populated cell clears ``MIN_CELL_COUNT``. Both of those protect one reading of
    the results page. A *sequence* of readings defeats them completely: the page
    is public and unthrottled, so two fetches thirty seconds apart could be
    subtracted, and the difference was a single ballot — naming that voter's
    choice together with their age bracket, their sex and their région, which is
    precisely the joint cell the schema refuses to store. No knowledge of when
    anybody voted was needed; polling in a loop enumerated them one at a time.

    **The rule.** What the page publishes while the vote is open is
    ``published_count``, and it may only move when both hold:

    * at least :data:`_SNAPSHOT_INTERVAL` has passed since it last moved, and
    * at least ``MIN_BUCKET_VOLUME`` **profiled** votes have arrived since then.

    The second is the one that does the work, and it counts profiled votes rather
    than votes on purpose. A window of fifty ballots containing a single filled-in
    profile would otherwise hand that one person's categories over intact — the
    step has to be large in the quantity it actually discloses.

    A closed votation is left alone: its counters have stopped moving, so there is
    no sequence to difference and :func:`all_breakdowns` publishes them live.

    Called from the ballot transaction, before this voter's own contribution is
    folded in, so nobody can trigger a step and read themselves in it. Commits
    nothing — the caller owns the transaction, as everywhere else here.
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


def _cells_by_dimension(
    db: Session, law_id: int, *, live: bool
) -> dict[str, dict[str, dict[str, int]]]:
    """Every counter for a law, in one query, as {dimension: {category: {choice: n}}}.

    ``live`` picks the column: the counters themselves once the vote has closed
    and nothing can move any more, the published snapshot while it is open. See
    :func:`refresh_snapshot`.
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


def all_breakdowns(db: Session, law: Law, *, min_cell: int | None = None) -> list[Breakdown]:
    """Every dimension's breakdown — two queries in total, not two per dimension.

    Takes the law rather than its id because *which* numbers may be published is
    a fact about the votation, not about the counters: an open one publishes the
    snapshot, a closed one the live totals. See :func:`refresh_snapshot`.
    """
    k = min_cell if min_cell is not None else get_settings().min_cell_count
    live = not law.is_open()
    if not live and law.demographics_snapshot_at is None:
        # Open, and no snapshot taken yet. Every dimension is withheld — but as
        # "not published yet", never as "nothing to show": people may well have
        # been filling the optional fields in all along.
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
    """How many voters chose to share a profile — context for reading breakdowns.

    Derived from the existing ``demographics_recorded`` flag on votes; no extra
    storage, and no per-voter profile anywhere.

    ``tally`` is the same optional-argument idiom the rest of the aggregation
    uses: both numbers here come out of the scan :func:`~app.crud.results.tally`
    already performs, so a caller that has one hands it over instead of walking
    every vote on the law a second time to answer a question about the same rows.
    """
    if tally is None:
        tally = results.tally(db, law_id)
    pct = round(100 * tally.with_profile / tally.total, 1) if tally.total else 0.0
    return ProvisionRate(total_votes=tally.total, with_profile=tally.with_profile, percentage=pct)
