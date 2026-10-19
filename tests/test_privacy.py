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
from tests.helpers import breakdown, cast_ballot, demographic_cells, make_law

# --- Nothing is issued to someone who only reads ------------------------------


def test_reading_a_law_sets_no_cookie(client, open_law):
    """No identifier is created for visitors who only read."""
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
    """A fake request with just the cookies."""
    return SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: cookie})


def _stored_for(cookie: str, law_id: int) -> dict[str, str]:
    """Categories stored in the cookie for one law."""
    return profile_cookie.load(_request_with(cookie)).for_law(law_id)


def test_changing_a_vote_moves_the_profile_with_it(client, open_law, db_session):
    """The profile moves to the new choice."""
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
    """Breakdown totals match the tally."""
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
    """Replaying an old profile cookie can't make a counter negative."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for", "sexe": "homme"})
    # Another voter, who doesn't touch the "homme/for" cell.
    for choice in ("against", "for", "against", "for", "against"):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": choice})
    assert all(count >= 0 for count in demographic_cells(db_session, open_law.id).values())


def test_the_server_still_stores_no_profile_per_voter(client, open_law, db_session):
    """Only counters; the vote row has no category."""
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
    # Exactly the columns listed on /vie-privee. In particular no previous choice.
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
    # Signed, so it can't be forged...
    assert "18-24" not in stored
    # ...and once read back, it carries the categories alone.
    assert _stored_for(stored, open_law.id) == {"age": "18-24"}


def test_a_tampered_profile_cookie_is_ignored():
    assert _stored_for("n-importe-quoi", 1) == {}


def test_a_cookie_from_before_the_per_law_split_is_ignored():
    """An old site-wide cookie is ignored, as if there were no cookie."""
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
    """Without a cookie, the old contribution stays where it was."""

    cast_ballot(db_session, open_law, "jeton", VoteChoice.FOR, age_bracket="65+")
    assert demographic_cells(db_session, open_law.id)[("age", "65+", "for")] == 1

    cast_ballot(db_session, open_law, "jeton", VoteChoice.AGAINST, previous_categories={})

    cells = demographic_cells(db_session, open_law.id)
    assert cells[("age", "65+", "for")] == 1

    # And stays there for any later change: the profile fields are no longer
    # offered, so the browser can't move the wrong cell later.
    cast_ballot(db_session, open_law, "jeton", VoteChoice.ABSTAIN, previous_categories={})
    assert demographic_cells(db_session, open_law.id)[("age", "65+", "for")] == 1


def test_a_profile_given_on_one_law_never_moves_another_laws_counters(client, db_session):
    """The cookie is per law: changing a vote on A uses A's profile, not B's."""
    a = make_law(db_session, slug="loi-a", title="Loi A")
    b = make_law(db_session, slug="loi-b", title="Loi B")

    client.post(f"/loi/{a.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    client.post(f"/loi/{b.slug}/vote", data={"choice": "for", "age_bracket": "65+"})

    # Someone else's vote, so a wrong decrement would show.
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
    """The cookie stays under 4 kB even after hundreds of laws."""
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
    """Clear the cached objects derived from SECRET_KEY."""
    import app.auth.base as auth_base

    auth_base.get_auth_provider.cache_clear()
    profile_cookie._serializer.cache_clear()


@pytest.fixture
def rotate_key(monkeypatch):
    """Switch to another SECRET_KEY, as on a new host.

    A fixture so the key caches are also cleared afterwards, which
    monkeypatch alone wouldn't do.
    """

    def rotate(*, new: str, fallbacks: list[str]) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "secret_key", new, raising=False)
        monkeypatch.setattr(settings, "secret_key_fallbacks", fallbacks, raising=False)
        _forget_signing_keys()

    yield rotate
    _forget_signing_keys()


def test_carrying_the_old_key_keeps_a_voter_identified(client, open_law, db_session, rotate_key):
    """With the old key as a fallback, voters keep their token after a key change."""
    old = get_settings().secret_key
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 1

    rotate_key(new="une-toute-autre-cle-de-quarante-caracteres", fallbacks=[old])

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})
    votes = db_session.query(Vote).filter_by(law_id=open_law.id).all()
    assert len(votes) == 1, "the same browser was counted twice across the rotation"
    assert votes[0].choice == "against", "and it is still their vote to change"


def test_dropping_the_fallback_does_log_everyone_out(client, open_law, db_session, rotate_key):
    """Without a fallback (leaked key), every voter gets a new token."""
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})

    rotate_key(new="une-cle-de-remplacement-sans-repli-du-tout", fallbacks=[])

    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "against"})
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 2


# --- Two ballots in flight at once --------------------------------------------
#
# These use real separate connections, to test two concurrent transactions.


@contextmanager
def _law_of_its_own(Session):
    """A committed law, deleted afterwards."""
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
        # Cascades to votes and counters.
        setup.execute(delete(Law).where(Law.id == law.id))
        setup.commit()
        setup.close()


def _both_at_once(Session, ballot) -> None:
    """Run ``ballot(db)`` twice on two connections at the same time.

    The barrier makes sure both threads are in the vote path together.
    """
    start = threading.Barrier(2, timeout=10)
    failures: list[Exception] = []

    def run() -> None:
        db = Session()
        try:
            start.wait()
            ballot(db)
            db.commit()
        except Exception as exc:  # re-raised below
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
    """Two simultaneous first ballots from one voter count the profile once.

    Both used to read ``demographics_recorded`` as false (e.g. on a double
    click).
    """
    Session = sessionmaker_for
    with _law_of_its_own(Session) as law_id:

        def ballot(db):
            vote, previous = votes_crud.cast_vote(
                db, db.get(Law, law_id), "meme-navigateur", VoteChoice.FOR
            )
            # Keeps the transaction open while the other thread reads. With the
            # lock, the other thread is waiting in `cast_vote` meanwhile.
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
    """Two simultaneous vote changes move the profile once.

    For an existing vote, the insert doesn't wait on anything, so without
    the row lock both would read the same previous choice.
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


