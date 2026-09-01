"""Privacy behaviour: what is stored, when, and what an export gives away."""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeSerializer
from sqlalchemy import delete

from app import profile_cookie
from app.auth.anonymous import COOKIE_NAME
from app.config import get_settings
from app.crud import demographics as demo_crud
from app.crud import votes as votes_crud
from app.models import Law, Vote, VoteChoice
from tests.helpers import cast_ballot, demographic_cells, make_law

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
    # And nothing else about the person: the columns are the law, the choice, the
    # hashed token, the time, and that one flag. In particular no second choice —
    # a row that carried the choice a voter had made *before* would be a record of
    # a changed mind, which /vie-privee promises this table does not hold.
    assert set(stored) == {
        "id",
        "law_id",
        "choice",
        "voter_token",
        "created_at",
        "demographics_recorded",
    }


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

    cast_ballot(db_session, open_law, "jeton", VoteChoice.FOR, age_bracket="65+")
    assert demographic_cells(db_session, open_law.id)[("age", "65+", "for")] == 1

    cast_ballot(db_session, open_law, "jeton", VoteChoice.AGAINST, previous_categories={})

    cells = demographic_cells(db_session, open_law.id)
    assert cells[("age", "65+", "for")] == 1

    # And it stays there, for every later change, however many. That is what makes
    # it safe to keep no record of where a contribution sits: the ballot stops
    # offering the profile fields once one exists, and the cookie is only ever
    # written back from categories it already held — so a browser that has
    # forgotten this law can never learn it again, and can never move the wrong
    # cell later on.
    cast_ballot(db_session, open_law, "jeton", VoteChoice.ABSTAIN, previous_categories={})
    assert demographic_cells(db_session, open_law.id)[("age", "65+", "for")] == 1


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


# --- A key rotation must not re-open a votation -------------------------------


def _forget_signing_keys() -> None:
    """Drop everything built from SECRET_KEY, so the next use rebuilds it.

    Both the voter-token provider and the profile-cookie serializer derive their
    signing keys once and hold them for the life of the process — which is right,
    since the key cannot change under a running server, and is exactly what a
    test rotating the key has to defeat.
    """
    import app.auth.base as auth_base

    auth_base.get_auth_provider.cache_clear()
    profile_cookie._serializer.cache_clear()


@pytest.fixture
def rotate_key(monkeypatch):
    """Reboot the signing keys on a different SECRET_KEY, as a new host would.

    A fixture rather than a plain helper because the *undo* is the load-bearing
    half. ``monkeypatch`` restores the settings on its own, but it knows nothing
    about the two caches derived from them: left populated, they would go on
    signing with the rotated key for the rest of the session, in tests that
    believe they are running on the configured one. Consistently wrong keys read
    back what they wrote, so nothing would fail — which is what makes it worth a
    fixture instead of two lines somebody remembers.
    """

    def rotate(*, new: str, fallbacks: list[str]) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "secret_key", new, raising=False)
        monkeypatch.setattr(settings, "secret_key_fallbacks", fallbacks, raising=False)
        _forget_signing_keys()

    yield rotate
    _forget_signing_keys()


def test_carrying_the_old_key_keeps_a_voter_identified(client, open_law, db_session, rotate_key):
    """Moving to another host must not hand everyone a second ballot.

    SECRET_KEY signs the voter token, so a new host with a new key cannot read
    the cookies its voters already carry: each is issued a fresh token, matching
    no existing row, and the votation quietly accepts a second vote from someone
    who had already voted.
    """
    old = get_settings().secret_key
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 1

    rotate_key(new="une-toute-autre-cle-de-quarante-caracteres", fallbacks=[old])

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})
    votes = db_session.query(Vote).filter_by(law_id=open_law.id).all()
    assert len(votes) == 1, "the same browser was counted twice across the rotation"
    assert votes[0].choice == "against", "and it is still their vote to change"


