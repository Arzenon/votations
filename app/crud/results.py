"""Aggregating votes into the deliberately simple, manipulation-resistant stats.

Only three numbers are ever surfaced publicly: % for / % against / % abstain and
the total voter count. The V1 evolution curve buckets votes over time, gated by a
minimum volume to prevent re-identification.

Several functions here need the same number — how many votes this law has — to
decide what may be published. They all take it as an optional argument so a page
that already knows it counts once instead of four times.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import as_utc
from app.models import Vote, VoteChoice

# How the export rounds `created_at`, per `export_time_resolution`.
_EXPORT_TRUNC = {"day": "day", "hour": "hour"}


def _display_zone() -> ZoneInfo:
    """The zone every published instant is expressed in. Validated at boot."""
    return ZoneInfo(get_settings().display_timezone)


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


@dataclass
class Tally:
    total: int
    counts: dict[str, int]
    percentages: dict[str, float]


def _tally_from_counts(counts: dict[str, int]) -> Tally:
    total = sum(counts.values())
    pct = {k: (round(100 * v / total, 1) if total else 0.0) for k, v in counts.items()}
    return Tally(total=total, counts=counts, percentages=pct)


def tally(db: Session, law_id: int) -> Tally:
    rows = db.execute(
        select(Vote.choice, func.count()).where(Vote.law_id == law_id).group_by(Vote.choice)
    ).all()
    counts = {c.value: 0 for c in VoteChoice}
    for choice, n in rows:
        counts[choice] = n
    return _tally_from_counts(counts)


def tallies_for(db: Session, law_ids: list[int]) -> dict[int, Tally]:
    """Tallies for many laws in one query — the admin dashboard lists them all."""
    if not law_ids:
        return {}
    rows = db.execute(
        select(Vote.law_id, Vote.choice, func.count())
        .where(Vote.law_id.in_(law_ids))
        .group_by(Vote.law_id, Vote.choice)
    ).all()
    counts: dict[int, dict[str, int]] = {
        law_id: {c.value: 0 for c in VoteChoice} for law_id in law_ids
    }
    for law_id, choice, n in rows:
        counts[law_id][choice] = n
    return {law_id: _tally_from_counts(c) for law_id, c in counts.items()}


def count_votes(db: Session, law_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Vote).where(Vote.law_id == law_id)) or 0


@dataclass
class EvolutionBucket:
    label: str
    counts: dict[str, int] = field(default_factory=dict)


def evolution(db: Session, law_id: int, *, total: int | None = None) -> list[EvolutionBucket]:
    """Daily vote counts over time (V1).

    Returns an empty list until the total reaches ``min_bucket_volume`` so that
    low-volume votations cannot be used to re-identify or fingerprint voters.

    Bucketing happens in the database: reading every vote row back into Python to
    group it was the single most expensive query in the application.
    """
    settings = get_settings()
    if total is None:
        total = count_votes(db, law_id)
    if total < settings.min_bucket_volume:
        return []

    zone = _display_zone()
    bucket = _bucketed("day", settings.display_timezone)
    rows = db.execute(
        select(bucket.label("bucket"), Vote.choice, func.count())
        .where(Vote.law_id == law_id)
        .group_by("bucket", Vote.choice)
        .order_by("bucket")
    ).all()

    out: dict[str, dict[str, int]] = {}
    for moment, choice, n in rows:
        label = as_utc(moment).astimezone(zone).strftime("%Y-%m-%d")
        out.setdefault(label, {c.value: 0 for c in VoteChoice})[choice] = n
    return [EvolutionBucket(label=label, counts=counts) for label, counts in sorted(out.items())]


def export_note(db: Session, law_id: int, *, total: int | None = None) -> str:
    """What was withheld from this law's export, in the words shown to readers."""
    settings = get_settings()
    if not _timestamps_allowed(db, law_id, total=total):
        return (
            "L'horodatage est omis : cette votation compte moins de "
            f"{settings.min_bucket_volume} votes, seuil en dessous duquel la "
            "répartition dans le temps n'est pas publiée — la publier ici "
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
    zone = _display_zone()

    if not with_time:
        moment = literal(None)
    elif trunc:
        moment = _bucketed(trunc, settings.display_timezone)
    else:
        moment = Vote.created_at

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