def test_two_ballots_at_once_take_one_snapshot(sessionmaker_for):
    """Two simultaneous ballots when a snapshot is due take it once.

    A second snapshot right after the first would be a step of one voter.
    """
    Session = sessionmaker_for
    volume = get_settings().min_bucket_volume
    with _law_of_its_own(Session) as law_id:
        setup = Session()
        try:
            law = setup.get(Law, law_id)
            for i in range(volume):
                cast_ballot(setup, law, f"avant-{i}", VoteChoice.FOR, age_bracket="35-49")
            assert law.demographics_snapshot_at is None, "one profile short of a snapshot"
        finally:
            setup.close()

        def ballot(db):
            law = db.get(Law, law_id)
            vote, previous = votes_crud.cast_vote(
                db, law, f"voter-{threading.get_ident()}", VoteChoice.FOR
            )
            demo_crud.refresh_snapshot(db, law)
            # Keeps the counters locked while the other thread checks.
            time.sleep(0.1)
            demo_crud.record(
                db,
                vote=vote,
                choice=VoteChoice.FOR,
                previous_choice=previous,
                # Same cell as before, so an extra step would show in it.
                age_bracket="35-49",
            )

        _both_at_once(Session, ballot)

        check = Session()
        try:
            _, published = demo_crud._cells_by_dimension(demo_crud._counters(check, law_id))
            assert published["age"]["35-49"]["for"] == volume, "a step of one voter"
        finally:
            check.close()


