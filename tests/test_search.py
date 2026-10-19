"""Search and the evolution curve."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import event

from app.config import get_settings
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.models import Law, Vote
from tests.helpers import make_law, publish_laws


def test_search_matches_summary_and_title(db_session):
    db_session.add_all(
        [
            Law(slug="a", title="Loi sur le climat", summary_text="énergie", published=True),
            Law(slug="b", title="Réforme fiscale", summary_text="impôts", published=True),
        ]
    )
    db_session.commit()
    found = laws_crud.search(db_session, query="climat")
    assert found.total == 1
    assert found.rows[0].slug == "a"


def test_search_status_filter(db_session, open_law, closed_law):
    found = laws_crud.search(db_session, status="closed")
    assert [law.slug for law in found.rows] == ["loi-close"]


def test_evolution_hidden_below_min_volume(db_session, open_law):
    db_session.add_all(
        [Vote(law_id=open_law.id, voter_token=f"t{i}", choice="for") for i in range(5)]
    )
    db_session.commit()
    # Default MIN_BUCKET_VOLUME (50) >> 5 ⇒ no buckets exposed.
    assert results_crud.evolution(db_session, open_law.id) == []


def test_evolution_shown_above_min_volume(db_session, open_law, monkeypatch):
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 3)
    db_session.add_all(
        [Vote(law_id=open_law.id, voter_token=f"t{i}", choice="for") for i in range(4)]
    )
    db_session.commit()
    buckets = results_crud.evolution(db_session, open_law.id)
    assert buckets
    assert sum(b.counts["for"] for b in buckets) == 4


# --- Search ------------------------------------------------------------------


def test_search_pages_in_sql_not_in_python(db_session):
    """Search pagination is done in SQL."""
    publish_laws(db_session, 45)
    seen: list[str] = []
    bind = db_session.get_bind()

    def record(conn, cursor, statement, params, context, many):
        seen.append(" ".join(statement.split()))

    event.listen(bind, "before_cursor_execute", record)
    try:
        found = laws_crud.search(db_session, page=2, page_size=20)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert found.total == 45
    assert len(found.rows) == 20
    selects = [s for s in seen if s.upper().startswith("SELECT")]
    assert any("LIMIT" in s for s in selects), "the window must be applied by the database"


def test_search_is_ordered_like_the_home_page(db_session):
    """Search uses the same order as the home page (promulgation date).

    Inserted so that creation order and promulgation order differ.
    """
    make_law(db_session, slug="loi-recente", title="Loi commune", promulgated_on=date(2026, 8, 20))
    make_law(db_session, slug="loi-ancienne", title="Loi commune", promulgated_on=date(2026, 1, 5))

    found = laws_crud.search(db_session, query="commune")
    assert [law.slug for law in found.rows] == ["loi-recente", "loi-ancienne"]


def test_search_by_status_filters_correctly(db_session, open_law, closed_law):
    opened = laws_crud.search(db_session, status="open").rows
    closed = laws_crud.search(db_session, status="closed").rows
    assert [law.slug for law in opened] == [open_law.slug]
    assert [law.slug for law in closed] == [closed_law.slug]


def test_a_wildcard_typed_in_the_box_never_matches_everything(db_session):
    """LIKE wildcards typed in the search box don't match everything."""
    db_session.add(Law(slug="pourcent", title="Loi 100 % renouvelable", published=True))
    db_session.add(Law(slug="autre", title="Loi ordinaire", published=True))
    db_session.commit()

    found = laws_crud.search(db_session, query="100 %")
    assert found.total == 1
    assert found.rows[0].slug == "pourcent"

    # A bare "%" is not "match everything".
    assert laws_crud.search(db_session, query="%").total == 0

    # The reference uses LIKE: "2026_214" must not match "2026-214".
    db_session.add(
        Law(slug="ref", title="Texte référencé", official_ref="LOI n° 2026-214", published=True)
    )
    db_session.commit()
    assert laws_crud.search(db_session, query="2026-214").total == 1
    assert laws_crud.search(db_session, query="2026_214").total == 0


# --- Full-text search ----------------------------------------------------------


def test_search_finds_a_plural_from_the_singular(client, db_session):
    make_law(db_session, slug="loi-retraites", title="Réforme des retraites", summary_text="Texte.")
    assert laws_crud.search(db_session, query="retraite").total == 1
    assert laws_crud.search(db_session, query="retraites").total == 1


def test_search_matches_a_word_still_being_typed(client, db_session):
    make_law(db_session, slug="loi-logement", title="Encadrement des loyers", summary_text="Texte.")
    assert laws_crud.search(db_session, query="loye").total == 1
    assert laws_crud.search(db_session, query="encadr").total == 1


def test_search_still_finds_a_law_by_its_official_number(client, db_session):
    make_law(db_session, slug="loi-num", title="Un texte", official_ref="LOI n° 2026-214")
    assert laws_crud.search(db_session, query="2026-214").total == 1


def test_search_requires_every_word(client, db_session):
    make_law(db_session, slug="loi-a1", title="Réforme des retraites", summary_text="Texte.")
    make_law(db_session, slug="loi-b1", title="Réforme du logement", summary_text="Texte.")
    assert laws_crud.search(db_session, query="réforme").total == 2
    assert laws_crud.search(db_session, query="réforme retraite").total == 1


