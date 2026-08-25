"""Voting flow: dedup, changeability, closed-law rejection, results math."""

import re

from app import ratelimit
from app.config import get_settings
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
        [Vote(law_id=open_law.id, voter_token=f"t{i}", choice="for") for i in range(3)]
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



# --- A refused vote must say so, with JavaScript on as well as off ------------

HX = {"HX-Request": "true"}


def _is_whole_page(text: str) -> bool:
    return "<!DOCTYPE html>" in text


def test_htmx_vote_on_a_law_that_just_closed_returns_a_ballot_not_a_page(client, closed_law):
    """htmx follows a 303 and swaps whatever it lands on into `#ballot`.

    So the redirect nested the entire site — header, disclaimer, footer — inside
    the ballot's div.
    """
    r = client.post(f"/loi/{closed_law.slug}/vote", data={"choice": "for"}, headers=HX)
    assert r.status_code == 200
    assert not _is_whole_page(r.text)
    assert 'id="ballot"' in r.text
    assert "vient de se clore" in r.text


def test_htmx_vote_without_a_choice_explains_itself(client, open_law):
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": ""}, headers=HX)
    assert r.status_code == 200
    assert not _is_whole_page(r.text)
    assert "Choisissez" in r.text


def test_htmx_vote_over_the_rate_limit_is_not_silent(client, open_law, monkeypatch):
    """htmx discards the body of a 4xx, so the button appeared to do nothing."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"}, headers=HX)
    assert r.status_code == 200
    assert not _is_whole_page(r.text)
    assert "Trop de votes" in r.text


def _checked_choice(html: str) -> str | None:
    """Which radio the rendered ballot has pre-selected, if any."""
    match = re.search(r'value="(\w+)"[^>]*\bchecked\b', html)
    return match.group(1) if match else None


def test_a_refused_vote_keeps_the_choice_already_recorded(client, open_law, monkeypatch):
    """A refusal must not silently clear the radio the voter had chosen."""
    cast = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"}, headers=HX)
    assert _checked_choice(cast.text) == "against"

    monkeypatch.setattr(get_settings(), "vote_rate_limit", 0, raising=False)
    ratelimit.reset()
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    # The first attempt in the window is allowed and spends the allowance; the
    # second is refused, and is the one whose ballot has to be right.
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "abstain"}, headers=HX)

    assert "Trop de votes" in refused.text
    assert _checked_choice(refused.text) == "for", (
        "the refusal must show what is on file, not the rejected attempt"
    )


def test_without_htmx_a_refused_vote_still_gets_the_ordinary_answer(client, closed_law, open_law):
    """The fragment is an addition, not a replacement: no JavaScript, no change."""
    r = client.post(f"/loi/{closed_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/loi/{closed_law.slug}"

    r = client.post("/loi/inconnue/vote", data={"choice": "for"})
    assert r.status_code == 404


# --- Concurrency --------------------------------------------------------------


def test_voting_twice_at_once_updates_instead_of_failing(db_session, open_law):
    """Read-then-insert raced against itself and hit the unique constraint."""
    token = "meme-navigateur"
    first = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.FOR)
    second = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.AGAINST)
    assert first.id == second.id
    assert second.choice == "against"
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 1