def _snapshot_while_a_ballot_holds_a_cell(
    Session, law_id, monkeypatch, *, ballot_profile, trigger_profile=None
):
    """A ballot takes its first cell and waits; meanwhile a ballot takes a snapshot.

    ``trigger_profile`` is the snapshotting voter's own profile, added after
    the snapshot as the vote route does. Fails on any error, deadlocks included.
    """
    holds_a_cell = threading.Event()
    original_add = demo_crud._add

    def slow_add(*args, **kwargs):
        original_add(*args, **kwargs)
        if not holds_a_cell.is_set():
            holds_a_cell.set()
            # Holds this cell while the snapshot locks the others.
            time.sleep(0.5)

    monkeypatch.setattr(demo_crud, "_add", slow_add)
    failures: list[Exception] = []

    def ballot():
        db = Session()
        try:
            vote, previous = votes_crud.cast_vote(
                db, db.get(Law, law_id), "pendant", VoteChoice.FOR
            )
            demo_crud.record(
                db, vote=vote, choice=VoteChoice.FOR, previous_choice=previous, **ballot_profile
            )
            db.commit()
        except Exception as exc:  # re-raised below
            failures.append(exc)
        finally:
            db.close()

    def snapshot():
        db = Session()
        try:
            assert holds_a_cell.wait(timeout=10)
            law = db.get(Law, law_id)
            vote, previous = votes_crud.cast_vote(db, law, "declencheur", VoteChoice.FOR)
            assert demo_crud.refresh_snapshot(db, law) is True
            if trigger_profile:
                demo_crud.record(
                    db,
                    vote=vote,
                    choice=VoteChoice.FOR,
                    previous_choice=previous,
                    **trigger_profile,
                )
            db.commit()
        except Exception as exc:  # re-raised below
            failures.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=ballot), threading.Thread(target=snapshot)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not failures, failures
    assert not any(thread.is_alive() for thread in threads)


def test_a_snapshot_and_a_ballot_do_not_deadlock(sessionmaker_for, monkeypatch):
    """A snapshot and a ballot take the counters in the same order, so they can't deadlock."""
    Session = sessionmaker_for
    volume = get_settings().min_bucket_volume
    profile = {"sexe": "femme", "region": "Bretagne"}
    with _law_of_its_own(Session) as law_id:
        setup = Session()
        try:
            law = setup.get(Law, law_id)
            # The region cell is created first, so a scan meets it before sexe.
            cast_ballot(setup, law, "avant-region", VoteChoice.FOR, region="Bretagne")
            for i in range(volume - 1):
                cast_ballot(setup, law, f"avant-{i}", VoteChoice.FOR, **profile)
            assert law.demographics_snapshot_at is None, "due on the next check"
        finally:
            setup.close()

        _snapshot_while_a_ballot_holds_a_cell(Session, law_id, monkeypatch, ballot_profile=profile)

        check = Session()
        try:
            law = check.get(Law, law_id)
            _, published = demo_crud._cells_by_dimension(demo_crud._counters(check, law_id))
            # The snapshot waited for the ballot, so it holds it and counts it.
            assert published["sexe"]["femme"]["for"] == volume
            assert law.demographics_snapshot_profiles == volume + 1
        finally:
            check.close()


def test_a_snapshot_and_a_ballot_creating_a_cell_do_not_deadlock(sessionmaker_for, monkeypatch):
    """A ballot creating a cell during a snapshot doesn't deadlock with it."""
    Session = sessionmaker_for
    volume = get_settings().min_bucket_volume
    with _law_of_its_own(Session) as law_id:
        setup = Session()
        try:
            law = setup.get(Law, law_id)
            for i in range(volume):
                cast_ballot(
                    setup, law, f"avant-{i}", VoteChoice.FOR, age_bracket="35-49", region="Bretagne"
                )
            assert law.demographics_snapshot_at is None, "due on the next check"
        finally:
            setup.close()

        _snapshot_while_a_ballot_holds_a_cell(
            Session,
            law_id,
            monkeypatch,
            ballot_profile={"age_bracket": "18-24", "region": "Bretagne"},
            trigger_profile={"age_bracket": "18-24"},
        )

        check = Session()
        try:
            counted, published = demo_crud._cells_by_dimension(demo_crud._counters(check, law_id))
            assert counted["age"]["18-24"]["for"] == 2, "both ballots counted"
            assert published["age"]["18-24"]["for"] == 0, "a new cell below a full step waits"
        finally:
            check.close()


# --- The published breakdown is a snapshot, not the live counters -------------


# Breakdown cells on a results page: (category, pour, contre, abstention).
# Not the whole page, since the headline tally is live.
_CELLS = re.compile(
    r'<th scope="row">([^<]+)</th>\s*<td>(\d+) \([\d.]+%\)</td>\s*'
    r"<td>(\d+) \([\d.]+%\)</td>\s*<td>(\d+) \([\d.]+%\)</td>"
)


