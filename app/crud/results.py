"""Vote aggregates: percentages, totals and the evolution curve.

The curve and the export's timestamps are withheld below a minimum volume to
prevent re-identification. Functions that need the vote count accept it as an
optional argument so a page can count once.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, literal, select
from sqlalchemy.orm import Session

from app.config import display_zone, get_settings
from app.db import as_utc
from app.models import Vote, VoteChoice


def _bucketed(unit: str, zone: str):
    """``Vote.created_at`` truncated to ``unit`` in the display zone.

    Truncating in UTC put votes cast between midnight and 2am Paris time on the
    previous day. The curve and the export must use the same boundaries.
    """
    local = func.timezone(zone, Vote.created_at)  # timestamptz to local timestamp
    return func.timezone(zone, func.date_trunc(unit, local))  # and back


@dataclass(frozen=True)
class Tally:
    """A law's headline figures. Frozen because the memo shares instances."""

    total: int
    counts: dict[str, int]
    percentages: dict[str, float]
    # Votes that came with an optional profile, counted in the same scan.
    with_profile: int = 0


_COUNT_COLUMNS = (
    func.count(),
    func.count().filter(Vote.demographics_recorded.is_(True)),
)


# Short-lived memo of per-law aggregates. The home page lists up to 150 laws
# and would otherwise scan all their votes on every visit.
#
# The window never exceeds the page's own Cache-Control max-age, which is why
# the durations live next to the headers in `app.routes.public.common`. A
# ballot calls `forget` for its law, before and after its commit, so a voter
# sees their own ballot right away and the window only matters for other
# changes (admin edits, a second replica).


class _Memo[T]:
    """Process-local, per-law memo with an expiry.

    Returned values are shared between readers and must not be mutated.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._entries: dict[int, tuple[float, T]] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, law_id: int, max_age: float) -> T | None:
        """The stored value, if younger than ``max_age``."""
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
    """Drop the memoised figures for a law that just changed."""
    _TALLIES.forget(law_id)
    _EVOLUTIONS.forget(law_id)


def reset_cache() -> None:
    """Forget everything (tests, manual reset)."""
    _TALLIES.clear()
    _EVOLUTIONS.clear()


def _tally_from_counts(counts: dict[str, int], with_profile: int = 0) -> Tally:
    total = sum(counts.values())
    pct = {k: (round(100 * v / total, 1) if total else 0.0) for k, v in counts.items()}
    return Tally(total=total, counts=counts, percentages=pct, with_profile=with_profile)


def empty_tally() -> Tally:
    """A zero tally, for the admin preview of an unsaved law (whose id is None)."""
    return _tally_from_counts({c.value: 0 for c in VoteChoice})


def tally(db: Session, law_id: int, *, max_age: float = 0.0) -> Tally:
    """This law's headline figures.

    ``max_age`` (seconds) accepts a memoised value that old. Zero counts now.
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
    """Tallies for many laws, in one query for those not already memoised."""
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

    Uses the partial index ``ix_votes_law_profiled``. Keep the bare column in
    the WHERE: Postgres won't use the partial index with ``.is_(True)``.
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
    # ISO date of the first day (`date.isoformat()`), parsed back by `app.charts`.
    label: str
    counts: dict[str, int]
    # How many voted on each day covered, not how: only the split is merged.
    daily: list[int]

    @property
    def days(self) -> int:
        """Calendar days covered: more than one when quiet days are merged."""
        return len(self.daily)

    @property
    def day(self) -> date:
        """The label as a date, for French formatting in templates."""
        return date.fromisoformat(self.label)

    @property
    def last_day(self) -> date:
        return self.day + timedelta(days=self.days - 1)


def _merge_quiet[T](
    units: list[tuple[T, dict[str, int]]], k: int
) -> list[tuple[T, T, dict[str, int]]]:
    """Group consecutive units until each group has ``k`` votes over two choices.

    A day showing one "Contre" tells anyone who knows that person voted that
    day how they voted, and so does a day of ten "Pour". Hiding the split
    alone isn't enough: the tally minus the other days gives it back.
    Leftover units join the last group, so the groups always add up to the
    tally. Returns (first, last, counts).
    """
    groups: list[tuple[T, T, dict[str, int]]] = []
    pending: tuple[T, dict[str, int]] | None = None
    for unit, counts in units:
        first, summed = pending if pending else (unit, dict.fromkeys(counts, 0))
        summed = {c: summed[c] + n for c, n in counts.items()}
        if sum(summed.values()) >= k and sum(1 for n in summed.values() if n) >= 2:
            groups.append((first, unit, summed))
            pending = None
        else:
            pending = (first, summed)
    if pending:
        first, summed = pending
        if groups:
            first, _, before = groups.pop()
            summed = {c: before[c] + summed[c] for c in summed}
        groups.append((first, units[-1][0], summed))
    return groups


def _units(db: Session, law_id: int, unit: str) -> list[tuple[datetime, dict[str, int]]]:
    """Vote counts per ``unit`` ("day" or "hour") in the display zone, oldest first."""
    bucket = _bucketed(unit, get_settings().display_timezone)
    rows = db.execute(
        select(bucket.label("bucket"), Vote.choice, func.count())
        .where(Vote.law_id == law_id)
        .group_by("bucket", Vote.choice)
        .order_by("bucket")
    ).all()
    found: dict[datetime, dict[str, int]] = {}
    for moment, choice, n in rows:
        found.setdefault(moment, {c.value: 0 for c in VoteChoice})[choice] = n
    return list(found.items())