def test_search_ignores_punctuation_a_visitor_types(client, db_session):
    """Punctuation is ignored (to_tsquery would fail on it)."""
    make_law(db_session, slug="loi-ponct", title="Loi sur l'eau", summary_text="Texte.")
    for query in ("l'eau", "eau !", "(eau)", "eau & | :"):
        assert laws_crud.search(db_session, query=query).total == 1, query


def test_the_curve_keeps_the_days_nobody_voted(db_session, open_law, monkeypatch):
    """Days without votes are kept as zeros, so spikes stand out."""
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 3, raising=False)
    monkeypatch.setattr(get_settings(), "min_cell_count", 3, raising=False)
    base = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    # Two choices each day, so neither day needs merging.
    for i in range(3):
        db_session.add(
            Vote(
                law_id=open_law.id,
                voter_token=f"a{i}",
                choice="against" if i == 0 else "for",
                created_at=base,
            )
        )
    for i in range(3):
        db_session.add(
            Vote(
                law_id=open_law.id,
                voter_token=f"b{i}",
                choice="against" if i == 0 else "for",
                created_at=base + timedelta(days=4),
            )
        )
    db_session.commit()

    buckets = results_crud.evolution(db_session, open_law.id)
    assert [b.label for b in buckets] == [
        "2026-05-04",
        "2026-05-05",
        "2026-05-06",
        "2026-05-07",
        "2026-05-08",
    ]
    assert [sum(b.counts.values()) for b in buckets] == [3, 0, 0, 0, 3]


def test_the_curve_does_not_invent_days_outside_the_votes(db_session, open_law, monkeypatch):
    """The curve starts and ends with the first and last vote."""
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 3, raising=False)
    moment = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
    for i in range(4):
        db_session.add(
            Vote(law_id=open_law.id, voter_token=f"c{i}", choice="for", created_at=moment)
        )
    db_session.commit()

    buckets = results_crud.evolution(db_session, open_law.id)
    assert [b.label for b in buckets] == ["2026-05-04"]


def _votes_on(db_session, law, day: int, choice: str, n: int) -> None:
    """``n`` votes at noon UTC on that day of May 2026."""
    moment = datetime(2026, 5, day, 12, 0, tzinfo=UTC)
    for i in range(n):
        db_session.add(
            Vote(
                law_id=law.id, voter_token=f"q{day}-{choice}-{i}", choice=choice, created_at=moment
            )
        )
    db_session.commit()


def test_a_quiet_day_is_merged_until_it_hides_its_voters(db_session, open_law, monkeypatch):
    """A day with too few votes is merged with the next ones.

    One "Contre" on a quiet day would tell anyone who knows that person voted
    that day how they voted.
    """
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 3, raising=False)
    monkeypatch.setattr(get_settings(), "min_cell_count", 10, raising=False)
    _votes_on(db_session, open_law, 4, "for", 18)
    _votes_on(db_session, open_law, 4, "against", 2)
    _votes_on(db_session, open_law, 8, "against", 1)
    _votes_on(db_session, open_law, 10, "for", 9)

    buckets = results_crud.evolution(db_session, open_law.id)
    assert [(b.label, b.days, sum(b.counts.values())) for b in buckets] == [
        ("2026-05-04", 1, 20),
        ("2026-05-05", 1, 0),
        ("2026-05-06", 1, 0),
        ("2026-05-07", 1, 0),
        ("2026-05-08", 3, 10),
    ]
    assert buckets[-1].counts == {"for": 9, "against": 1, "abstain": 0}
    assert buckets[-1].daily == [1, 0, 9], "each day keeps its number of votes"


def test_a_day_of_one_choice_is_merged_too(db_session, open_law, monkeypatch):
    """Twelve "Pour" in a day tell anyone who knows someone voted that day how they voted."""
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 3, raising=False)
    monkeypatch.setattr(get_settings(), "min_cell_count", 10, raising=False)
    _votes_on(db_session, open_law, 4, "for", 18)
    _votes_on(db_session, open_law, 4, "against", 2)
    # The 5th has enough votes, all "Pour"; the 6th could stand on its own.
    _votes_on(db_session, open_law, 5, "for", 12)
    _votes_on(db_session, open_law, 6, "for", 9)
    _votes_on(db_session, open_law, 6, "abstain", 1)

    buckets = results_crud.evolution(db_session, open_law.id)
    assert [(b.label, b.days, b.daily) for b in buckets] == [
        ("2026-05-04", 1, [20]),
        ("2026-05-05", 2, [12, 10]),
    ]
    assert buckets[-1].counts == {"for": 21, "against": 0, "abstain": 1}


def test_quiet_days_at_the_end_join_the_last_period(client, db_session, open_law, monkeypatch):
    """Leftover quiet days join the period before, so every period adds up.

    Left alone, the tally minus the other periods would give them back.
    """
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 3, raising=False)
    monkeypatch.setattr(get_settings(), "min_cell_count", 10, raising=False)
    _votes_on(db_session, open_law, 4, "for", 20)
    _votes_on(db_session, open_law, 6, "against", 1)

    buckets = results_crud.evolution(db_session, open_law.id)
    assert [(b.label, b.days) for b in buckets] == [("2026-05-04", 3)]
    assert buckets[0].counts == {"for": 20, "against": 1, "abstain": 0}
    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "du 4 mai 2026 au 6 mai 2026" in page, "the table names the merged period"
