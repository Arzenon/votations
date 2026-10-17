"""Voting flow: dedup, changeability, closed-law rejection, results math."""

import re

from markupsafe import escape

from app import ratelimit
from app.config import get_settings
from app.crud import results as results_crud
from app.crud import votes as votes_crud
from app.models import Vote, VoteChoice
from app.routes.public.laws import NO_CHOICE_NOTICE, TOO_MANY_NOTICE


def test_one_vote_per_voter_then_changeable(client, open_law, db_session):
    # First vote.
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303
    assert db_session.query(Vote).count() == 1

    # Same browser votes again: updated, not duplicated.
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
    """htmx would follow a redirect and swap the whole page into `#ballot`."""
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
    """htmx ignores the body of a 4xx, so the button would seem to do nothing."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"}, headers=HX)
    assert r.status_code == 200
    assert not _is_whole_page(r.text)
    # Escaped like the template does (apostrophes become entities).
    assert escape(TOO_MANY_NOTICE) in r.text


def _checked_choice(html: str) -> str | None:
    """The pre-selected radio in the ballot, if any."""
    match = re.search(r'value="(\w+)"[^>]*\bchecked\b', html)
    return match.group(1) if match else None


def test_a_refused_vote_keeps_the_choice_already_recorded(client, open_law, monkeypatch):
    """A refusal keeps the radio the voter had chosen."""
    cast = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"}, headers=HX)
    assert _checked_choice(cast.text) == "against"

    monkeypatch.setattr(get_settings(), "vote_rate_limit", 0, raising=False)
    ratelimit.reset()
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    # The first attempt uses the allowance, the second is refused.
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    refused = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "abstain"}, headers=HX)

    assert escape(TOO_MANY_NOTICE) in refused.text
    assert _checked_choice(refused.text) == "for", (
        "the refusal must show what is on file, not the rejected attempt"
    )


def test_without_htmx_a_refused_vote_still_gets_the_ordinary_answer(client, closed_law, open_law):
    """Without JavaScript, refusals still redirect or 404."""
    r = client.post(f"/loi/{closed_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/loi/{closed_law.slug}"

    r = client.post("/loi/inconnue/vote", data={"choice": "for"})
    assert r.status_code == 404


# --- Concurrency --------------------------------------------------------------


def test_voting_twice_at_once_updates_instead_of_failing(db_session, open_law):
    """A second ballot updates the vote instead of hitting the unique constraint."""
    token = "meme-navigateur"
    first, first_previous = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.FOR)
    second, second_previous = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.AGAINST)
    assert first.id == second.id
    assert second.choice == "against"
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 1
    # The previous choice is returned, for the demographic counters.
    assert first_previous is None
    assert second_previous == "for"


# --- Choice labels ---------------------------------------------------------------


def test_every_choice_has_a_label():
    """Every choice has a label, in ballot order."""
    from app.labels import CHOICE_LABELS
    from app.models import VoteChoice

    assert set(CHOICE_LABELS) == {c.value for c in VoteChoice}
    assert list(CHOICE_LABELS) == [c.value for c in VoteChoice], "ballot order"
    assert all(label for label in CHOICE_LABELS.values())


def test_the_ballot_and_the_results_name_the_choices_the_same_way(client, closed_law, db_session):
    """The results page uses the same labels as the ballot."""
    from app.labels import CHOICE_LABELS

    db_session.add(Vote(law_id=closed_law.id, voter_token="t", choice="for"))
    db_session.commit()

    page = client.get(f"/loi/{closed_law.slug}/resultats").text
    for label in CHOICE_LABELS.values():
        assert label in page, label


# --- A refused ballot keeps what was entered -------------------------------------


def test_a_refused_ballot_hands_back_the_profile_that_was_typed(client, open_law):
    """A refused ballot keeps the profile the voter selected."""
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        headers=HX,
        data={"choice": "", "age_bracket": "35-49", "sexe": "femme", "region": "Bretagne"},
    )
    assert r.status_code == 200
    assert escape(NO_CHOICE_NOTICE) in r.text
    assert '<option value="35-49" selected>' in r.text
    assert '<option value="femme" selected>' in r.text
    assert '<option value="Bretagne" selected>' in r.text
    assert r.text.count(" selected>") == 3, "only what was chosen comes back selected"


def test_a_rate_limited_ballot_hands_it_back_too(client, open_law, monkeypatch):
    """Same when the refusal comes from the rate limit."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    # First ballot without a profile, so the dropdowns are still shown.
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "against", "age_bracket": "25-34"},
        headers=HX,
    )
    assert escape(TOO_MANY_NOTICE) in r.text
    assert '<option value="25-34" selected>' in r.text


def test_a_category_this_site_does_not_know_is_never_echoed_back(client, open_law):
    """Only known options can be marked as selected."""
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        headers=HX,
        data={"choice": "", "age_bracket": "150-200", "sexe": "<script>"},
    )
    assert " selected>" not in r.text
    assert "150-200" not in r.text
    assert "<script>" not in r.text


def test_an_untouched_ballot_selects_nothing(client, open_law):
    r = client.get(f"/loi/{open_law.slug}")
    assert " selected>" not in r.text
