"""Work the pages must not do, checked by counting SQL statements."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event

from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.models import Law, Vote
from tests.helpers import admin_headers, make_law, publish_laws

# --- Query counts -------------------------------------------------------------


@pytest.fixture
def count_queries(db_session):
    """Count the statements one block of code sends."""
    from contextlib import contextmanager

    @contextmanager
    def counter():
        seen: list[str] = []
        bind = db_session.get_bind()

        def record(conn, cursor, statement, params, context, many):
            seen.append(" ".join(statement.split()))

        event.listen(bind, "before_cursor_execute", record)
        try:
            yield seen
        finally:
            event.remove(bind, "before_cursor_execute", record)

    return counter


def test_the_law_page_does_not_read_every_vote(client, db_session, open_law, count_queries):
    """The law page doesn't fetch the votes."""
    for i in range(60):
        db_session.add(Vote(law_id=open_law.id, voter_token=f"v{i}", choice="for"))
    db_session.commit()

    with count_queries() as seen:
        client.get(f"/loi/{open_law.slug}")

    full_scans = [s for s in seen if "votes.voter_token" in s and "count" not in s.lower()]
    assert full_scans == [], f"the busiest page must not load vote rows: {full_scans}"


def test_an_open_law_page_does_not_count_the_votes(client, open_law, db_session):
    """The tally is only rendered once voting has closed."""
    from sqlalchemy import event

    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", record)
    try:
        client.get(f"/loi/{open_law.slug}")
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", record)

    tallies = [s for s in seen if "count" in s.lower() and "votes" in s.lower()]
    assert not any("group by" in s.lower() for s in tallies)


def test_the_dashboard_tallies_every_law_in_one_query(client, db_session, count_queries):
    publish_laws(db_session, 10)
    with count_queries() as seen:
        client.get("/admin", headers=admin_headers())
    tally_queries = [s for s in seen if "GROUP BY votes.law_id" in s]
    assert len(tally_queries) == 1, "one grouped query, not one per law"


def test_the_evolution_curve_is_bucketed_by_the_database(db_session, open_law, count_queries):
    base = datetime.now(UTC) - timedelta(days=3)
    for i in range(60):
        db_session.add(
            Vote(
                law_id=open_law.id,
                voter_token=f"e{i}",
                choice="for",
                created_at=base + timedelta(hours=i),
            )
        )
    db_session.commit()

    with count_queries() as seen:
        buckets = results_crud.evolution(db_session, open_law.id)

    assert buckets, "60 votes clears the anonymity gate"
    assert sum(sum(b.counts.values()) for b in buckets) == 60
    assert any("date_trunc" in s for s in seen)
    assert not any("votes.voter_token" in s for s in seen), "no row-by-row fetch"


# --- Cheaper public pages ---------------------------------------------------


def test_an_absurd_page_number_is_clamped(client, db_session):
    make_law(db_session, slug="loi-p", title="Loi paginée")
    r = client.get("/recherche?page=999999999")
    assert r.status_code == 200
    assert "Page 1 / 1" in r.text or "Loi paginée" in r.text


def test_a_listing_that_shows_no_excerpt_does_not_fetch_one(db_session, count_queries):
    """Search results and the admin table don't load the summaries."""
    publish_laws(db_session, 3)
    with count_queries() as seen:
        laws_crud.search(db_session, query=None, status=None, page=1)
    selects = [s for s in seen if s.lower().startswith("select") and " laws" in s.lower()]
    assert selects, "the search really did query the laws table"
    assert not any("summary_text" in s for s in selects), (
        "summary_text is fetched for a listing that never renders it"
    )


def test_the_home_page_gets_a_cut_excerpt_not_the_whole_summary(db_session, count_queries):
    """The home page excerpt is cut by the database, without an extra query."""
    publish_laws(db_session, 3)
    with count_queries() as listing:
        rows = laws_crud.list_published(db_session)

    assert any("left(" in s.lower() for s in listing), "the excerpt was not cut in SQL"
    assert not any(
        re.search(r"laws\.summary_text(?!\s*,)", s) and "left(" not in s.lower() for s in listing
    ), "the whole summary column was still selected"

    with count_queries() as touching:
        assert rows[0].excerpt == "texte"
    assert not touching, f"reading the excerpt cost {len(touching)} extra queries"


def test_the_excerpt_says_when_there_is_more(db_session):
    """The excerpt is one character longer than shown, to detect truncation."""
    long_summary = "x" * (laws_crud.EXCERPT_CHARS + 500)
    make_law(db_session, slug="loi-longue", title="Loi longue", summary_text=long_summary)
    row = next(r for r in laws_crud.list_published(db_session) if r.slug == "loi-longue")
    assert row.excerpt is not None
    assert len(row.excerpt) == laws_crud.EXCERPT_CHARS + 1, (
        "one character past what is displayed is what tells the page to trail off"
    )


