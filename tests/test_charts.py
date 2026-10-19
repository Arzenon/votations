"""The evolution curve, drawn server-side as inline SVG."""

from __future__ import annotations

import re

from app.config import get_settings

# --- Evolution curve -------------------------------------------------------------


def _bucket(label, **counts):
    from app.crud.results import EvolutionBucket

    full = {"for": 0, "against": 0, "abstain": 0, **counts}
    return EvolutionBucket(label=label, counts=full, daily=[sum(full.values())])


def test_the_chart_stays_inside_its_plot_area():
    """The smoothed curve never goes below zero."""

    from app.charts import build_evolution_chart

    # A spike between two quiet days makes a naive spline overshoot.
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
        assert min(ys) >= chart.plot_top - 0.01, "the curve goes above the plot"
        assert max(ys) <= chart.plot_bottom + 0.01, "the curve goes below zero"


def test_the_chart_axis_stops_on_a_round_number():
    from app.charts import build_evolution_chart

    chart = build_evolution_chart([_bucket("2026-05-01", **{"for": 37})])
    assert chart.max_value == 40
    assert [v for _y, v in chart.y_ticks] == [0, 10, 20, 30, 40]


def test_the_chart_thins_its_date_labels():
    """Date labels are thinned out."""
    from app.charts import build_evolution_chart

    for days in (2, 7, 20, 30, 45, 120):
        chart = build_evolution_chart([_bucket(f"2026-05-{(i % 28) + 1:02}") for i in range(days)])
        assert len(chart.x_ticks) <= 8, f"{days} days: too many labels"
        gaps = [
            chart.x_ticks[i + 1][0] - chart.x_ticks[i][0] for i in range(len(chart.x_ticks) - 1)
        ]
        assert not gaps or min(gaps) > 40, f"{days} days: labels too close"
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
    # The data table is still there.
    assert "Voir les chiffres jour par jour" in page


# --- A year-long vote ----------------------------------------------------------


def _days(n):
    from datetime import date, timedelta

    from app.crud.results import EvolutionBucket

    return [
        EvolutionBucket(
            label=(date(2026, 1, 1) + timedelta(days=i)).isoformat(),
            counts={"for": i % 7, "against": i % 5, "abstain": i % 3},
            daily=[i % 7 + i % 5 + i % 3],
        )
        for i in range(n)
    ]


def test_dots_are_dropped_once_they_stop_marking_anything():
    """Dots are dropped when there are too many points."""
    from app.charts import MAX_DOTS, build_evolution_chart

    short = build_evolution_chart(_days(MAX_DOTS))
    assert all(len(s.dots) == MAX_DOTS for s in short.series), "a short vote keeps its dots"

    crowded = build_evolution_chart(_days(MAX_DOTS + 1))
    assert all(s.dots == [] for s in crowded.series)
    assert all(s.line and s.area for s in crowded.series), "the curve itself must remain"


