"""The evolution curve, rendered server-side as inline SVG.

No JS library: the CSP only allows our own scripts, it's lighter on slow
connections, and the chart shows up without JavaScript. The same data is in a
table below, so the SVG is an image with a text description.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.labels import CHOICE_LABELS

# viewBox dimensions; the SVG scales to its container.
WIDTH = 720.0
HEIGHT = 280.0
PAD_LEFT = 46.0
PAD_RIGHT = 26.0
PAD_TOP = 18.0
PAD_BOTTOM = 36.0

# Max labels on the time axis.
MAX_X_LABELS = 7

# Above this many points, dots are no longer drawn (they would overlap).
MAX_DOTS = 60

# Above this many points, days are grouped (weeks, etc.). A year of daily
# points is unreadable and heavy. The table below stays daily.
MAX_POINTS = 90


@dataclass(frozen=True)
class Period:
    """Time covered by one point, with its French wording."""

    days: int
    # Used as "par {name}", e.g. "jour".
    name: str
    # Used as "en {span}", e.g. "une journée".
    span: str


# Narrowest first. Whole weeks, so a weekly pattern doesn't make the curve wobble.
PERIODS = (
    Period(1, "jour", "une journée"),
    Period(7, "semaine", "une semaine"),
    Period(14, "quinzaine", "une quinzaine"),
    Period(28, "période de quatre semaines", "quatre semaines"),
)


@dataclass(frozen=True)
class _Point:
    """One point: its first day and its vote counts."""

    label: str
    counts: dict[str, int]


def _period_for(days: int) -> Period:
    """The narrowest period giving at most MAX_POINTS points (else the widest)."""
    for period in PERIODS:
        if -(-days // period.days) <= MAX_POINTS:
            return period
    return PERIODS[-1]


def _daily(buckets) -> list[_Point]:
    """One point per day. A bucket of merged quiet days is spread over its days.

    Drawn whole on its first day, ten votes over ten quiet days would read as
    ten votes in one day. Remainders go to the first days, so totals are exact.
    """
    out = []
    for b in buckets:
        for i in range(b.days):
            share: dict[str, int] = {}
            for key in CHOICE_LABELS:
                whole, rest = divmod(b.counts.get(key, 0), b.days)
                share[key] = whole + (i < rest)
            out.append(_Point((b.day + timedelta(days=i)).isoformat(), share))
    return out


def _grouped(buckets, period: Period) -> list[_Point]:
    """Sum daily buckets into points of ``period``, oldest first.

    Summed rather than averaged, so a spike stays visible. Only the last point
    may cover a shorter period.
    """
    if period.days == 1:
        return [_Point(b.label, dict(b.counts)) for b in buckets]
    return [
        _Point(
            window[0].label,
            {key: sum(b.counts.get(key, 0) for b in window) for key in CHOICE_LABELS},
        )
        for window in (
            buckets[start : start + period.days] for start in range(0, len(buckets), period.days)
        )
    ]


@dataclass
class Series:
    key: str
    label: str
    line: str  # SVG path of the curve
    area: str  # same path closed down to the baseline
    dots: list[tuple[float, float]] = field(default_factory=list)
    total: int = 0
    peak: int = 0


@dataclass
class EvolutionChart:
    width: float
    height: float
    plot_left: float
    plot_right: float
    plot_top: float
    plot_bottom: float
    series: list[Series]
    y_ticks: list[tuple[float, int]]
    x_ticks: list[tuple[float, str]]
    max_value: int
    description: str
    # Shown on the page, since the table is always daily.
    period: Period = PERIODS[0]


def _nice_ceiling(value: int) -> tuple[int, int]:
    """A round axis maximum and step (1, 2 or 5 times a power of ten, 4 steps)."""
    if value <= 4:
        return max(value, 1), 1
    magnitude = 1
    while True:
        for factor in (1, 2, 5):
            step = factor * magnitude
            if step * 4 >= value:
                return step * 4, step
        magnitude *= 10


def _smooth(points: list[tuple[float, float]]) -> str:
    """Catmull-Rom curve through the points, as cubic Béziers.

    Control points are clamped to each segment's vertical range so the curve
    never dips below zero after a spike.
    """
    if not points:
        return ""
    if len(points) == 1:
        x, y = points[0]
        return f"M {x:.1f} {y:.1f}"

    out = [f"M {points[0][0]:.1f} {points[0][1]:.1f}"]
    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2

        low, high = min(p1[1], p2[1]), max(p1[1], p2[1])
        c1x = p1[0] + (p2[0] - p0[0]) / 6
        c1y = min(max(p1[1] + (p2[1] - p0[1]) / 6, low), high)
        c2x = p2[0] - (p3[0] - p1[0]) / 6
        c2y = min(max(p2[1] - (p3[1] - p1[1]) / 6, low), high)
        out.append(f"C {c1x:.1f} {c1y:.1f}, {c2x:.1f} {c2y:.1f}, {p2[0]:.1f} {p2[1]:.1f}")
    return " ".join(out)


def _short_date(label: str) -> str:
    """``2026-05-04`` to ``04/05``, or the label unchanged."""
    try:
        return date.fromisoformat(label).strftime("%d/%m")
    except ValueError:
        return label


def _describe(buckets, series: list[Series], period: Period, *, merged: bool) -> str:
    """Text alternative for screen readers.

    Takes the daily buckets so the range ends on the actual last day.
    """
    if not buckets:
        return "Aucune donnée."
    span = f"du {_short_date(buckets[0].label)} au {_short_date(buckets[-1].label)}"
    parts = [f"{s.label} : {s.total} votes, au plus {s.peak} en {period.span}" for s in series]
    text = f"Votes par {period.name} {span}. " + " ; ".join(parts) + "."
    if merged:
        text += " Les jours calmes regroupés sont répartis à parts égales."
    return text


def build_evolution_chart(buckets) -> EvolutionChart | None:
    """Build the chart from daily buckets. None if there's nothing to draw.

    Grouping days into points (see MAX_POINTS) happens here only.
    """
    if not buckets:
        return None

    # One point per day, then `drawn` is what goes on the chart.
    merged = any(b.days > 1 for b in buckets)
    buckets = _daily(buckets)
    period = _period_for(len(buckets))
    drawn = _grouped(buckets, period)

    keys = list(CHOICE_LABELS)
    peak = max((max(b.counts.get(k, 0) for k in keys) for b in drawn), default=0)
    top, step = _nice_ceiling(peak)

    plot_left, plot_right = PAD_LEFT, WIDTH - PAD_RIGHT
    plot_top, plot_bottom = PAD_TOP, HEIGHT - PAD_BOTTOM
    plot_height = plot_bottom - plot_top
    plot_width = plot_right - plot_left

    def x_at(index: int) -> float:
        if len(drawn) == 1:
            return (plot_left + plot_right) / 2
        return plot_left + plot_width * index / (len(drawn) - 1)

    def y_at(value: int) -> float:
        return plot_bottom - plot_height * (value / top if top else 0)

    series = []
    for key in keys:
        points = [(x_at(i), y_at(b.counts.get(key, 0))) for i, b in enumerate(drawn)]
        line = _smooth(points)
        area = ""
        if len(points) > 1:
            area = (
                f"{line} L {points[-1][0]:.1f} {plot_bottom:.1f} "
                f"L {points[0][0]:.1f} {plot_bottom:.1f} Z"
            )
        counts = [b.counts.get(key, 0) for b in drawn]
        series.append(
            Series(
                key=key,
                label=CHOICE_LABELS[key],
                line=line,
                area=area,
                # See MAX_DOTS.
                dots=points if len(drawn) <= MAX_DOTS else [],
                total=sum(counts),
                peak=max(counts, default=0),
            )
        )

    y_ticks = [(y_at(v), v) for v in range(0, top + 1, step)]

    # Thin out date labels. Always show the last day, dropping the previous label
    # if it would be too close. The stride divides the gaps (labels - 1).
    stride = max(1, -(-(len(drawn) - 1) // max(1, MAX_X_LABELS - 1)))
    indices = [i for i in range(len(drawn)) if i % stride == 0]
    last = len(drawn) - 1
    if last not in indices:
        if indices and last - indices[-1] < stride / 2:
            indices.pop()
        indices.append(last)
    x_ticks = [(x_at(i), _short_date(drawn[i].label)) for i in indices]

    # The last label shows the actual last day, not the first day of the last
    # group.
    if x_ticks:
        x_ticks[-1] = (x_ticks[-1][0], _short_date(buckets[-1].label))

    return EvolutionChart(
        width=WIDTH,
        height=HEIGHT,
        plot_left=plot_left,
        plot_right=plot_right,
        plot_top=plot_top,
        plot_bottom=plot_bottom,
        series=series,
        y_ticks=y_ticks,
        x_ticks=x_ticks,
        max_value=top,
        description=_describe(buckets, series, period, merged=merged),
        period=period,
    )