def test_the_home_page_trails_off_at_the_length_the_database_cut(client, db_session):
    """The template truncates at the same length as the SQL."""
    cut = laws_crud.EXCERPT_CHARS
    make_law(db_session, slug="loi-longue", title="Loi longue", summary_text="a" * (cut + 500))
    make_law(db_session, slug="loi-courte", title="Loi courte", summary_text="b" * (cut - 5))

    body = client.get("/").text
    assert "a" * cut + "…" in body, "a summary longer than the cut trails off at the cut"
    assert "a" * (cut + 1) not in body, "and the overshoot character is never displayed"
    assert "b" * (cut - 5) in body and "b" * (cut - 5) + "…" not in body, "short: no ellipsis"


def test_recording_a_vote_reads_it_back_once(db_session, open_law, count_queries):
    """Casting a vote reads the row back once."""
    from app.crud import votes as votes_crud
    from app.models import VoteChoice

    with count_queries() as seen:
        votes_crud.cast_vote(db_session, open_law, "jeton-anonyme", VoteChoice.FOR)
    reads = [s for s in seen if s.lower().startswith("select") and " votes" in s.lower()]
    assert len(reads) == 1, f"{len(reads)} reads of the vote row: {reads}"


def test_the_results_page_walks_the_votes_once_for_both_counts(
    client, db_session, open_law, count_queries
):
    """The tally and the profile rate come from a single scan."""
    for i in range(60):
        db_session.add(
            Vote(
                law_id=open_law.id,
                voter_token=f"p{i}",
                choice="for",
                demographics_recorded=(i % 2 == 0),
            )
        )
    db_session.commit()

    with count_queries() as seen:
        r = client.get(f"/loi/{open_law.slug}/resultats")
    assert r.status_code == 200

    counting = [
        s
        for s in seen
        if "from votes" in s.lower() and "count" in s.lower() and "date_trunc" not in s.lower()
    ]
    assert len(counting) == 1, f"{len(counting)} scans of the votes to count them: {counting}"


def test_the_tally_carries_how_many_voters_shared_a_profile(db_session, open_law):
    """The tally includes the number of profiled votes."""
    from app.crud import demographics as demo_crud

    for i in range(10):
        db_session.add(
            Vote(
                law_id=open_law.id,
                voter_token=f"q{i}",
                choice="for",
                demographics_recorded=(i < 4),
            )
        )
    db_session.commit()

    tally = results_crud.tally(db_session, open_law.id)
    assert (tally.total, tally.with_profile) == (10, 4)

    provision = demo_crud.provision_rate(db_session, open_law.id, tally=tally)
    assert (provision.total_votes, provision.with_profile, provision.percentage) == (10, 4, 40.0)


def test_deriving_the_profile_rate_from_a_tally_costs_nothing(db_session, open_law, count_queries):
    from app.crud import demographics as demo_crud

    db_session.add(Vote(law_id=open_law.id, voter_token="r0", choice="for"))
    db_session.commit()
    tally = results_crud.tally(db_session, open_law.id)

    with count_queries() as seen:
        demo_crud.provision_rate(db_session, open_law.id, tally=tally)
    assert not seen, f"handing the tally over still cost {len(seen)} queries"


def test_a_colliding_slug_is_resolved_in_one_query(db_session, count_queries):
    """A slug collision is resolved in one query."""
    for n in range(6):
        make_law(db_session, slug="loi-x" if n == 0 else f"loi-x-{n + 1}", title=f"Loi X {n}")

    with count_queries() as seen:
        slug = laws_crud.unique_slug(db_session, "Loi X")
    assert slug == "loi-x-7"
    assert len(seen) == 1, f"{len(seen)} queries to pick one slug"


def test_a_very_long_title_leaves_room_for_its_suffix(db_session):
    """A very long slug leaves room for the "-2" suffix."""
    # Long enough to need trimming, within laws.title's 500 characters.
    title = "Loi " + "extraordinairement longue " * 16
    first = laws_crud.unique_slug(db_session, title)
    make_law(db_session, slug=first, title=title)
    second = laws_crud.unique_slug(db_session, title)

    assert second != first
    width = Law.__table__.c["slug"].type.length
    assert len(second) <= width, "the second slug does not fit the column"


def test_the_home_page_counts_every_law_s_votes_in_one_query(client, db_session, count_queries):
    """Home page participation counts take a single query."""
    from tests.helpers import publish_laws

    publish_laws(db_session, 12)
    with count_queries() as seen:
        client.get("/")
    counted = [q for q in seen if "count" in q.lower() and " votes" in q.lower()]
    assert len(counted) <= 1, f"{len(counted)} count queries: {counted}"


# --- The memo: never staler than the page already says it may be --------------


def test_the_home_page_does_not_recount_inside_its_own_cache_window(
    client, db_session, count_queries
):
    """Within its cache window, the home page doesn't count again."""
    publish_laws(db_session, 5)
    client.get("/")  # warms it

    with count_queries() as seen:
        client.get("/")
    counted = [q for q in seen if "count" in q.lower() and " votes" in q.lower()]
    assert counted == [], f"the page counted the votes again: {counted}"