def test_a_long_votation_is_drawn_week_by_week():
    """A year-long vote is drawn week by week (53 points instead of 365)."""
    from app.charts import MAX_POINTS, build_evolution_chart

    days = _days(365)
    chart = build_evolution_chart(days)

    assert chart.period.days == 7
    assert len(chart.series[0].dots) == 53, "53 weekly points, and dots at that spacing"
    assert all(s.line and s.area for s in chart.series)
    assert len(chart.x_ticks) <= 7

    # A week's point is the sum of its days.
    for serie in chart.series:
        assert serie.total == sum(b.counts[serie.key] for b in days), serie.key

    # The description names the unit, since the table keeps days.
    assert "Votes par semaine" in chart.description
    assert "en une semaine" in chart.description

    assert -(-len(days) // chart.period.days) <= MAX_POINTS


def test_the_curve_only_starts_grouping_past_three_months():
    """Grouping only starts after three months."""
    from app.charts import build_evolution_chart

    assert build_evolution_chart(_days(90)).period.days == 1
    assert build_evolution_chart(_days(91)).period.days == 7
    # Then it keeps widening.
    assert build_evolution_chart(_days(1000)).period.days == 14
    assert build_evolution_chart(_days(3000)).period.days == 28


def test_a_one_day_mobilisation_still_shows_as_a_spike():
    """A one-day spike still shows when grouped (sum, not average)."""
    from app.charts import build_evolution_chart

    days = [_bucket(day.label, **{"for": 1, "against": 1}) for day in _days(365)]
    days[200] = _bucket(days[200].label, **{"for": 500, "against": 1})

    pour = next(s for s in build_evolution_chart(days).series if s.key == "for")
    # A quiet week sums to 7; the spike week must stand out.
    assert pour.peak >= 500


def test_the_axis_never_draws_more_labels_than_it_promises():
    """The axis never has more than MAX_X_LABELS labels."""
    from app.charts import MAX_X_LABELS, _short_date, build_evolution_chart

    for n in (1, 2, 3, 7, 8, 30, 60, 61, 90, 180, 365, 400, 733):
        days = _days(n)
        chart = build_evolution_chart(days)
        assert len(chart.x_ticks) <= MAX_X_LABELS, f"{n} days -> {len(chart.x_ticks)} labels"
        # The last label is the actual last day, even when grouped by week.
        assert chart.x_ticks[-1][1] == _short_date(days[-1].label), f"{n} days"
        # And the first, or the axis starts nowhere.
        assert chart.x_ticks[0][1] == _short_date(days[0].label), f"{n} days"


# --- The table under the curve ------------------------------------------------


def test_the_day_by_day_table_is_dated_in_french(client, open_law, monkeypatch):
    """The daily table shows French dates, not ISO."""
    from datetime import UTC, datetime

    from app.config import display_zone
    from app.labels import french_date

    monkeypatch.setattr(get_settings(), "min_bucket_volume", 2, raising=False)
    for _ in range(4):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
        client.cookies.clear()

    page = client.get(f"/loi/{open_law.slug}/resultats").text
    # Today in the display zone.
    today = datetime.now(UTC).astimezone(display_zone()).date()

    assert f'<th scope="row">{french_date(today)}</th>' in page, (
        "the table rows should have French dates"
    )
    # Only check the row header: the JSON-LD has ISO dates on purpose.
    assert f'<th scope="row">{today.isoformat()}' not in page, "the table should not show ISO dates"


def test_merged_quiet_days_are_spread_not_drawn_as_one_day():
    """Ten votes over ten quiet days read as one a day, not ten in a day."""
    from app.charts import build_evolution_chart
    from app.crud.results import EvolutionBucket

    buckets = [
        EvolutionBucket(
            label=f"2026-05-{1 + 10 * i:02}",
            counts={"for": 9, "against": 1, "abstain": 0},
            daily=[1] * 10,
        )
        for i in range(3)
    ]
    chart = build_evolution_chart(buckets)
    assert "Pour : 27 votes, au plus 1 en une journée" in chart.description
    assert chart.x_ticks[-1][1] == "30/05", "the axis ends on the last merged day"


def test_every_merged_day_keeps_its_total_whatever_the_split():
    """Each merged day keeps its own total, and each choice adds up to the period's."""
    from app.charts import _daily
    from app.crud.results import EvolutionBucket

    for counts, daily in [
        ({"for": 4, "against": 3, "abstain": 3}, [1] * 10),
        ({"for": 5, "against": 5, "abstain": 0}, [5, 5]),
        ({"for": 9, "against": 1, "abstain": 0}, [1] * 10),
        ({"for": 7, "against": 0, "abstain": 6}, [0, 4, 0, 9]),
        # A spike inside a merged period stays on its day.
        ({"for": 1800, "against": 200, "abstain": 9}, [1] * 9 + [2000]),
    ]:
        bucket = EvolutionBucket(label="2026-05-01", counts=counts, daily=daily)
        days = _daily([bucket])
        assert [sum(point.counts.values()) for point in days] == daily, (counts, daily)
        for key, n in counts.items():
            assert sum(point.counts[key] for point in days) == n, (counts, daily)
