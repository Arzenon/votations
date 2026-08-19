"""Privacy behaviour: what is stored, when, and what an export gives away."""

from __future__ import annotations

from types import SimpleNamespace

from app import profile_cookie
from app.auth.anonymous import COOKIE_NAME
from app.crud import demographics as demo_crud
from app.models import DemographicTally, Vote

# --- Nothing is issued to someone who only reads ------------------------------


def test_reading_a_law_sets_no_cookie(client, open_law):
    """A year-long identifier for a visitor who never votes is not "no data"."""
    r = client.get(f"/loi/{open_law.slug}")
    assert r.status_code == 200
    assert COOKIE_NAME not in r.cookies
    assert not any(c.startswith(COOKIE_NAME) for c in r.headers.get_list("set-cookie"))


def test_reading_the_home_and_results_pages_sets_no_cookie(client, open_law):
    for path in ("/", "/recherche", f"/loi/{open_law.slug}/resultats"):
        r = client.get(path)
        assert not r.headers.get_list("set-cookie"), path


def test_voting_is_what_issues_the_token(client, open_law):
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303
    assert any(c.startswith(COOKIE_NAME) for c in r.headers.get_list("set-cookie"))


def test_a_voter_still_sees_their_own_vote_afterwards(client, open_law):
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})
    page = client.get(f"/loi/{open_law.slug}")
    assert "Modifier mon vote" in page.text
    assert 'value="against"' in page.text.split("Modifier mon vote")[0]


# --- Demographic counters follow a changed vote -------------------------------


def _request_with(cookie: str) -> SimpleNamespace:
    """The one attribute `profile_cookie.read` looks at."""
    return SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: cookie})


def _cells(db_session, law_id) -> dict[tuple[str, str, str], int]:
    rows = db_session.query(DemographicTally).filter_by(law_id=law_id).all()
    return {(r.dimension, r.category, r.choice): r.count for r in rows}


def test_changing_a_vote_moves_the_profile_with_it(client, open_law, db_session):
    """The breakdown used to keep counting a voter under the choice they left."""
    client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for", "age_bracket": "35-49", "sexe": "femme"},
    )
    assert _cells(db_session, open_law.id)[("age", "35-49", "for")] == 1

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})

    cells = _cells(db_session, open_law.id)
    assert cells.get(("age", "35-49", "for"), 0) == 0
    assert cells[("age", "35-49", "against")] == 1
    assert cells.get(("sexe", "femme", "for"), 0) == 0
    assert cells[("sexe", "femme", "against")] == 1


def test_the_breakdown_totals_match_the_tally(client, open_law, db_session):
    """The discrepancy this prevents is exactly what fuels "the numbers are rigged"."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "region": "Bretagne"})
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "abstain"})

    cells = _cells(db_session, open_law.id)
    region_total = sum(v for (dim, _, _), v in cells.items() if dim == "region")
    assert region_total == 1
    assert cells[("region", "Bretagne", "abstain")] == 1


def test_the_profile_is_counted_once_however_often_the_vote_changes(client, open_law, db_session):
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "age_bracket": "25-34"})
    for choice in ("against", "abstain", "for", "against"):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": choice})

    cells = _cells(db_session, open_law.id)
    assert sum(v for (dim, _, _), v in cells.items() if dim == "age") == 1
    assert cells[("age", "25-34", "against")] == 1


def test_no_counter_can_go_negative(client, open_law, db_session):
    """Even a browser replaying an old profile cookie cannot drive a cell below zero."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "sexe": "homme"})
    # A second, unrelated voter contributes nothing to the "homme/for" cell.
    for choice in ("against", "for", "against", "for", "against"):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": choice})
    assert all(count >= 0 for count in _cells(db_session, open_law.id).values())


def test_the_server_still_stores_no_profile_per_voter(client, open_law, db_session):
    """The whole point: counters only, and a choice — never a category — on the vote."""
    client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for", "age_bracket": "50-64", "sexe": "femme", "region": "Corse"},
    )
    vote = db_session.query(Vote).filter_by(law_id=open_law.id).one()
    stored = {column.name: getattr(vote, column.name) for column in vote.__table__.columns}
    assert "50-64" not in str(stored)
    assert "femme" not in str(stored)
    assert "Corse" not in str(stored)
    assert stored["demographics_recorded"] is True
    assert stored["demographics_choice"] == "for"


def test_the_profile_cookie_holds_categories_and_never_the_vote(client, open_law):
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "against", "age_bracket": "18-24"},
        follow_redirects=False,
    )
    raw = next(
        c for c in r.headers.get_list("set-cookie") if c.startswith(profile_cookie.COOKIE_NAME)
    )
    assert "HttpOnly" in raw
    stored = client.cookies.get(profile_cookie.COOKIE_NAME)
    assert stored is not None
    # Signed, so its content is opaque to the page and unforgeable in transit...
    assert "18-24" not in stored
    # ...and once read back, it carries the categories alone.
    assert profile_cookie.read(_request_with(stored)) == {"age": "18-24"}


def test_a_tampered_profile_cookie_is_ignored():
    assert profile_cookie.read(_request_with("n-importe-quoi")) == {}


def test_an_invalid_category_is_never_recorded(client, open_law, db_session):
    client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for", "age_bracket": "150-200", "sexe": "inventé"},
    )
    assert _cells(db_session, open_law.id) == {}


def test_without_a_cookie_nothing_moves_and_nothing_breaks(open_law, db_session):
    """Cleared cookies simply leave an old contribution where it was."""
    from app.crud import votes as votes_crud
    from app.models import VoteChoice

    vote = votes_crud.cast_vote(db_session, open_law, "jeton", VoteChoice.FOR)
    demo_crud.record(db_session, vote=vote, choice=VoteChoice.FOR, age_bracket="65+")
    assert _cells(db_session, open_law.id)[("age", "65+", "for")] == 1

    vote = votes_crud.cast_vote(db_session, open_law, "jeton", VoteChoice.AGAINST)
    demo_crud.record(db_session, vote=vote, choice=VoteChoice.AGAINST, previous={})

    cells = _cells(db_session, open_law.id)
    assert cells[("age", "65+", "for")] == 1
    assert vote.demographics_choice == "against"
