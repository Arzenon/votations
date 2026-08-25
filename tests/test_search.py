"""V1: advanced search filtering and anti-manipulation evolution gating."""


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
    rows, total, _pages = laws_crud.search(db_session, query="climat")
    assert total == 1
    assert rows[0].slug == "a"


def test_search_status_filter(db_session, open_law, closed_law):
    rows, _total, _pages = laws_crud.search(db_session, status="closed")
    assert [law.slug for law in rows] == ["loi-close"]


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
    """A page used to be sliced out of every published law loaded into memory."""
    publish_laws(db_session, 45)
    seen: list[str] = []
    bind = db_session.get_bind()

    def record(conn, cursor, statement, params, context, many):
        seen.append(" ".join(statement.split()))

    event.listen(bind, "before_cursor_execute", record)
    try:
        rows, total, _pages = laws_crud.search(db_session, page=2, page_size=20)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert total == 45
    assert len(rows) == 20
    selects = [s for s in seen if s.upper().startswith("SELECT")]
    assert any("LIMIT" in s for s in selects), "the window must be applied by the database"


def test_search_by_status_filters_correctly(db_session, open_law, closed_law):
    opened, _, _pages = laws_crud.search(db_session, status="open")
    closed, _, _pages = laws_crud.search(db_session, status="closed")
    assert [law.slug for law in opened] == [open_law.slug]
    assert [law.slug for law in closed] == [closed_law.slug]


def test_a_wildcard_typed_in_the_box_never_matches_everything(db_session):
    """LIKE wildcards must not survive into the query as wildcards.

    Search is full-text now, so punctuation is not indexed and a bare « % » finds
    nothing rather than finding the law that contains one. What matters is
    unchanged and still asserted: neither wildcard can be used to match the whole
    corpus, and the words around them are searched as words.
    """
    db_session.add(Law(slug="pourcent", title="Loi 100 % renouvelable", published=True))
    db_session.add(Law(slug="autre", title="Loi ordinaire", published=True))
    db_session.commit()

    hits, total, _pages = laws_crud.search(db_session, query="100 %")
    assert total == 1
    assert hits[0].slug == "pourcent"

    # A bare "%" is not "match everything".
    assert laws_crud.search(db_session, query="%")[1] == 0

    # The official reference is still matched with LIKE, so both wildcards have
    # to stay literal there: "2026_214" must not match "2026-214".
    db_session.add(
        Law(slug="ref", title="Texte référencé", official_ref="LOI n° 2026-214", published=True)
    )
    db_session.commit()
    assert laws_crud.search(db_session, query="2026-214")[1] == 1
    assert laws_crud.search(db_session, query="2026_214")[1] == 0


# --- Recherche plein texte ----------------------------------------------------


def test_search_finds_a_plural_from_the_singular(client, db_session):
    make_law(db_session, slug="loi-retraites", title="Réforme des retraites", summary_text="Texte.")
    assert laws_crud.search(db_session, query="retraite")[1] == 1
    assert laws_crud.search(db_session, query="retraites")[1] == 1


def test_search_matches_a_word_still_being_typed(client, db_session):
    make_law(db_session, slug="loi-logement", title="Encadrement des loyers", summary_text="Texte.")
    assert laws_crud.search(db_session, query="loye")[1] == 1
    assert laws_crud.search(db_session, query="encadr")[1] == 1


def test_search_still_finds_a_law_by_its_official_number(client, db_session):
    make_law(db_session, slug="loi-num", title="Un texte", official_ref="LOI n° 2026-214")
    assert laws_crud.search(db_session, query="2026-214")[1] == 1


def test_search_requires_every_word(client, db_session):
    make_law(db_session, slug="loi-a1", title="Réforme des retraites", summary_text="Texte.")
    make_law(db_session, slug="loi-b1", title="Réforme du logement", summary_text="Texte.")
    assert laws_crud.search(db_session, query="réforme")[1] == 2
    assert laws_crud.search(db_session, query="réforme retraite")[1] == 1


def test_search_ignores_punctuation_a_visitor_types(client, db_session):
    """`to_tsquery` would treat these as syntax and fail the whole query."""
    make_law(db_session, slug="loi-ponct", title="Loi sur l'eau", summary_text="Texte.")
    for query in ("l'eau", "eau !", "(eau)", "eau & | :"):
        assert laws_crud.search(db_session, query=query)[1] == 1, query
