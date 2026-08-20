"""V1: advanced search filtering and anti-manipulation evolution gating."""

from app.config import get_settings
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.models import Law, Vote


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
