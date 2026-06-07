"""Voting flow: dedup, changeability, closed-law rejection, results math."""

from app.crud import results as results_crud
from app.crud import votes as votes_crud
from app.models import Vote, VoteChoice


def test_one_vote_per_voter_then_changeable(client, open_law, db_session):
    # First vote.
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303
    assert db_session.query(Vote).count() == 1

    # Same browser (cookie persisted) votes again → updates, not duplicates.
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})
    assert db_session.query(Vote).count() == 1
    assert db_session.query(Vote).one().choice == "against"


def test_distinct_voters_counted_separately(open_law, db_session):
    from fastapi.testclient import TestClient

    from app.main import app

    c1 = TestClient(app)
    c2 = TestClient(app)
    c1.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    c2.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})
    assert db_session.query(Vote).count() == 2


def test_cannot_vote_on_closed_law(client, closed_law, db_session):
    r = client.post(f"/loi/{closed_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303  # redirected back, no vote recorded
    assert db_session.query(Vote).count() == 0


def test_cast_vote_raises_when_closed(closed_law, db_session):
    import pytest

    with pytest.raises(votes_crud.VotingClosed):
        votes_crud.cast_vote(db_session, closed_law, "tok", VoteChoice.FOR)


def test_results_math(open_law, db_session):
    db_session.add_all(
        [
            Vote(law_id=open_law.id, voter_token=f"t{i}", choice="for")
            for i in range(3)
        ]
        + [Vote(law_id=open_law.id, voter_token="a", choice="against")]
    )
    db_session.commit()
    tally = results_crud.tally(db_session, open_law.id)
    assert tally.total == 4
    assert tally.counts == {"for": 3, "against": 1, "abstain": 0}
    assert tally.percentages["for"] == 75.0
    assert tally.percentages["against"] == 25.0


def test_htmx_vote_returns_fragment(client, open_law):
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert 'id="ballot"' in r.text
    assert "enregistré" in r.text