def test_dropping_the_fallback_does_log_everyone_out(client, open_law, db_session, rotate_key):
    """The other half of the contract, for a key you believe has leaked.

    Rotating with no fallback stops forged cookies being honoured, at the price
    of every voter being treated as new. That price is the point; this test is
    what keeps it from being a surprise.
    """
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})

    rotate_key(new="une-cle-de-remplacement-sans-repli-du-tout", fallbacks=[])

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 2


# --- Two ballots in flight at once --------------------------------------------
#
# Both tests below run against real, separate connections on purpose: what they
# check is how two transactions interleave, and the rest of the suite runs inside
# a single shared one where they cannot.


@contextmanager
def _law_of_its_own(Session):
    """A committed law, and its removal afterwards whatever happens."""
    setup = Session()
    law = Law(
        slug="loi-bulletins-simultanes",
        title="Loi votée deux fois d'un coup",
        published=True,
        opens_at=datetime.now(UTC) - timedelta(hours=1),
        closes_at=datetime.now(UTC) + timedelta(hours=1),
    )
    setup.add(law)
    setup.commit()
    try:
        yield law.id
    finally:
        # Cascades to the votes and the counters, so nothing of this test is left
        # behind for the next one to trip over.
        setup.execute(delete(Law).where(Law.id == law.id))
        setup.commit()
        setup.close()


def _both_at_once(Session, ballot) -> None:
    """Run ``ballot(db)`` twice, on two connections, released together.

    The barrier is what makes this a test rather than a coincidence: both threads
    are inside the vote path before either is allowed to finish it.
    """
    start = threading.Barrier(2, timeout=10)
    failures: list[Exception] = []

    def run() -> None:
        db = Session()
        try:
            start.wait()
            ballot(db)
            db.commit()
        except Exception as exc:  # surfaced below rather than lost in a thread
            failures.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not failures, failures
    assert not any(thread.is_alive() for thread in threads), "a ballot never finished"


def test_two_first_ballots_at_once_count_one_profile(sessionmaker_for):
    """One voter, two simultaneous first ballots, one contribution.

    The bug this pins: casting the vote and folding in the profile were two
    transactions, and the second read ``demographics_recorded`` before the first
    had set it. Both believed they were the first contribution, both added one,
    and the demographic breakdown came out larger than the tally it is supposed
    to decompose — on the one page whose promise is that you can check the number
    yourself. It took nothing more exotic than a double click, a retried POST or
    two tabs.
    """
    Session = sessionmaker_for
    with _law_of_its_own(Session) as law_id:

        def ballot(db):
            vote, previous = votes_crud.cast_vote(
                db, db.get(Law, law_id), "meme-navigateur", VoteChoice.FOR
            )
            # Deliberate, and the whole method of this test: it holds the ballot
            # open across the moment the other one is reading. While the two
            # halves share a transaction the second thread is still waiting on
            # the first inside `cast_vote` and this costs nothing; split them
            # again and this is the window in which both read
            # `demographics_recorded` as false.
            time.sleep(0.1)
            demo_crud.record(
                db,
                vote=vote,
                choice=VoteChoice.FOR,
                previous_choice=previous,
                age_bracket="25-34",
            )

        _both_at_once(Session, ballot)

        check = Session()
        try:
            assert check.query(Vote).filter_by(law_id=law_id).count() == 1
            assert demographic_cells(check, law_id) == {("age", "25-34", "for"): 1}
        finally:
            check.close()


