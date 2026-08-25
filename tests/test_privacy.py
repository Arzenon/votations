"""Privacy behaviour: what is stored, when, and what an export gives away."""

from __future__ import annotations

from types import SimpleNamespace

from itsdangerous import URLSafeSerializer

from app import profile_cookie
from app.auth.anonymous import COOKIE_NAME
from app.config import get_settings
from app.crud import demographics as demo_crud
from app.crud import votes as votes_crud
from app.models import Vote, VoteChoice
from tests.helpers import demographic_cells, make_law

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
    """The one attribute `profile_cookie.load` looks at."""
    return SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: cookie})


def _stored_for(cookie: str, law_id: int) -> dict[str, str]:
    """What the cookie says this browser contributed to one law."""
    return profile_cookie.load(_request_with(cookie)).for_law(law_id)


def test_changing_a_vote_moves_the_profile_with_it(client, open_law, db_session):
    """The breakdown used to keep counting a voter under the choice they left."""
    client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for", "age_bracket": "35-49", "sexe": "femme"},
    )
    assert demographic_cells(db_session, open_law.id)[("age", "35-49", "for")] == 1

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})

    cells = demographic_cells(db_session, open_law.id)
    assert cells.get(("age", "35-49", "for"), 0) == 0
    assert cells[("age", "35-49", "against")] == 1
    assert cells.get(("sexe", "femme", "for"), 0) == 0
    assert cells[("sexe", "femme", "against")] == 1


def test_the_breakdown_totals_match_the_tally(client, open_law, db_session):
    """The discrepancy this prevents is exactly what fuels "the numbers are rigged"."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "region": "Bretagne"})
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "abstain"})

    cells = demographic_cells(db_session, open_law.id)
    region_total = sum(v for (dim, _, _), v in cells.items() if dim == "region")
    assert region_total == 1
    assert cells[("region", "Bretagne", "abstain")] == 1


def test_the_profile_is_counted_once_however_often_the_vote_changes(client, open_law, db_session):
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "age_bracket": "25-34"})
    for choice in ("against", "abstain", "for", "against"):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": choice})

    cells = demographic_cells(db_session, open_law.id)
    assert sum(v for (dim, _, _), v in cells.items() if dim == "age") == 1
    assert cells[("age", "25-34", "against")] == 1


def test_no_counter_can_go_negative(client, open_law, db_session):
    """Even a browser replaying an old profile cookie cannot drive a cell below zero."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "sexe": "homme"})
    # A second, unrelated voter contributes nothing to the "homme/for" cell.
    for choice in ("against", "for", "against", "for", "against"):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": choice})
    assert all(count >= 0 for count in demographic_cells(db_session, open_law.id).values())


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
    assert _stored_for(stored, open_law.id) == {"age": "18-24"}


def test_a_tampered_profile_cookie_is_ignored():
    assert _stored_for("n-importe-quoi", 1) == {}


def test_a_cookie_from_before_the_per_law_split_is_ignored():
    """The old format held one profile for the whole site; it must not be trusted.

    Its values are categories rather than per-law maps, so every entry is dropped
    and the browser behaves as if it had no cookie: nothing moves, which is the
    documented safe fallback.
    """
    from itsdangerous import URLSafeSerializer

    from app.config import get_settings

    old = URLSafeSerializer(get_settings().secret_key, salt="voter-profile").dumps(
        {"age": "18-24", "sexe": "femme"}
    )
    assert _stored_for(old, 1) == {}


def test_an_invalid_category_is_never_recorded(client, open_law, db_session):
    client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for", "age_bracket": "150-200", "sexe": "inventé"},
    )
    assert demographic_cells(db_session, open_law.id) == {}


def test_without_a_cookie_nothing_moves_and_nothing_breaks(open_law, db_session):
    """Cleared cookies simply leave an old contribution where it was."""

    vote = votes_crud.cast_vote(db_session, open_law, "jeton", VoteChoice.FOR)
    demo_crud.record(db_session, vote=vote, choice=VoteChoice.FOR, age_bracket="65+")
    assert demographic_cells(db_session, open_law.id)[("age", "65+", "for")] == 1

    vote = votes_crud.cast_vote(db_session, open_law, "jeton", VoteChoice.AGAINST)
    demo_crud.record(db_session, vote=vote, choice=VoteChoice.AGAINST, previous={})

    cells = demographic_cells(db_session, open_law.id)
    assert cells[("age", "65+", "for")] == 1
    # And the tracker keeps pointing at the choice the counter actually sits
    # under. Advancing it would make the column claim the contribution had moved
    # when it had not, so a later change with a restored cookie would decrement
    # the wrong cell.
    assert vote.demographics_choice == "for"


def test_a_profile_given_on_one_law_never_moves_another_laws_counters(client, db_session):
    """The cookie held one profile for the whole site while counters are per-law.

    Voting 18-24 on A then 65+ on B, then changing the vote on A, used to
    decrement A's 65+ cell — other people's votes — and strand the real 18-24
    contribution under the old choice.
    """
    a = make_law(db_session, slug="loi-a", title="Loi A")
    b = make_law(db_session, slug="loi-b", title="Loi B")

    client.post(f"/loi/{a.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    client.post(f"/loi/{b.slug}/vote", data={"choice": "for", "age_bracket": "65+"})

    # Somebody else's vote, so a stray decrement would be visible rather than clamped.
    client_cells_before = demographic_cells(db_session, a.id)
    assert client_cells_before == {("age", "18-24", "for"): 1}

    client.post(f"/loi/{a.slug}/vote", data={"choice": "against"})

    after = demographic_cells(db_session, a.id)
    assert after.get(("age", "18-24", "against")) == 1, "the voter's own cell should move"
    assert after.get(("age", "18-24", "for"), 0) == 0
    assert after.get(("age", "65+", "against"), 0) == 0, "law B's category must not appear on A"
    # And law B is untouched by the change made on law A.
    assert demographic_cells(db_session, b.id) == {("age", "65+", "for"): 1}


def test_the_cookie_remembers_several_laws_at_once(client, db_session):
    a = make_law(db_session, slug="loi-c", title="Loi C")
    b = make_law(db_session, slug="loi-d", title="Loi D")
    client.post(f"/loi/{a.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    client.post(f"/loi/{b.slug}/vote", data={"choice": "for", "age_bracket": "65+"})


    stored = client.cookies.get(profile_cookie.COOKIE_NAME)
    assert _stored_for(stored, a.id) == {"age": "18-24"}
    assert _stored_for(stored, b.id) == {"age": "65+"}


def test_the_cookie_cannot_grow_without_bound():
    """A browser that votes on hundreds of laws must not exceed the 4 kB limit."""
    from types import SimpleNamespace

    response = SimpleNamespace(set_cookie=lambda *a, **k: captured.update(value=a[1]))
    captured: dict = {}
    store: dict = {}
    serializer = URLSafeSerializer(get_settings().secret_key, salt="voter-profile")

    for law_id in range(400):
        store[str(law_id)] = {"age": "18-24", "sexe": "femme", "region": "Île-de-France"}
    request = SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: serializer.dumps(store)})

    profile_cookie.load(request).remember(response, 999, {"age": "25-34"})
    assert len(captured["value"].encode()) <= 4000