def _published_cells(client, law):
    return _CELLS.findall(client.get(f"/loi/{law.slug}/resultats").text)


def _profiled_ballots(db, law, n, *, choice=VoteChoice.FOR, start=0, **profile):
    """Cast ``n`` ballots with a profile."""
    for i in range(start, start + n):
        cast_ballot(db, law, f"snap-{i}", choice, **profile)


def _first_snapshot(db, law, **profile):
    """Cast enough profiled ballots for the first snapshot.

    MIN_BUCKET_VOLUME + 1, since the snapshot is taken before the triggering
    ballot is counted.
    """
    volume = get_settings().min_bucket_volume
    _profiled_ballots(db, law, volume + 1, **profile)
    assert law.demographics_snapshot_at is not None, "expected a first snapshot by now"
    return volume


def test_one_ballot_never_moves_the_published_breakdown(open_law, db_session, client):
    """Comparing the page before and after one ballot reveals nothing."""
    # All in one category, so the table passes the suppression threshold.
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49", sexe="femme")

    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "35-49" in page, "the seeded breakdown should be published by now"
    assert "arrêtées au" in page, "a lagged figure has to say that it is lagged"
    before = _CELLS.findall(page)
    assert before, "nothing to difference - the test would prove nothing"

    # One more voter, with a unique profile.
    cast_ballot(
        db_session, open_law, "the-target", VoteChoice.AGAINST, age_bracket="65+", sexe="autre"
    )

    assert _published_cells(client, open_law) == before, (
        "one ballot moved the published breakdown - SEC-1 is back"
    )
    # The headline tally is live.
    assert demo_crud.provision_rate(db_session, open_law.id).total_votes == volume + 2


def test_nothing_is_published_before_the_first_snapshot(open_law, db_session):
    """Before the first snapshot the page says "pas encore publiée", not "aucune donnée"."""
    _profiled_ballots(db_session, open_law, 3, age_bracket="35-49")

    breakdowns = demo_crud.all_breakdowns(db_session, open_law)
    assert all(not bd.shown for bd in breakdowns)
    assert {bd.reason for bd in breakdowns} == {"pending"}


def _settle_the_trigger(db, law, *, start):
    """Bring the first step's triggering cell to a full ``min_cell_count`` change.

    The ballot that triggers a step is counted after it, so its cell moved by
    one: on its own, that cell would hold its category back (see `_held`).
    """
    _profiled_ballots(db, law, get_settings().min_cell_count - 1, start=start, age_bracket="35-49")


def _close(db, law):
    """Close the vote, past the grace that keeps the last step after closing."""
    law.closes_at = datetime.now(UTC) - timedelta(minutes=5)
    db.commit()


def _as_of(db, law):
    return demo_crud.published_as_of(law, demo_crud.all_breakdowns(db, law))


def test_a_votation_that_never_published_shows_its_live_counters_once_closed(open_law, db_session):
    """No snapshot ever published: live counters are shown once closed.

    There's no earlier published figure to compare against.
    """
    _profiled_ballots(db_session, open_law, 12, age_bracket="35-49", sexe="femme")
    # Too few profiles for a snapshot while open.
    assert demo_crud.all_breakdowns(db_session, open_law)[0].reason == "pending"

    _close(db_session, open_law)

    age = breakdown(db_session, open_law, "age")
    assert age.shown is True
    assert age.rows[0].counts["for"] == 12
    assert _as_of(db_session, open_law) is None, "live, so undated"


def test_closing_does_not_release_a_step_too_small_to_be_one(open_law, db_session, client):
    """Closing follows the same volume rule as any other step.

    Otherwise comparing the page before and after closing would reveal the
    last few voters.
    """
    _first_snapshot(db_session, open_law, age_bracket="35-49", sexe="femme")
    before = _published_cells(client, open_law)

    # One profiled ballot arrives, then the votation closes.
    cast_ballot(
        db_session, open_law, "last-voter", VoteChoice.AGAINST, age_bracket="65+", sexe="homme"
    )
    _close(db_session, open_law)

    assert _published_cells(client, open_law) == before, (
        "closing published the residual, so one voter's whole profile is the difference"
    )
    assert _as_of(db_session, open_law) is not None, "and it is dated"
    assert breakdown(db_session, open_law, "age").shown is True, (
        "the last step's figures are still shown"
    )


