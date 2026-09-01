"""Aggregating votes into the deliberately simple, manipulation-resistant stats.

Only three numbers are ever surfaced publicly: % for / % against / % abstain and
the total voter count. The V1 evolution curve buckets votes over time, gated by a
minimum volume to prevent re-identification.

Several functions here need the same number — how many votes this law has — to
decide what may be published. They all take it as an optional argument so a page
that already knows it counts once instead of four times.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, literal, select
from sqlalchemy.orm import Session

from app.config import display_zone, get_settings
from app.db import as_utc
from app.models import Vote, VoteChoice

# How the export rounds `created_at`, per `export_time_resolution`.
_EXPORT_TRUNC = {"day": "day", "hour": "hour"}


def _bucketed(unit: str, zone: str):
    """``Vote.created_at`` rounded down to ``unit`` **in the display zone**.

    A day has to be the day a voter would say they voted on. Truncating in UTC
    filed every vote cast between midnight and 2 a.m. Paris under the previous
    day — visibly wrong on a curve headed « jour par jour », and wrong in the
    export too, which must round on exactly the same boundaries as the curve or
    it would partition the votes a second, different way.

    Round trip through the zone rather than stopping at the local timestamp: the
    result stays a real instant, so the caller can format it in any zone and DST
    is handled by the database rather than by arithmetic here.
    """
    local = func.timezone(zone, Vote.created_at)  # timestamptz → local timestamp
    return func.timezone(zone, func.date_trunc(unit, local))  # → timestamptz


@dataclass(frozen=True)
class Tally:
    """A law's headline figures. Frozen because it is shared.

    :data:`_TALLIES` hands the same instance to every reader inside its window,
    so a caller that mutated one would be rewriting a number for everybody. The
    two dicts inside it are still mutable — Python offers no cheap way not to be
    — and are likewise nobody's to change.
    """

    total: int
    counts: dict[str, int]
    percentages: dict[str, float]
    #: How many of these votes came with an optional profile. Counted here rather
    #: than by a query of its own because it is the same scan: the results page
    #: needs both numbers, and asking twice meant walking every vote on the law
    #: twice to answer one question about them. Holds a count of votes, never a
    #: category — the profile itself is still stored nowhere.
    with_profile: int = 0


#: One pass over a law's votes answers both questions the pages ask of them.
_COUNT_COLUMNS = (
    func.count(),
    func.count().filter(Vote.demographics_recorded.is_(True)),
)


# --- A short-lived memo of the figures a whole page is made of ---------------
#
# Every aggregate here is a scan of one law's votes. That is one query, which is
# what the suite checks — and an *unbounded* one, which is what it cannot. The
# home page asks for the participation of every law it lists, up to fifty per
# band across three bands: one query, and every vote ever cast on a hundred and
# fifty votations walked to draw a hundred and fifty cards. Nothing in the
# shipped stack caches on the application's behalf (Caddy does not, by default),
# so the busiest page of the site paid that per visitor.
#
# **The window is never longer than what the page already promises its reader.**
# `Cache-Control: public, max-age=60` on the home page is a statement that the
# figures on it may be a minute old; serving them from here inside that same
# minute tells nobody anything new. That is the whole justification, and it is
# why the durations live beside the headers in `app.routes.public.common` rather
# than here — the two must be the same number or this stops being honest.
#
# Which is also why the results page of an **open** votation is deliberately not
# memoised, even though it declares thirty seconds. That header is aimed at one
# reader's own browser, and its thirty seconds were chosen so that somebody who
# has just voted still sees their ballot; a memo shared by everyone would take
# that away from them. A closed votation cannot move at all, so there it is free.
#
# A ballot drops the law it was cast on (see `forget`), so under the single
# container the compose file runs these figures are not merely fresh-ish but
# exact, and a busy site re-counts one law rather than a hundred and fifty. The
# window is the backstop for everything that is not a ballot — an archived law,
# an admin edit, a second replica — not the mechanism.


class _Memo[T]:
    """Process-local, per-law, time-bounded. Not a cache in front of the truth.

    Small on purpose: no eviction policy worth the name, just an expiry check and
    a ceiling past which everything goes. It holds a handful of integers per law
    and the corpus is hundreds of rows, so the ceiling exists to bound a mistake
    rather than to manage a working set.

    **What it hands back is shared, and therefore read-only.** Every reader
    inside the window gets the same object, so mutating one would rewrite a
    figure for everybody. :class:`Tally` is frozen to say so; the vote curve is a
    plain list, and the only thing that consumes it — `app.charts` — copies each
    bucket's counts before touching them.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._entries: dict[int, tuple[float, T]] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, law_id: int, max_age: float) -> T | None:
        """The remembered value, if it is younger than ``max_age``."""
        if max_age <= 0:
            return None
        with self._lock:
            found = self._entries.get(law_id)
        if found is None:
            return None
        stored_at, value = found
        return value if time.monotonic() - stored_at <= max_age else None

    def put(self, law_id: int, value: T) -> None:
        with self._lock:
            if len(self._entries) >= self._max_entries and law_id not in self._entries:
                self._entries.clear()
            self._entries[law_id] = (time.monotonic(), value)

    def forget(self, law_id: int) -> None:
        with self._lock:
            self._entries.pop(law_id, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_TALLIES: _Memo[Tally] = _Memo()
_EVOLUTIONS: _Memo[list[EvolutionBucket]] = _Memo()


def forget(law_id: int) -> None:
    """Drop what is remembered about one law, because it has just changed.

    Called by :func:`app.crud.votes.cast_vote`. It runs before the ballot's
    transaction commits, so a reader arriving in the microseconds between the two
    can still store the previous figures — and then serve them for the length of
    the window, which is exactly the staleness the page already declares. The
    window is what bounds this; the call is what makes it rare.
    """
    _TALLIES.forget(law_id)
    _EVOLUTIONS.forget(law_id)


def reset_cache() -> None:
    """Forget everything (tests, and a deliberate operational reset)."""
    _TALLIES.clear()
    _EVOLUTIONS.clear()


def _tally_from_counts(counts: dict[str, int], with_profile: int = 0) -> Tally:
    total = sum(counts.values())
    pct = {k: (round(100 * v / total, 1) if total else 0.0) for k, v in counts.items()}
    return Tally(total=total, counts=counts, percentages=pct, with_profile=with_profile)


def tally(db: Session, law_id: int, *, max_age: float = 0.0) -> Tally:
    """This law's headline figures.

    ``max_age`` in seconds lets a caller accept a figure it has already told its
    reader may be that old; left at zero the count is taken now. See the note at
    :class:`_Memo` for why only some callers pass it.
    """
    remembered = _TALLIES.get(law_id, max_age)
    if remembered is not None:
        return remembered
    rows = db.execute(
        select(Vote.choice, *_COUNT_COLUMNS).where(Vote.law_id == law_id).group_by(Vote.choice)
    ).all()
    counts = {c.value: 0 for c in VoteChoice}
    profiled = 0
    for choice, n, with_profile in rows:
        counts[choice] = n
        profiled += with_profile
    result = _tally_from_counts(counts, profiled)
    _TALLIES.put(law_id, result)
    return result


def tallies_for(db: Session, law_ids: list[int], *, max_age: float = 0.0) -> dict[int, Tally]:
    """Tallies for many laws — the home page and the admin listings.

    One query for the laws that have to be counted, and none at all when every
    one of them is already remembered. That second half is what makes the memo
    worth having on the home page: a ballot forgets the single law it was cast
    on, so a busy site re-counts *that* law and reads the other hundred and
    forty-nine from memory, rather than either walking all of them or serving all
    of them stale until the window turns over.
    """
    if not law_ids:
        return {}

    out: dict[int, Tally] = {}
    wanted: list[int] = []
    for law_id in law_ids:
        remembered = _TALLIES.get(law_id, max_age)
        if remembered is None:
            wanted.append(law_id)
        else:
            out[law_id] = remembered
    if not wanted:
        return out

    rows = db.execute(
        select(Vote.law_id, Vote.choice, *_COUNT_COLUMNS)
        .where(Vote.law_id.in_(wanted))
        .group_by(Vote.law_id, Vote.choice)
    ).all()
    counts: dict[int, dict[str, int]] = {
        law_id: {c.value: 0 for c in VoteChoice} for law_id in wanted
    }
    profiled: dict[int, int] = dict.fromkeys(wanted, 0)
    for law_id, choice, n, with_profile in rows:
        counts[law_id][choice] = n
        profiled[law_id] += with_profile
    for law_id, c in counts.items():
        counted = _tally_from_counts(c, profiled[law_id])
        _TALLIES.put(law_id, counted)
        out[law_id] = counted
    return out


def count_votes(db: Session, law_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Vote).where(Vote.law_id == law_id)) or 0


