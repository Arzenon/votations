"""Aggregate demographics: one-time recording, no PII, and cell suppression."""

from app.crud import demographics as demo_crud
from app.db import utcnow
from app.models import DemographicTally, Vote, VoteChoice
from tests.helpers import cast_ballot


def _dimension(db, law, dimension, **kwargs):
    """One dimension's breakdown, via `all_breakdowns`."""
    return next(
        bd for bd in demo_crud.all_breakdowns(db, law, **kwargs) if bd.dimension == dimension
    )


def _cell(db, law_id, dimension, category, choice):
    return (
        db.query(DemographicTally)
        .filter_by(law_id=law_id, dimension=dimension, category=category, choice=choice)
        .first()
    )


def test_record_increments_marginal_counters(open_law, db_session):
    vote = cast_ballot(
        db_session, open_law, "tok-1", VoteChoice.FOR, age_bracket="35-49", sexe="femme"
    )

    assert _cell(db_session, open_law.id, "age", "35-49", "for").count == 1
    assert _cell(db_session, open_law.id, "sexe", "femme", "for").count == 1
    assert vote.demographics_recorded is True


def test_no_per_voter_demographics_stored(open_law, db_session):
    cast_ballot(db_session, open_law, "tok-2", VoteChoice.FOR, age_bracket="25-34", sexe="homme")

    # Counters carry no token; vote row carries no demographic values.
    cols = {c.name for c in DemographicTally.__table__.columns}
    assert "voter_token" not in cols and "token" not in cols
    vote_cols = {c.name for c in Vote.__table__.columns}
    assert "age_bracket" not in vote_cols and "sexe" not in vote_cols


def test_demographics_recorded_once_despite_vote_change(open_law, db_session):
    cast_ballot(db_session, open_law, "tok-3", VoteChoice.FOR, age_bracket="25-34", sexe="homme")
    # The voter resubmits their profile: no double counting.
    cast_ballot(
        db_session, open_law, "tok-3", VoteChoice.AGAINST, age_bracket="25-34", sexe="homme"
    )

    assert _cell(db_session, open_law.id, "age", "25-34", "for").count == 1
    assert _cell(db_session, open_law.id, "age", "25-34", "against") is None


def test_invalid_values_ignored(open_law, db_session):
    vote = cast_ballot(db_session, open_law, "tok-4", VoteChoice.FOR, age_bracket="999", sexe="")
    assert db_session.query(DemographicTally).count() == 0
    assert vote.demographics_recorded is False


def _seed_cells(db, law, dimension, data):
    """Seed {category: {choice: count}} as already published (snapshot taken).

    These tests are about suppression; the snapshot rule is in test_privacy.
    """
    for category, choices in data.items():
        for choice, count in choices.items():
            db.add(
                DemographicTally(
                    law_id=law.id,
                    dimension=dimension,
                    category=category,
                    choice=choice,
                    count=count,
                    published_count=count,
                )
            )
    law.demographics_snapshot_at = utcnow()
    db.commit()


def test_breakdown_hidden_below_threshold(open_law, db_session):
    # One cell (2) is below the threshold (10): the whole table is hidden.
    _seed_cells(
        db_session,
        open_law,
        "age",
        {
            "35-49": {"for": 40, "against": 30, "abstain": 12},
            "25-34": {"for": 15, "against": 2, "abstain": 11},  # the "2" trips suppression
        },
    )
    bd = _dimension(db_session, open_law, "age")
    assert bd.shown is False
    assert bd.reason == "below_threshold"


def test_breakdown_shown_above_threshold(open_law, db_session):
    _seed_cells(
        db_session,
        open_law,
        "age",
        {
            "35-49": {"for": 40, "against": 30, "abstain": 12},
            "50-64": {"for": 22, "against": 18, "abstain": 10},
        },
    )
    bd = _dimension(db_session, open_law, "age")
    assert bd.shown is True
    # Canonical ordering: 35-49 before 50-64.
    assert [r.category for r in bd.rows] == ["35-49", "50-64"]
    first = bd.rows[0]
    assert first.total == 82
    assert first.percentages["for"] == round(100 * 40 / 82, 1)