def test_closing_releases_settled_categories_and_holds_the_others(open_law, db_session, client):
    """Closing is a step too: age goes live, a lone Corsican vote stays unpublished."""
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49")
    _settle_the_trigger(db_session, open_law, start=volume + 1)
    cast_ballot(db_session, open_law, "seul-en-corse", VoteChoice.AGAINST, region="Corse")
    _profiled_ballots(db_session, open_law, volume, start=volume + 100, age_bracket="65+")
    _close(db_session, open_law)

    assert {r.category for r in breakdown(db_session, open_law, "age").rows} == {"35-49", "65+"}, (
        "live"
    )
    # Not "all live": a category with a vote is left out, and the page says so.
    assert _as_of(db_session, open_law) == open_law.closes_at

    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "Corse" not in page
    assert "Non publiée" in page
    assert "Pas encore publiée" not in page, "a closed vote has no later step to promise"
    assert "jamais publiée n'apparaît pas" in page


def test_a_closed_votation_publishes_live_once_the_residual_is_a_real_step(open_law, db_session):
    """Live counters are shown once the remainder is large enough."""
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49")
    _settle_the_trigger(db_session, open_law, start=volume + 1)
    _profiled_ballots(db_session, open_law, volume, start=volume + 100, age_bracket="65+")
    _close(db_session, open_law)

    age = breakdown(db_session, open_law, "age")
    assert _as_of(db_session, open_law) is None, "live, so undated"
    assert {r.category for r in age.rows} == {"35-49", "65+"}


def test_the_export_dates_its_breakdowns_exactly_as_the_page_does(open_law, db_session, client):
    """The JSON export shows the same snapshot date as the page."""
    _first_snapshot(db_session, open_law, age_bracket="35-49", sexe="femme")
    cast_ballot(db_session, open_law, "last-voter", VoteChoice.AGAINST, age_bracket="65+")
    _close(db_session, open_law)

    payload = client.get(f"/loi/{open_law.slug}/export.json").json()
    assert payload["demographics_as_of"] is not None, "a snapshot, and the file says so"
    categories = {row["category"] for block in payload["demographics"] for row in block["rows"]}
    assert "65+" not in categories, "the export released what the page withheld"


def test_the_snapshot_steps_by_profiles_not_by_votes(open_law, db_session):
    """The snapshot counts profiled votes, not all votes."""
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49")

    # Many more ballots, but only one with a profile: no step.
    open_law.demographics_snapshot_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    for i in range(volume * 2):
        cast_ballot(db_session, open_law, f"bare-{i}", VoteChoice.FOR)
    cast_ballot(db_session, open_law, "lone-profile", VoteChoice.AGAINST, age_bracket="65+")

    assert demo_crud.refresh_snapshot(db_session, open_law) is False


def test_the_snapshot_does_move_once_enough_profiles_arrive(open_law, db_session):
    """The snapshot moves once enough profiles arrive."""
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49")

    # Move the clock back past the hourly limit.
    open_law.demographics_snapshot_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    _settle_the_trigger(db_session, open_law, start=volume + 1)
    _profiled_ballots(db_session, open_law, volume + 1, start=volume + 100, age_bracket="65+")

    age = breakdown(db_session, open_law, "age")
    assert age.shown is True
    assert {r.category for r in age.rows} == {"35-49", "65+"}