def count_profiled(db: Session, law_id: int) -> int:
    """How many of this law's votes came with an optional profile.

    The same number :class:`Tally` carries as ``with_profile``, asked on its own
    because the one caller that needs it here is the ballot path — see
    :func:`app.crud.demographics.refresh_snapshot`, which uses it to decide
    whether the published breakdown may move — and that path has no use for the
    per-choice grouping ``tally`` does on the way.

    Answered from ``ix_votes_law_profiled``, the partial index that holds the
    profiled votes and nothing else, so this costs what it counts rather than
    what the law has received. ``ix_votes_law_choice`` could answer it too, from
    its included column — by walking every vote on the law, which on this path is
    every ballot's worth of work.

    **The bare column, not ``.is_(True)``**, exactly as in
    :func:`app.crud.laws._published` and for exactly the same reason: Postgres
    applies a partial index only where it can prove the WHERE implies the index
    predicate, and it cannot prove that of ``demographics_recorded IS true``. The
    two spellings return the same rows, which is what makes tidying this one back
    a change that costs a full scan and shows nothing.
    """
    return (
        db.scalar(
            select(func.count())
            .select_from(Vote)
            .where(Vote.law_id == law_id, Vote.demographics_recorded)
        )
        or 0
    )


@dataclass
class EvolutionBucket:
    #: The calendar day, ISO. Machine-facing on purpose: `app.charts` parses it
    #: back to lay out the axis, and it is the only form that survives being
    #: sorted, compared and grouped. **Always ``date.isoformat()``** — nothing
    #: else may be put here, which is what lets :attr:`day` be strict.
    label: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def day(self) -> date:
        """The same day, as a date the page can render in French.

        The results page shows these buckets in a table, and it was printing
        ``label`` straight — three date formats within one section, one of them
        ISO, on a site that routes every other date through `app.labels` so that
        no machine-readable string reaches a French page.
        """
        return date.fromisoformat(self.label)