def _periods(
    db: Session, law_id: int, unit: str
) -> list[tuple[datetime, datetime, dict[str, int]]]:
    """Vote counts per ``unit`` ("day" or "hour"), quiet ones merged (see `_merge_quiet`).

    Hours are merged inside each merged day, so they add up to the curve's
    days: otherwise a day minus its hour groups could give back a late vote
    whose hour group runs past midnight.
    """
    units = _units(db, law_id, unit)
    k = get_settings().min_cell_count
    if unit == "day":
        return _merge_quiet(units, k)

    # The same days as the curve, rebuilt from the hours.
    zone = display_zone()
    by_day: dict[date, dict[str, int]] = {}
    for moment, counts in units:
        day = by_day.setdefault(as_utc(moment).astimezone(zone).date(), dict.fromkeys(counts, 0))
        for choice, n in counts.items():
            day[choice] += n
    periods = []
    end = 0
    for _, last_day, _ in _merge_quiet(list(by_day.items()), k):
        start = end
        while end < len(units) and as_utc(units[end][0]).astimezone(zone).date() <= last_day:
            end += 1
        periods += _merge_quiet(units[start:end], k)
    return periods


def evolution(
    db: Session, law_id: int, *, total: int | None = None, max_age: float = 0.0
) -> list[EvolutionBucket]:
    """Vote counts over time: one bucket per calendar day, or per merged period.

    Empty until the total reaches ``min_bucket_volume``, so low-volume votes
    can't be used to identify voters. The split between choices is merged
    over neighbouring days (see `_merge_quiet`); each day keeps its own number
    of votes, so a campaign's spike still shows inside a merged period.

    Days without votes are filled with zeros, otherwise a spike from an
    organised campaign wouldn't stand out on the chart.
    """
    remembered = _EVOLUTIONS.get(law_id, max_age)
    if remembered is not None:
        return remembered

    settings = get_settings()
    if total is None:
        total = count_votes(db, law_id)
    if total < settings.min_bucket_volume:
        # Memoised too: crossing the threshold takes a ballot, which forgets it.
        _EVOLUTIONS.put(law_id, [])
        return []

    zone = display_zone()
    units = _units(db, law_id, "day")
    voted = {as_utc(moment).astimezone(zone).date(): sum(c.values()) for moment, c in units}
    curve: list[EvolutionBucket] = []
    for first, last, counts in _merge_quiet(units, settings.min_cell_count):
        start, end = (as_utc(moment).astimezone(zone).date() for moment in (first, last))
        if curve:
            # Step by calendar day, not 24 hours, because of DST.
            curve += [
                EvolutionBucket(
                    label=day.isoformat(), counts={c.value: 0 for c in VoteChoice}, daily=[0]
                )
                for day in _days_between(
                    curve[-1].last_day + timedelta(days=1), start - timedelta(days=1)
                )
            ]
        curve.append(
            EvolutionBucket(
                label=start.isoformat(),
                counts=counts,
                daily=[voted.get(day, 0) for day in _days_between(start, end)],
            )
        )
    _EVOLUTIONS.put(law_id, curve)
    return curve


def _days_between(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def export_note(db: Session, law_id: int, *, total: int | None = None) -> str:
    """Explains to readers what was left out of this law's export."""
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
    k = settings.min_cell_count
    if settings.export_time_resolution == "day":
        rounded = (
            f"de son jour, mais les jours sont regroupés avec leurs voisins jusqu'à "
            f"compter {k} votes partagés entre au moins deux choix, comme sur la courbe"
        )
    else:
        rounded = (
            f"de son heure, mais les heures sont regroupées avec leurs voisines jusqu'à "
            f"compter {k} votes partagés entre au moins deux choix, sans sortir des "
            "jours regroupés de la courbe"
        )
    return (
        f"Chaque vote est daté {rounded} : 'created_at' est alors le début de la période regroupée."
    )


def _timestamps_allowed(db: Session, law_id: int, *, total: int | None = None) -> bool:
    """Whether the export may include timestamps (same threshold as the curve)."""
    if total is None:
        total = count_votes(db, law_id)
    return total >= get_settings().min_bucket_volume


def iter_export_rows(
    db: Session, law_id: int, *, chunk: int = 1000, total: int | None = None
) -> Iterator[dict]:
    """Stream anonymised rows: law_id, choice and created_at only.

    Below ``min_bucket_volume`` created_at is empty. Above it, it is truncated
    to ``export_time_resolution`` with quiet periods merged, like the published
    curve: created_at is then the period's start.

    Rows are sorted by (time, choice) so their order doesn't leak the sequence
    in which votes arrived.
    """
    resolution = get_settings().export_time_resolution
    with_time = _timestamps_allowed(db, law_id, total=total)
    zone = display_zone()

    if with_time and resolution != "exact":
        # Rounded rows are identical within a period: rebuilt from its counts.
        for first, _, counts in _periods(db, law_id, resolution):
            created = _iso(first, resolution, zone)
            for choice in sorted(counts):
                row = {"law_id": law_id, "choice": choice, "created_at": created}
                for _ in range(counts[choice]):
                    yield row
        return

    moment: ColumnElement[Any] = Vote.created_at.expression if with_time else literal(None)

    result = db.execute(
        select(moment.label("moment"), Vote.choice)
        .where(Vote.law_id == law_id)
        .order_by("moment", Vote.choice)
    ).yield_per(chunk)

    for created, choice in result:
        yield {
            "law_id": law_id,
            "choice": choice,
            "created_at": _iso(created, None, zone) if with_time else "",
        }


def _iso(created: datetime | None, trunc: str | None, zone: ZoneInfo) -> str:
    """Format a timestamp in the display zone. Day resolution gives a bare date."""
    if created is None:
        return ""
    created = as_utc(created).astimezone(zone)
    if trunc == "day":
        return created.date().isoformat()
    if trunc == "hour":
        return created.strftime("%Y-%m-%dT%H:00:00%z")
    return created.isoformat()