def test_a_cell_that_moved_by_one_holds_only_its_category_back(open_law, db_session, client):
    """A step copies a category only if each of its cells moved by 0 or a full cell.

    Otherwise a step of 50 profiles moving "Corse, Pour" by one would tell
    anyone who knows a Corsican voted in that window how they voted. The rest
    of the table moves on, and the held category says how old it is.
    """
    _profiled_ballots(db_session, open_law, 15, region="Corse")
    _profiled_ballots(db_session, open_law, 36, start=15, region="Bretagne")
    first_step = open_law.demographics_snapshot_at
    assert first_step is not None, "a first step, Corse at 15"
    open_law.demographics_snapshot_at = first_step - timedelta(days=1)
    db_session.commit()
    first_step = open_law.demographics_snapshot_at

    # Bretagne gains enough for a second step; Corse gains one vote.
    _profiled_ballots(db_session, open_law, 1, start=100, region="Corse")
    _profiled_ballots(db_session, open_law, 60, start=200, region="Bretagne")
    assert open_law.demographics_snapshot_at > first_step, "expected a second step"

    _, published = demo_crud._cells_by_dimension(demo_crud._counters(db_session, open_law.id))
    assert published["region"]["Bretagne"]["for"] > 36, "Bretagne moved"
    assert published["region"]["Corse"]["for"] == 15, "Corse waited"

    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert 'Corse<br><span class="meta">arrêtée au' in page
    assert "Bretagne<br>" not in page, "only the held category is dated"
    assert "palier plus ancien que les autres" in " ".join(page.split()), (
        "and the footnote says why"
    )
    payload = client.get(f"/loi/{open_law.slug}/export.json").json()
    region = next(block for block in payload["demographics"] if block["dimension"] == "region")
    as_of = {row["category"]: row["as_of"] for row in region["rows"]}
    assert as_of["Bretagne"] == payload["demographics_as_of"]
    assert as_of["Corse"] < payload["demographics_as_of"]
    # Corse's old count against Bretagne's new one would be neither date's share.
    shares = {row["category"]: row["share_pct"] for row in region["rows"]}
    assert shares == {"Bretagne": 100.0, "Corse": None}
    national = {row["category"]: row["reference_pct"] for row in region["rows"]}
    # On the same categories as the shares: Bretagne alone would read 100%.
    assert national == {"Bretagne": None, "Corse": None}


def test_a_table_held_at_one_older_step_keeps_its_shares(open_law, db_session, client):
    """Every region from the same older step: compared with each other, not hidden."""
    _profiled_ballots(db_session, open_law, 20, region="Bretagne", age_bracket="35-49")
    _profiled_ballots(db_session, open_law, 31, start=20, region="Corse", age_bracket="35-49")
    first_step = open_law.demographics_snapshot_at
    assert first_step is not None
    open_law.demographics_snapshot_at = first_step - timedelta(days=1)
    db_session.commit()

    # Age carries the second step; each region gained a single vote.
    _settle_the_trigger(db_session, open_law, start=100)
    _profiled_ballots(db_session, open_law, 1, start=200, region="Bretagne")
    _profiled_ballots(db_session, open_law, 50, start=300, age_bracket="65+")
    assert open_law.demographics_snapshot_at > first_step - timedelta(days=1)

    region = breakdown(db_session, open_law, "region")
    assert all(row.held for row in region.rows), "both from the first step"
    assert {row.category: row.share for row in region.rows} == {"Bretagne": 40.0, "Corse": 60.0}
    page = " ".join(client.get(f"/loi/{open_law.slug}/resultats").text.split())
    assert "n'a pas de parts" not in page, "every row has its shares, nothing to explain"


def test_the_pending_label_does_not_move_with_one_voter(open_law, db_session):
    """While open, a field nobody filled in reads "pending", not "empty".

    Otherwise the first voter to give a sex would flip the label at once and
    tell which field they filled in.
    """
    _first_snapshot(db_session, open_law, age_bracket="35-49")

    assert breakdown(db_session, open_law, "sexe").reason == "pending"
    cast_ballot(db_session, open_law, "premiere-a-dire", VoteChoice.FOR, sexe="femme")
    assert breakdown(db_session, open_law, "sexe").reason == "pending"


def test_a_voter_never_reads_themselves_in_the_step_they_triggered(open_law, db_session):
    """The snapshot is taken before the ballot that triggers it is counted."""
    volume = get_settings().min_bucket_volume
    _profiled_ballots(db_session, open_law, volume, age_bracket="35-49")
    assert open_law.demographics_snapshot_at is None, "one profile short of a snapshot"

    # This ballot triggers the step.
    cast_ballot(db_session, open_law, "trigger", VoteChoice.FOR, age_bracket="65+")
    assert open_law.demographics_snapshot_at is not None

    cells = demographic_cells(db_session, open_law.id)
    assert cells[("age", "65+", "for")] == 1, "the live counter has it"
    _, published = demo_crud._cells_by_dimension(demo_crud._counters(db_session, open_law.id))
    assert published["age"]["35-49"]["for"] == volume, "the step published the others"
    assert published["age"]["65+"]["for"] == 0, "but never the voter who triggered it"