def evolution(
    db: Session, law_id: int, *, total: int | None = None, max_age: float = 0.0
) -> list[EvolutionBucket]:
    """Daily vote counts over time (V1), one bucket per calendar day.

    Returns an empty list until the total reaches ``min_bucket_volume`` so that
    low-volume votations cannot be used to re-identify or fingerprint voters.

    Bucketing happens in the database: reading every vote row back into Python to
    group it was the single most expensive query in the application.

    **Days with no votes are filled in with zeros**, because the chart spaces its
    points by position and not by date: a day nobody voted used to be deleted
    rather than drawn flat, so thirty quiet days and thirty busy ones produced the
    same picture. That is the one reading the page asks of this curve — « une
    mobilisation organisée laisse un pic sur une journée » — and dropping the
    quiet days is precisely what flattens the pic back out.

    Nothing is revealed by the filling: a day absent from the old list already
    meant "no votes", and the range still stops at the first and last day anyone
    actually voted.

    ``max_age`` works exactly as it does on :func:`tally`, and is passed by the
    same caller for the same reason: this is the results page's *second* scan of
    the same rows, and on a closed votation neither of them can produce a
    different answer than it did a moment ago.
    """
    remembered = _EVOLUTIONS.get(law_id, max_age)
    if remembered is not None:
        return remembered

    settings = get_settings()
    if total is None:
        total = count_votes(db, law_id)
    if total < settings.min_bucket_volume:
        # Remembered too: below the gate this is the answer until the gate is
        # cleared, and clearing it means a ballot, which forgets the law.
        _EVOLUTIONS.put(law_id, [])
        return []

    zone = display_zone()
    bucket = _bucketed("day", settings.display_timezone)
    rows = db.execute(
        select(bucket.label("bucket"), Vote.choice, func.count())
        .where(Vote.law_id == law_id)
        .group_by("bucket", Vote.choice)
        .order_by("bucket")
    ).all()

    out: dict[date, dict[str, int]] = {}
    for moment, choice, n in rows:
        day = as_utc(moment).astimezone(zone).date()
        out.setdefault(day, {c.value: 0 for c in VoteChoice})[choice] = n

    # Walk calendar days rather than adding 24 hours: a DST boundary makes those
    # two different things, and the day is what the label and the export agree on.
    curve: list[EvolutionBucket] = []
    if out:
        curve = [
            EvolutionBucket(
                label=day.isoformat(),
                counts=out.get(day) or {c.value: 0 for c in VoteChoice},
            )
            for day in _days_between(min(out), max(out))
        ]
    _EVOLUTIONS.put(law_id, curve)
    return curve


