"""The evolution curve, drawn server-side as inline SVG."""

from __future__ import annotations

import re

from app.config import get_settings

# --- Courbe d'évolution -------------------------------------------------------


def _bucket(label, **counts):
    from app.crud.results import EvolutionBucket

    full = {"for": 0, "against": 0, "abstain": 0, **counts}
    return EvolutionBucket(label=label, counts=full)


def test_the_chart_stays_inside_its_plot_area():
    """A smoothed curve must never draw a day below zero votes."""

    from app.charts import build_evolution_chart

    # A spike between two quiet days is what makes a naive spline overshoot.
    chart = build_evolution_chart(
        [
            _bucket("2026-05-01", **{"for": 1}),
            _bucket("2026-05-02", **{"for": 40}),
            _bucket("2026-05-03", **{"for": 1}),
            _bucket("2026-05-04", **{"for": 2}),
        ]
    )
    assert chart is not None
    for serie in chart.series:
        ys = [float(v) for v in re.findall(r"[-\d.]+ ([-\d.]+)", serie.line)]
        assert min(ys) >= chart.plot_top - 0.01, "la courbe sort par le haut"
        assert max(ys) <= chart.plot_bottom + 0.01, "la courbe passe sous zéro"


def test_the_chart_axis_stops_on_a_round_number():
    from app.charts import build_evolution_chart

    chart = build_evolution_chart([_bucket("2026-05-01", **{"for": 37})])
    assert chart.max_value == 40
    assert [v for _y, v in chart.y_ticks] == [0, 10, 20, 30, 40]


def test_the_chart_thins_its_date_labels():
    """Whatever the span, the dates must stay far enough apart to be read."""
    from app.charts import build_evolution_chart

    for days in (2, 7, 20, 30, 45, 120):
        chart = build_evolution_chart([_bucket(f"2026-05-{(i % 28) + 1:02}") for i in range(days)])
        assert len(chart.x_ticks) <= 8, f"{days} jours : trop d'étiquettes"
        gaps = [
            chart.x_ticks[i + 1][0] - chart.x_ticks[i][0] for i in range(len(chart.x_ticks) - 1)
        ]
        assert not gaps or min(gaps) > 40, f"{days} jours : étiquettes trop serrées"
    assert chart.x_ticks[0][1] == "01/05"


def test_the_chart_describes_itself_for_a_screen_reader():
    from app.charts import build_evolution_chart

    chart = build_evolution_chart(
        [_bucket("2026-05-01", **{"for": 3}), _bucket("2026-05-02", **{"for": 5})]
    )
    assert "01/05" in chart.description
    assert "Pour : 8 votes" in chart.description


def test_the_results_page_draws_the_curve(client, open_law, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 2, raising=False)
    for _ in range(4):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
        client.cookies.clear()

    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "<svg" in page
    assert 'role="img"' in page
    assert "Évolution des votes, par jour" in page
    # The figures stay available underneath, not replaced by the picture.
    assert "Voir les chiffres jour par jour" in page


# --- Un scrutin d'un an --------------------------------------------------------


def _days(n):
    from datetime import date, timedelta

    from app.crud.results import EvolutionBucket

    return [
        EvolutionBucket(
            label=(date(2026, 1, 1) + timedelta(days=i)).isoformat(),
            counts={"for": i % 7, "against": i % 5, "abstain": i % 3},
        )
        for i in range(n)
    ]


def test_dots_are_dropped_once_they_stop_marking_anything():
    """`r="3"` circles closer together than their own diameter merge into a band.

    The guard is on the number of *drawn points*, not on the number of days, so
    it now bites in a narrower range than it used to: past three months the days
    are grouped (see below) and the points thin out on their own.
    """
    from app.charts import MAX_DOTS, build_evolution_chart

    short = build_evolution_chart(_days(MAX_DOTS))
    assert all(len(s.dots) == MAX_DOTS for s in short.series), "a short vote keeps its dots"

    crowded = build_evolution_chart(_days(MAX_DOTS + 1))
    assert all(s.dots == [] for s in crowded.series)
    assert all(s.line and s.area for s in crowded.series), "the curve itself must remain"


def test_a_long_votation_is_drawn_week_by_week():
    """At launch every votation runs a year, and 365 points is not a curve.

    Spaced 1.8 px apart on a 720-unit canvas the three series overlap into a
    smear, and the figure costs 90 kB of path data on a page this site promises
    to serve over slow connections. Grouping the days is what makes it legible
    again rather than merely smaller — a year drawn by week is 53 points, far
    enough apart that they get their dots back.
    """
    from app.charts import MAX_POINTS, build_evolution_chart

    days = _days(365)
    chart = build_evolution_chart(days)

    assert chart.period.days == 7
    assert len(chart.series[0].dots) == 53, "53 weekly points, and dots at that spacing"
    assert all(s.line and s.area for s in chart.series)
    assert len(chart.x_ticks) <= 7

    # Grouping moves nothing: a week's point is the sum of its days.
    for serie in chart.series:
        assert serie.total == sum(b.counts[serie.key] for b in days), serie.key

    # And the description says which unit it is describing, since the table
    # underneath the figure is still day by day.
    assert "Votes par semaine" in chart.description
    assert "en une semaine" in chart.description

    assert -(-len(days) // chart.period.days) <= MAX_POINTS


def test_the_curve_only_starts_grouping_past_three_months():
    """One point per day for as long as that is still readable, and no sooner."""
    from app.charts import build_evolution_chart

    assert build_evolution_chart(_days(90)).period.days == 1
    assert build_evolution_chart(_days(91)).period.days == 7
    # And it keeps widening rather than letting the point count run away.
    assert build_evolution_chart(_days(1000)).period.days == 14
    assert build_evolution_chart(_days(3000)).period.days == 28


def test_a_one_day_mobilisation_still_shows_as_a_spike():
    """The whole reason this curve exists, and the reason grouping sums.

    An average would dissolve a single loud day into the quiet ones around it,
    which is exactly the reading « une mobilisation organisée laisse un pic »
    depends on. A sum carries it into the point that contains it.
    """
    from app.charts import build_evolution_chart

    days = _days(365)
    for day in days:
        day.counts = {"for": 1, "against": 1, "abstain": 0}
    days[200].counts = {"for": 500, "against": 1, "abstain": 0}

    pour = next(s for s in build_evolution_chart(days).series if s.key == "for")
    # A quiet week sums to 7; the week holding the spike must still tower over it.
    assert pour.peak >= 500


def test_the_axis_never_draws_more_labels_than_it_promises():
    """`MAX_X_LABELS` said seven and a year-long votation drew eight.

    The stride divided the number of days rather than the gaps between labels, so
    a full set could still have the final day appended on top of it.
    """
    from app.charts import MAX_X_LABELS, _short_date, build_evolution_chart

    for n in (1, 2, 3, 7, 8, 30, 60, 61, 90, 180, 365, 400, 733):
        days = _days(n)
        chart = build_evolution_chart(days)
        assert len(chart.x_ticks) <= MAX_X_LABELS, f"{n} jours -> {len(chart.x_ticks)} étiquettes"
        # The last day is always named: it is where the curve stops, and a reader
        # cannot date the end of the votation from anything else on the figure.
        # True whatever the grouping — a point is labelled with the day its period
        # opens, so past three months this is the one tick that has to say
        # otherwise, or a votation closing mid-week would be dated days early.
        assert chart.x_ticks[-1][1] == _short_date(days[-1].label), f"{n} jours"
        # And the first, or the axis starts nowhere.
        assert chart.x_ticks[0][1] == _short_date(days[0].label), f"{n} jours"