def test_region_recorded_as_marginal(open_law, db_session):
    cast_ballot(
        db_session, open_law, "tok-r", VoteChoice.FOR, age_bracket="35-49", region="Bretagne"
    )
    assert _cell(db_session, open_law.id, "region", "Bretagne", "for").count == 1
    # No age x region cells, only separate counters.
    dims = {d.dimension for d in db_session.query(DemographicTally).all()}
    assert dims == {"age", "region"}


def test_provision_rate(open_law, db_session):
    for i in range(4):
        cast_ballot(db_session, open_law, f"p{i}", VoteChoice.FOR)
    # Two of the four share a profile.
    cast_ballot(db_session, open_law, "p0", VoteChoice.FOR, sexe="femme")
    cast_ballot(db_session, open_law, "p1", VoteChoice.FOR, sexe="homme")

    rate = demo_crud.provision_rate(db_session, open_law.id)
    assert rate.total_votes == 4
    assert rate.with_profile == 2
    assert rate.percentage == 50.0


def test_breakdown_includes_share_and_reference(open_law, db_session):
    _seed_cells(
        db_session,
        open_law,
        "sexe",
        {
            "femme": {"for": 30, "against": 20, "abstain": 10},  # 60
            "homme": {"for": 25, "against": 10, "abstain": 10},  # 45
        },
    )
    bd = _dimension(db_session, open_law, "sexe")
    assert bd.shown is True
    assert bd.dimension_total == 105
    femme = bd.rows[0]
    assert femme.share == round(100 * 60 / 105, 1)
    assert femme.reference == 51.5  # from INSEE_REFERENCE


def test_export_json_only_includes_shown_breakdowns(client, open_law, db_session):
    _seed_cells(
        db_session,
        open_law,
        "sexe",
        {
            "femme": {"for": 30, "against": 20, "abstain": 11},
            "homme": {"for": 25, "against": 19, "abstain": 14},
        },
    )
    _seed_cells(db_session, open_law, "age", {"18-24": {"for": 1, "against": 0, "abstain": 0}})

    data = client.get(f"/loi/{open_law.slug}/export.json").json()
    dims = {d["dimension"] for d in data["demographics"]}
    assert "sexe" in dims  # above threshold: exported
    assert "age" not in dims  # below threshold: suppressed
    # Per-vote rows still carry no demographic fields.
    for row in data["votes"]:
        assert set(row.keys()) == {"law_id", "choice", "created_at"}


def test_the_national_share_covers_the_same_categories_as_the_share_here():
    """A category left out of the table is left out of "France" too.

    Otherwise 35-49 at 54.5% of the shown voters sat next to 23.5% of the
    whole population, an over-representation that isn't there.
    """
    from app.crud.demographics import Breakdown, BreakdownRow, _same_date_shares

    def row(category, total, share, reference):
        counts = {"for": total, "against": 0, "abstain": 0}
        return BreakdownRow(category, counts, total, {}, share=share, reference=reference)

    age = Breakdown("age", "Tranche d'âge", shown=True, threshold=10)
    age.rows = [row("35-49", 60, 54.5, 23.5), row("65+", 50, 45.5, 27.8)]
    _same_date_shares(age)
    assert [(r.share, r.reference) for r in age.rows] == [(54.5, 45.8), (45.5, 54.2)]

    sexe = Breakdown("sexe", "Sexe", shown=True, threshold=10)
    sexe.rows = [row("femme", 40, 80.0, 51.5), row("autre", 10, 20.0, None)]
    _same_date_shares(sexe)
    assert [r.reference for r in sexe.rows] == [None, None], "femme alone would read 100%"

    # "autre" has no national figure but counts in "Part ici": 45/45 against
    # 51.5/48.5 made both sexes look under-represented.
    sexe.rows = [
        row("femme", 135, 45.0, 51.5),
        row("homme", 135, 45.0, 48.5),
        row("autre", 30, 10.0, None),
    ]
    _same_date_shares(sexe)
    assert [r.reference for r in sexe.rows] == [46.4, 43.6, None]
