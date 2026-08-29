"""The evolution curve, drawn on the server as inline SVG.

No charting library, and no JavaScript. Three reasons, in order of weight:

* the Content-Security-Policy is ``script-src 'self'`` with no third-party host,
  and a chart is not worth loosening it for;
* the charter names slow connections explicitly, and a few hundred bytes of SVG
  beats fifty kilobytes of library;
* a curve that only exists once JavaScript has run is a curve some readers never
  see. This one is in the HTML.

The figure is decoration over data that is also published as a table underneath,
so it is marked up as an image with a text description rather than as something
a screen reader has to walk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Canvas. A viewBox with width="100%" scales to any column without reflowing the
# geometry, so the same numbers serve a phone and a desktop.
WIDTH = 720.0
HEIGHT = 280.0
PAD_LEFT = 46.0
PAD_RIGHT = 26.0
PAD_TOP = 18.0
PAD_BOTTOM = 36.0

# At most this many labels on the time axis, whatever the number of days.
MAX_X_LABELS = 7

# Past this many days the per-day dots are dropped and the curve is drawn as a
# line and an area alone. A year-long votation plots 365 points 1.8 px apart, so
# `r="3"` circles overlap fourfold: they stop marking anything and merge into a
# thick band, while costing about 100 kB of extra SVG on a page this site
# promises to serve over slow connections. The exact figures stay available
# underneath, in the table the chart is a picture of.
MAX_DOTS = 60

SERIES_LABELS = {"for": "Pour", "against": "Contre", "abstain": "Abstention"}


@dataclass
class Series:
    key: str
    label: str
    line: str  # SVG path — the curve itself
    area: str  # SVG path — the same curve closed to the baseline
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


def _nice_ceiling(value: int) -> tuple[int, int]:
    """A round top for the axis, and a step that divides it.

    Axes that stop at 37 with ticks at 9.25 read as machine output. This walks
    the 1 / 2 / 5 × 10ⁿ ladder until four steps clear the highest bar.
    """
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
    """A Catmull-Rom curve through every point, as cubic béziers.

    Control points are clamped to the vertical range of the segment they belong
    to. Without that, a spike between two low days makes the curve dip below the
    baseline on its way back down — drawing negative votes that were never cast.
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
    """``2026-05-04`` → ``04/05``, falling back to the raw label."""
    try:
        return date.fromisoformat(label).strftime("%d/%m")
    except ValueError:
        return label


def _describe(buckets, series: list[Series]) -> str:
    """The sentence a screen reader gets instead of the picture."""
    if not buckets:
        return "Aucune donnée."
    span = f"du {_short_date(buckets[0].label)} au {_short_date(buckets[-1].label)}"
    parts = [f"{s.label} : {s.total} votes, au plus {s.peak} en un jour" for s in series]
    return f"Votes par jour {span}. " + " ; ".join(parts) + "."


def build_evolution_chart(buckets) -> EvolutionChart | None:
    """Turn the daily buckets into everything the template needs to draw.

    Returns None when there is nothing to draw — the caller already withholds the
    data below the anonymity threshold, so this only guards the empty case.
    """
    if not buckets:
        return None

    keys = list(SERIES_LABELS)
    peak = max((max(b.counts.get(k, 0) for k in keys) for b in buckets), default=0)
    top, step = _nice_ceiling(peak)

    plot_left, plot_right = PAD_LEFT, WIDTH - PAD_RIGHT
    plot_top, plot_bottom = PAD_TOP, HEIGHT - PAD_BOTTOM
    plot_height = plot_bottom - plot_top
    plot_width = plot_right - plot_left

    def x_at(index: int) -> float:
        if len(buckets) == 1:
            return (plot_left + plot_right) / 2
        return plot_left + plot_width * index / (len(buckets) - 1)

    def y_at(value: int) -> float:
        return plot_bottom - plot_height * (value / top if top else 0)

    series = []
    for key in keys:
        points = [(x_at(i), y_at(b.counts.get(key, 0))) for i, b in enumerate(buckets)]
        line = _smooth(points)
        area = ""
        if len(points) > 1:
            area = (
                f"{line} L {points[-1][0]:.1f} {plot_bottom:.1f} "
                f"L {points[0][0]:.1f} {plot_bottom:.1f} Z"
            )
        counts = [b.counts.get(key, 0) for b in buckets]
        series.append(
            Series(
                key=key,
                label=SERIES_LABELS[key],
                line=line,
                area=area,
                # Dropped on a long votation — see MAX_DOTS.
                dots=points if len(buckets) <= MAX_DOTS else [],
                total=sum(counts),
                peak=max(counts, default=0),
            )
        )

    y_ticks = [(y_at(v), v) for v in range(0, top + 1, step)]

    # Thin the date labels rather than letting them collide and turn to mush.
    # The last day is always worth showing, but only if the stride did not
    # already leave a label right next to it — two dates a few pixels apart are
    # less readable than one.
    #
    # The stride divides the *intervals* between labels, not the days: with
    # `MAX_X_LABELS` labels there are one fewer gaps between them. Dividing by the
    # label count instead left room for the final day to be appended on top of a
    # full set, and a year-long votation drew eight labels under a constant that
    # says at most seven.
    stride = max(1, -(-(len(buckets) - 1) // max(1, MAX_X_LABELS - 1)))
    indices = [i for i in range(len(buckets)) if i % stride == 0]
    last = len(buckets) - 1
    if last not in indices:
        if indices and last - indices[-1] < stride / 2:
            indices.pop()
        indices.append(last)
    x_ticks = [(x_at(i), _short_date(buckets[i].label)) for i in indices]

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
        description=_describe(buckets, series),
    )