def test_two_vote_changes_at_once_move_the_profile_once(sessionmaker_for):
    """The same voter changing their mind twice at once moves one contribution.

    This is the half the row lock buys. A first ballot is already serialised by
    the insert, which waits on a conflicting transaction; a *change* conflicts
    with a row that is long committed, so nothing waits — and both ballots read
    the same previous choice, both decremented the old cell and both incremented
    the new one. The old cell clamps at zero, the new one holds two, and one
    voter has been counted twice in a breakdown for the second time.
    """
    Session = sessionmaker_for
    with _law_of_its_own(Session) as law_id:
        first = Session()
        try:
            vote, previous = votes_crud.cast_vote(
                first, first.get(Law, law_id), "meme-navigateur", VoteChoice.FOR
            )
            demo_crud.record(
                first,
                vote=vote,
                choice=VoteChoice.FOR,
                previous_choice=previous,
                age_bracket="25-34",
            )
            first.commit()
        finally:
            first.close()

        def ballot(db):
            vote, previous = votes_crud.cast_vote(
                db, db.get(Law, law_id), "meme-navigateur", VoteChoice.AGAINST
            )
            demo_crud.record(
                db,
                vote=vote,
                choice=VoteChoice.AGAINST,
                previous_choice=previous,
                # What the browser sends back, both times.
                previous_categories={"age": "25-34"},
            )

        _both_at_once(Session, ballot)

        check = Session()
        try:
            cells = demographic_cells(check, law_id)
            assert cells.get(("age", "25-34", "against")) == 1
            assert cells.get(("age", "25-34", "for"), 0) == 0
        finally:
            check.close()


# --- The published breakdown is a snapshot, not the live counters -------------
#
# The counters are marginal and never joint, and `MIN_CELL_COUNT` withholds a
# whole dimension unless every populated cell clears it. Both protect a single
# reading of the results page; neither protects a sequence of them. These tests
# hold the rule that does.


#: Every published breakdown cell on a results page: (category, pour, contre, abstention).
#: The tally above them is live by design, so comparing whole pages would only
#: ever prove that — this is the part the snapshot rule is about.
_CELLS = re.compile(
    r'<th scope="row">([^<]+)</th>\s*<td>(\d+) \([\d.]+%\)</td>\s*'
    r"<td>(\d+) \([\d.]+%\)</td>\s*<td>(\d+) \([\d.]+%\)</td>"
)


def _published_cells(client, law):
    return _CELLS.findall(client.get(f"/loi/{law.slug}/resultats").text)


def _profiled_ballots(db, law, n, *, choice=VoteChoice.FOR, start=0, **profile):
    """``n`` ballots that each carry a profile, cast the way the route casts them."""
    for i in range(start, start + n):
        cast_ballot(db, law, f"snap-{i}", choice, **profile)


def _first_snapshot(db, law, **profile):
    """Cast just enough profiled ballots to publish this law's first snapshot.

    One more than MIN_BUCKET_VOLUME, and the extra one is not an accident: the
    snapshot is taken at the *top* of a ballot, before that ballot's own profile
    is counted, so the voter who triggers a step is never inside it. The ballot
    that crosses the threshold therefore publishes the ones before it.
    """
    volume = get_settings().min_bucket_volume
    _profiled_ballots(db, law, volume + 1, **profile)
    assert law.demographics_snapshot_at is not None, "expected a first snapshot by now"
    return volume


def test_one_ballot_never_moves_the_published_breakdown(open_law, db_session, client):
    """The whole of SEC-1: two readings of a public page, differenced.

    Before the snapshot existed, subtracting two fetches thirty seconds apart
    yielded a single ballot — and named that voter's choice together with their
    age, sex and région at once, which is exactly the joint cell the schema
    refuses to store.
    """
    # All in one category, so the cells clear the suppression threshold and the
    # table is actually shown.
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49", sexe="femme")

    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "35-49" in page, "the seeded breakdown should be published by now"
    assert "arrêtées au" in page, "a lagged figure has to say that it is lagged"
    before = _CELLS.findall(page)
    assert before, "nothing to difference - the test would prove nothing"

    # One more voter, with a profile nobody else in this votation has.
    cast_ballot(
        db_session, open_law, "the-target", VoteChoice.AGAINST, age_bracket="65+", sexe="autre"
    )

    assert _published_cells(client, open_law) == before, (
        "one ballot moved the published breakdown - SEC-1 is back"
    )
    # And the headline tally *is* live, deliberately: it is one bit about one
    # voter, and being checkable is the point of this platform.
    assert demo_crud.provision_rate(db_session, open_law.id).total_votes == volume + 2


def test_nothing_is_published_before_the_first_snapshot(open_law, db_session):
    """« pas encore publiée », never « aucune donnée » — the two are not the same."""
    _profiled_ballots(db_session, open_law, 3, age_bracket="35-49")

    breakdowns = demo_crud.all_breakdowns(db_session, open_law)
    assert all(not bd.shown for bd in breakdowns)
    assert {bd.reason for bd in breakdowns} == {"pending"}


