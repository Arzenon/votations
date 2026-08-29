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
    assert "Évolution des votes, jour par jour" in page
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


def test_a_long_votation_drops_the_per_day_dots():
    """At launch every votation runs a year, and 365 dots stop being dots.

    Spaced 1.8 px apart with `r="3"` they overlap fourfold: they mark nothing and
    merge into a thick band, while adding about 60 kB of SVG to a page this site
    promises to serve over slow connections. The line and the area still draw the
    shape, and the exact figures are in the table underneath.
    """
    from app.charts import MAX_DOTS, build_evolution_chart

    short = build_evolution_chart(_days(MAX_DOTS))
    assert all(len(s.dots) == MAX_DOTS for s in short.series), "a short vote keeps its dots"

    long = build_evolution_chart(_days(365))
    assert all(s.dots == [] for s in long.series)
    assert all(s.line and s.area for s in long.series), "the curve itself must remain"
    # And the description a screen reader gets is unaffected by the thinning.
    assert "Votes par jour" in long.description


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
        assert chart.x_ticks[-1][1] == _short_date(days[-1].label)
        # And the first, or the axis starts nowhere.
        assert chart.x_ticks[0][1] == _short_date(days[0].label)
