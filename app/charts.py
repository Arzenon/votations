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

# Past this many drawn points the dots are dropped and the curve is a line and an
# area alone: `r="3"` circles closer together than their own diameter stop
# marking anything and merge into a thick band.
MAX_DOTS = 60

# And past this many, days are grouped together rather than drawn one by one.
#
# Ninety points is already dense on a 720-unit canvas — seven pixels apart — and
# a year-long votation asked for 365, which is 1.8. At that spacing there is no
# per-day resolution left to see: the curve is sub-pixel in x, the three series
# overlap into a smear, and the figure costs 90 kB of path data on a page this
# site promises to serve over slow connections. Grouping is what makes it legible
# again, not only smaller — a year drawn week by week is 53 points, 12 px apart,
# and gets its dots back.
#
# Nothing is hidden by it. The table underneath is built from the daily buckets
# and stays daily, which is what the chart is a picture *of*; this only decides
# how the picture is drawn.
MAX_POINTS = 90

SERIES_LABELS = {"for": "Pour", "against": "Contre", "abstain": "Abstention"}


@dataclass(frozen=True)
class Period:
    """How much time one drawn point covers, and how to say so in French."""

    days: int
    #: « jour » — read as « par {name} », so it names the unit, not a count.
    name: str
    #: « une journée » — read as « en {span} » and « un pic sur {span} ». Both
    #: sentences are about one point's worth of time, so they share one wording;
    #: assembling it in the template instead would put French grammar in a place
    #: no test reads.
    span: str


# The widths a point may cover, narrowest first. Whole weeks past the first, so a
# grouped point always spans the same days of the week as its neighbours and a
# weekly rhythm in the votes cannot turn into a wobble in the curve.
PERIODS = (
    Period(1, "jour", "une journée"),
    Period(7, "semaine", "une semaine"),
    Period(14, "quinzaine", "une quinzaine"),
    Period(28, "période de quatre semaines", "quatre semaines"),
)


@dataclass(frozen=True)
class _Point:
    """One drawn point: where it starts, and the votes it covers."""

    label: str
    counts: dict[str, int]


def _period_for(days: int) -> Period:
    """The narrowest period that keeps the curve under :data:`MAX_POINTS`.

    Falls back to the widest rung rather than inventing one: a votation open long
    enough to overflow four-week points is seven years old, and drawing it with a
    few points too many is a better answer than a unit nobody can name.
    """
    for period in PERIODS:
        if -(-days // period.days) <= MAX_POINTS:
            return period
    return PERIODS[-1]


def _grouped(buckets, period: Period) -> list[_Point]:
    """Sum the daily buckets into points of ``period``, oldest first.

    Summed, never averaged. The one reading this curve exists for is « une
    mobilisation organisée laisse un pic » — and an average is precisely the
    operation that dissolves a spike into the days around it, while a sum carries
    it into the point that contains it.

    Grouping runs from the oldest day, so the *last* point is the one that may
    cover a short period. On a votation still open that is not an artefact at
    all — the current week genuinely is not over — and on a closed one it is a
    single point at the edge rather than anything in the body of the curve.
    """
    if period.days == 1:
        return [_Point(b.label, dict(b.counts)) for b in buckets]
    return [
        _Point(
            window[0].label,
            {key: sum(b.counts.get(key, 0) for b in window) for key in SERIES_LABELS},
        )
        for window in (
            buckets[start : start + period.days] for start in range(0, len(buckets), period.days)
        )
    ]


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
    #: What one point covers. Read by the template, which has to say so: a curve
    #: drawn week by week over a table drawn day by day is only honest if the
    #: page names the difference.
    period: Period = PERIODS[0]


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


def _describe(buckets, series: list[Series], period: Period) -> str:
    """The sentence a screen reader gets instead of the picture.

    ``buckets`` is the *daily* list, not the grouped one, because the span has to
    end on the last day anybody voted — the last grouped point is labelled with
    the day its period *starts*, which would cut the sentence short by up to a
    period. The peaks come from ``series`` and are therefore per grouped point,
    which is why the unit is named rather than assumed.
    """
    if not buckets:
        return "Aucune donnée."
    span = f"du {_short_date(buckets[0].label)} au {_short_date(buckets[-1].label)}"
    parts = [f"{s.label} : {s.total} votes, au plus {s.peak} en {period.span}" for s in series]
    return f"Votes par {period.name} {span}. " + " ; ".join(parts) + "."


def build_evolution_chart(buckets) -> EvolutionChart | None:
    """Turn the daily buckets into everything the template needs to draw.

    Returns None when there is nothing to draw — the caller already withholds the
    data below the anonymity threshold, so this only guards the empty case.

    ``buckets`` is always one entry per calendar day; how many of them share a
    drawn point is decided here (see :data:`MAX_POINTS`) and nowhere else, so the
    caller and the table it also feeds are unaffected.
    """
    if not buckets:
        return None

    # `buckets` stays what the parameter says it is — one entry per calendar day —
    # and `drawn` is what actually goes on the canvas. Everything below is about
    # the picture and reads `drawn`; the two places that are about the votation
    # rather than the picture, the axis's final label and the text description,
    # say `buckets` and mean it.
    period = _period_for(len(buckets))
    drawn = _grouped(buckets, period)

    keys = list(SERIES_LABELS)
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
                label=SERIES_LABELS[key],
                line=line,
                area=area,
                # Dropped on a long votation — see MAX_DOTS.
                dots=points if len(drawn) <= MAX_DOTS else [],
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
    stride = max(1, -(-(len(drawn) - 1) // max(1, MAX_X_LABELS - 1)))
    indices = [i for i in range(len(drawn)) if i % stride == 0]
    last = len(drawn) - 1
    if last not in indices:
        if indices and last - indices[-1] < stride / 2:
            indices.pop()
        indices.append(last)
    x_ticks = [(x_at(i), _short_date(drawn[i].label)) for i in indices]

    # The right-hand label names the last day of the votation, not the day its
    # last point *starts* on.
    #
    # A point is labelled with the day its period opens, which is the right
    # answer for every tick along the axis and the wrong one at the very end:
    # grouped by week, a votation closing on a Wednesday ends on a point labelled
    # with the preceding Sunday, and the figure then dates the end of the vote
    # four days early. Nothing else on the chart carries that date — the axis is
    # the only place a reader can read where the curve stops — so the last tick
    # is the one that has to name it.
    #
    # A no-op while the curve is drawn day by day, where the two are the same day.
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
        description=_describe(buckets, series, period),
        period=period,
    )