def test_a_ballot_makes_the_home_page_count_that_law_again(client, db_session, open_law):
    """A ballot invalidates the memo for its law."""
    assert "Aucun vote" in client.get("/").text
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert "1 vote" in client.get("/").text


def test_an_open_votation_is_not_recounted_for_every_visitor(client, open_law, count_queries):
    """A shared law's results page doesn't scan its votes on each view."""
    client.get(f"/loi/{open_law.slug}/resultats")
    with count_queries() as seen:
        client.get(f"/loi/{open_law.slug}/resultats")
    touched = [q for q in seen if "from votes" in q.lower()]
    assert touched == [], f"an open vote was counted again within its memo: {touched}"


@pytest.mark.parametrize("stored", ["before the commit", "after the last forget"])
def test_a_voter_sees_their_ballot_on_the_results_page_right_away(client, open_law, stored):
    """Even if a page counted the votes during the ballot and memoised that count."""
    real_forget = results_crud.forget
    seen: list[int] = []

    def forget(law_id: int) -> None:
        real_forget(law_id)
        if not seen:
            # The call before the commit: a reader starts counting here.
            seen.append(results_crud._TALLIES.version(law_id))
            if stored == "before the commit":
                results_crud._TALLIES.put(law_id, results_crud.empty_tally(), seen[0])

    with patch.object(results_crud, "forget", forget):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    if stored == "after the last forget":
        results_crud._TALLIES.put(open_law.id, results_crud.empty_tally(), seen[0])
    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "Nombre total de votant·e·s : <strong>1</strong>" in page


def test_a_closed_votation_reads_its_results_from_the_memo(client, closed_law, count_queries):
    """Results of a closed vote are memoised."""
    client.get(f"/loi/{closed_law.slug}/resultats")
    with count_queries() as seen:
        client.get(f"/loi/{closed_law.slug}/resultats")
    touched = [q for q in seen if "from votes" in q.lower()]
    assert touched == [], f"a closed vote was counted again: {touched}"


def test_the_memo_window_is_the_one_the_page_promises():
    """The memo duration equals the page's max-age."""
    from app.models import STATUS_CLOSED, STATUS_OPEN
    from app.routes.public.common import (
        CLOSED_RESULTS_CACHE,
        OPEN_RESULTS_MAX_AGE,
        PUBLIC_PAGE_CACHE,
        PUBLIC_PAGE_MAX_AGE,
        results_memo_age,
    )

    assert PUBLIC_PAGE_CACHE["Cache-Control"] == f"public, max-age={PUBLIC_PAGE_MAX_AGE}"
    assert 0 < results_memo_age(STATUS_OPEN) <= OPEN_RESULTS_MAX_AGE
    assert CLOSED_RESULTS_CACHE["Cache-Control"] == (
        f"public, max-age={results_memo_age(STATUS_CLOSED):.0f}"
    )


# --- The public listings must stay on ix_laws_home ----------------------------


def test_the_public_visibility_filter_can_use_its_partial_index():
    """The filter uses `published`, not `published IS true`.

    Postgres won't use the partial index ix_laws_home with `IS true`
    (0.3 ms vs 26.8 ms at 50,000 laws).
    """
    from sqlalchemy import select

    from app.crud.laws import _published
    from app.models import Law

    where = str(_published(select(Law.id)).compile(compile_kwargs={"literal_binds": True}))
    assert "laws.published AND" in where
    assert "published IS true" not in where.lower()


def test_the_index_the_listings_rely_on_is_declared():
    """ix_laws_home is declared on the model with the expected predicate."""
    from sqlalchemy import text

    from app.models import Law

    index = next(i for i in Law.__table__.indexes if i.name == "ix_laws_home")
    where = index.dialect_options["postgresql"]["where"]
    assert str(text("published AND archived_at IS NULL")) == str(where)


def test_a_closed_votation_with_a_snapshot_costs_no_extra_scan(
    client, closed_law, db_session, count_queries
):
    """A closed vote with a snapshot needs no extra scan (uses Tally.with_profile)."""
    for i in range(30):
        db_session.add(
            Vote(
                law_id=closed_law.id,
                voter_token=f"snap{i}",
                choice="for",
                demographics_recorded=True,
            )
        )
    closed_law.demographics_snapshot_at = datetime.now(UTC) - timedelta(days=2)
    closed_law.demographics_snapshot_profiles = 10
    db_session.commit()

    with count_queries() as seen:
        r = client.get(f"/loi/{closed_law.slug}/resultats")
    assert r.status_code == 200
    counting = [
        s
        for s in seen
        if "from votes" in s.lower() and "count" in s.lower() and "date_trunc" not in s.lower()
    ]
    assert len(counting) == 1, f"{len(counting)} scans to count the votes: {counting}"