def test_a_trimmed_profile_cookie_leaves_the_store_saying_the_same_thing():
    """After trimming, the store matches what the cookie contains."""
    store = profile_cookie.ProfileStore(
        {str(i): {"age": "35-49", "sexe": "femme", "region": "Bretagne"} for i in range(200)}
    )
    response = SimpleNamespace(set_cookie=lambda *a, **k: None)
    store.remember(response, 999, {"age": "18-24"})

    assert len(store.laws) <= profile_cookie._MAX_LAWS
    assert store.for_law(999) == {"age": "18-24"}, "the law just written must survive the trim"
    _value, kept = profile_cookie._fit(store.laws)
    assert kept == store.laws


# --- Only a vote that is over for good shows live figures ----------------------


def test_a_rescheduled_vote_never_shows_its_live_counters(open_law, db_session, client):
    """Moving the opening to later isn't closing: ballots will come after it.

    The live tables would otherwise show below any step, then be compared
    with the next one once voting resumes.
    """
    for i in range(30):
        cast_ballot(db_session, open_law, f"tot-{i}", VoteChoice.FOR, age_bracket="25-34")
    open_law.opens_at = datetime.now(UTC) + timedelta(hours=1)
    db_session.commit()
    assert open_law.status() == "scheduled"

    assert all(bd.reason == "pending" for bd in demo_crud.all_breakdowns(db_session, open_law))
    payload = client.get(f"/loi/{open_law.slug}/export.json").json()
    assert payload["demographics"] == []
    assert payload["demographics_as_of"] is None


def test_the_last_step_holds_for_a_moment_after_the_closing(open_law, db_session):
    """A ballot that passed its last check before the closing may still be committing."""
    volume = _first_snapshot(db_session, open_law, age_bracket="35-49")
    _settle_the_trigger(db_session, open_law, start=volume + 1)
    _profiled_ballots(db_session, open_law, volume, start=volume + 100, age_bracket="65+")

    open_law.closes_at = datetime.now(UTC) - timedelta(seconds=30)
    db_session.commit()
    assert _as_of(db_session, open_law) == open_law.demographics_snapshot_at, "still the step"
    assert "65+" not in {r.category for r in breakdown(db_session, open_law, "age").rows}


def test_a_ballot_still_recording_at_the_closing_is_refused(
    client, open_law, db_session, monkeypatch
):
    """A ballot that crosses the closing is refused, so nothing lands after it."""
    original_record = demo_crud.record

    def record_then_close(db, *, vote, **kwargs):
        on_file = original_record(db, vote=vote, **kwargs)
        # The clock passes the closing while this ballot is still in its transaction.
        db.get(Law, vote.law_id).closes_at = datetime.now(UTC) - timedelta(seconds=1)
        return on_file

    monkeypatch.setattr(demo_crud, "record", record_then_close)
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for", "age_bracket": "35-49"},
        headers={"HX-Request": "true"},
    )
    assert "vient de se clore" in r.text
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 0
    assert demographic_cells(db_session, open_law.id).get(("age", "35-49", "for"), 0) == 0


def test_the_page_says_the_final_figures_are_coming_during_the_grace(open_law, db_session, client):
    """Right after the closing the figures aren't final yet, and the page says so."""
    for i in range(30):
        cast_ballot(db_session, open_law, f"grace-{i}", VoteChoice.FOR, age_bracket="25-34")
    open_law.closes_at = datetime.now(UTC) - timedelta(seconds=30)
    db_session.commit()

    page = " ".join(client.get(f"/loi/{open_law.slug}/resultats").text.split())
    assert "paraissent quelques minutes après la clôture" in page
    assert "Non publiée" not in page, "nothing was refused, it just isn't final yet"

    _close(db_session, open_law)
    page = " ".join(client.get(f"/loi/{open_law.slug}/resultats").text.split())
    assert "quelques minutes" not in page