def _days_between(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def export_note(db: Session, law_id: int, *, total: int | None = None) -> str:
    """What was withheld from this law's export, in the words shown to readers."""
    settings = get_settings()
    if not _timestamps_allowed(db, law_id, total=total):
        return (
            "L'horodatage est omis : cette votation compte moins de "
            f"{settings.min_bucket_volume} votes, seuil en dessous duquel la "
            "répartition dans le temps n'est pas publiée - la publier ici "
            "reviendrait à contourner ce seuil."
        )
    if settings.export_time_resolution == "exact":
        return "L'horodatage est publié à la seconde près."
    # The rounding resolution is no longer spelled out here. It is still the same
    # one the published evolution uses — that is what makes the sentence true —
    # but naming it added a clause readers had to decode to reach the point.
    return "L'export ne révèle rien de plus que la page de résultats."


def _timestamps_allowed(db: Session, law_id: int, *, total: int | None = None) -> bool:
    """Whether this law has enough votes for a time column to be publishable.

    The evolution curve is withheld below ``min_bucket_volume``. An export
    carrying per-vote times would hand back exactly what that gate withholds — at
    any resolution the reader chooses — so the same gate applies here.
    """
    if total is None:
        total = count_votes(db, law_id)
    return total >= get_settings().min_bucket_volume


def iter_export_rows(
    db: Session, law_id: int, *, chunk: int = 1000, total: int | None = None
) -> Iterator[dict]:
    """Raw anonymised rows, streamed.

    Contains ONLY law_id, choice and created_at. No voter token, no identifier.

    Two reductions, so that the export never reveals more than the results page:

    * below ``min_bucket_volume`` the time column is empty, because that is
      precisely where the evolution curve is withheld;
    * above it, times are truncated to ``export_time_resolution`` — the same
      resolution the published curve uses.

    Rows come out ordered by (time, choice) and by nothing else. Sorting on the
    two published columns means the order carries no information the file does
    not already state — in particular not the sequence votes arrived in, which an
    id or timestamp ordering would have leaked. A shuffle would not do: any
    reproducible one can be replayed to recover the original order.
    """
    settings = get_settings()
    trunc = _EXPORT_TRUNC.get(settings.export_time_resolution)
    with_time = _timestamps_allowed(db, law_id, total=total)
    zone = display_zone()

    # Three different expressions land here — a literal NULL, a truncation, or
    # the raw column — so the variable is annotated as what they have in common.
    moment: ColumnElement[Any]
    if not with_time:
        moment = literal(None)
    elif trunc:
        moment = _bucketed(trunc, settings.display_timezone)
    else:
        moment = Vote.created_at.expression

    result = db.execute(
        select(moment.label("moment"), Vote.choice)
        .where(Vote.law_id == law_id)
        .order_by("moment", Vote.choice)
    ).yield_per(chunk)

    for created, choice in result:
        yield {
            "law_id": law_id,
            "choice": choice,
            "created_at": _iso(created, trunc, zone) if with_time else "",
        }


def _iso(created: datetime | None, trunc: str | None, zone: ZoneInfo) -> str:
    """One rounded instant, written in the zone the results page uses.

    The day form is a bare calendar date on purpose: it is the same day the curve
    labels, and an offset on it would only invite the reader to convert it back
    into some other day.
    """
    if created is None:
        return ""
    created = as_utc(created).astimezone(zone)
    if trunc == "day":
        return created.date().isoformat()
    if trunc == "hour":
        return created.strftime("%Y-%m-%dT%H:00:00%z")
    return created.isoformat()
