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
    # Escaped, because that is how the template emits it — the apostrophes in the
    # French copy become entities. Comparing against the constant rather than a
    # copy of its wording keeps this a test of the behaviour, not of the phrasing.
    assert escape(TOO_MANY_NOTICE) in r.text


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

    assert escape(TOO_MANY_NOTICE) in refused.text
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
    first, first_previous = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.FOR)
    second, second_previous = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.AGAINST)
    assert first.id == second.id
    assert second.choice == "against"
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 1
    # And the second ballot reports what it replaced, which is what lets the
    # demographic counters move without the row having to remember it.
    assert first_previous is None
    assert second_previous == "for"


# --- Les trois choix, écrits une fois -------------------------------------------


def test_every_choice_has_a_label():
    """A choice with no label is a column that simply stops being drawn.

    The mapping used to exist in three copies — the chart legend, the results
    bars, and an if/elif chain in the ballot whose `else` was « Abstention ». A
    fourth choice would have been *offered to voters* under that label.
    """
    from app.labels import CHOICE_LABELS
    from app.models import VoteChoice

    assert set(CHOICE_LABELS) == {c.value for c in VoteChoice}
    assert list(CHOICE_LABELS) == [c.value for c in VoteChoice], "ballot order"
    assert all(label for label in CHOICE_LABELS.values())


def test_the_ballot_and_the_results_name_the_choices_the_same_way(client, closed_law, db_session):
    """One mapping, so the ballot and the tables cannot disagree about a word."""
    from app.labels import CHOICE_LABELS

    db_session.add(Vote(law_id=closed_law.id, voter_token="t", choice="for"))
    db_session.commit()

    page = client.get(f"/loi/{closed_law.slug}/resultats").text
    for label in CHOICE_LABELS.values():
        assert label in page, label


# --- Un bulletin refusé rend ce qui avait été saisi ------------------------------


def test_a_refused_ballot_hands_back_the_profile_that_was_typed(client, open_law):
    """The profile is typed on the same submit as the vote, and was being cleared.

    A voter who forgets to choose gets their ballot back with a message — and had
    three dropdowns silently reset to « Ne pas renseigner », with nothing saying
    they had to fill them in again.
    """
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
    """The refusal a voter is least at fault for — an address is not a person."""
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    ratelimit.reset()

    # A first ballot with no profile: it spends the allowance without recording a
    # contribution, so the dropdowns are still on offer for the second.
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, headers=HX)
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "against", "age_bracket": "25-34"},
        headers=HX,
    )
    assert escape(TOO_MANY_NOTICE) in r.text
    assert '<option value="25-34" selected>' in r.text


def test_a_category_this_site_does_not_know_is_never_echoed_back(client, open_law):
    """`selected` is decided by matching an option, so nothing else can be marked."""
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