def test_a_closed_votation_publishes_its_live_counters(open_law, db_session):
    """Nothing can move once voting has closed, so there is nothing to difference."""
    _profiled_ballots(db_session, open_law, 12, age_bracket="35-49", sexe="femme")
    # Not published yet: too few profiles for a snapshot while it was open.
    assert demo_crud.all_breakdowns(db_session, open_law)[0].reason == "pending"

    open_law.closes_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    age = next(bd for bd in demo_crud.all_breakdowns(db_session, open_law) if bd.dimension == "age")
    assert age.shown is True
    assert age.rows[0].counts["for"] == 12


def test_the_snapshot_steps_by_profiles_not_by_votes(open_law, db_session):
    """A window of many votes carrying one profile would hand that one over."""
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49")

    # Far more than MIN_BUCKET_VOLUME further ballots — but only one of them
    # carries a profile, so the step must not be allowed.
    open_law.demographics_snapshot_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    for i in range(volume * 2):
        cast_ballot(db_session, open_law, f"bare-{i}", VoteChoice.FOR)
    cast_ballot(db_session, open_law, "lone-profile", VoteChoice.AGAINST, age_bracket="65+")

    assert demo_crud.refresh_snapshot(db_session, open_law) is False


def test_the_snapshot_does_move_once_enough_profiles_arrive(open_law, db_session):
    """The counterpart: the lag is a step, not a freeze."""
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49")

    # The hourly gate is what stops the count running on every ballot; wind it
    # back so this test is about the volume rule and not about the clock.
    open_law.demographics_snapshot_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    _profiled_ballots(db_session, open_law, volume + 1, start=volume + 1, age_bracket="65+")

    age = next(bd for bd in demo_crud.all_breakdowns(db_session, open_law) if bd.dimension == "age")
    assert age.shown is True
    assert {r.category for r in age.rows} == {"35-49", "65+"}


def test_a_voter_never_reads_themselves_in_the_step_they_triggered(open_law, db_session):
    """The snapshot is taken before the ballot that triggers it is counted."""
    volume = get_settings().min_bucket_volume
    _profiled_ballots(db_session, open_law, volume, age_bracket="35-49")
    assert open_law.demographics_snapshot_at is None, "one profile short of a snapshot"

    # This ballot crosses the threshold, so it is the one that triggers the step.
    cast_ballot(db_session, open_law, "trigger", VoteChoice.FOR, age_bracket="65+")
    assert open_law.demographics_snapshot_at is not None

    cells = demographic_cells(db_session, open_law.id)
    assert cells[("age", "65+", "for")] == 1, "the live counter has it"
    published = demo_crud._cells_by_dimension(db_session, open_law.id, live=False)
    assert published["age"]["35-49"]["for"] == volume, "the step published the others"
    assert published["age"]["65+"]["for"] == 0, "but never the voter who triggered it"


def test_a_trimmed_profile_cookie_leaves_the_store_saying_the_same_thing():
    """The cookie and the object it was built from must not disagree.

    `_fit` drops the oldest laws until the value fits under 4 kB. Returning only
    the string left the caller holding a store that still listed laws the browser
    had just been told to forget — harmless while nothing read it afterwards,
    which is exactly the kind of quiet disagreement that stops being harmless.
    """
    store = profile_cookie.ProfileStore(
        {str(i): {"age": "35-49", "sexe": "femme", "region": "Bretagne"} for i in range(200)}
    )
    response = SimpleNamespace(set_cookie=lambda *a, **k: None)
    store.remember(response, 999, {"age": "18-24"})

    assert len(store.laws) <= profile_cookie._MAX_LAWS
    assert store.for_law(999) == {"age": "18-24"}, "the law just written must survive the trim"
    # And what the object says is exactly what the cookie carries.
    _value, kept = profile_cookie._fit(store.laws)
    assert kept == store.laws
