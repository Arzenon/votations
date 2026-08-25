"""Work the pages must *not* do.

Asserted by counting the statements a request sends, because these are the
regressions nobody notices until a votation gets popular: a page that reads every
vote to render a ballot, or a listing that fetches prose to draw a table of
titles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.models import Vote
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
    """It used to fetch every vote row for an evolution curve it never rendered."""
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


# --- B7 / C8: cheaper public pages -------------------------------------------


def test_an_absurd_page_number_is_clamped(client, db_session):
    make_law(db_session, slug="loi-p", title="Loi paginée")
    r = client.get("/recherche?page=999999999")
    assert r.status_code == 200
    assert "Page 1 / 1" in r.text or "Loi paginée" in r.text


def test_a_listing_that_shows_no_excerpt_does_not_fetch_one(db_session, count_queries):
    """The search results and the admin table show titles, references and dates.

    They were fetching whole summaries to throw them away. Deferred rather than
    dropped, so an attribute touched anyway still works — it just costs a query.
    """
    publish_laws(db_session, 3)
    with count_queries() as seen:
        laws_crud.search(db_session, query=None, status=None, page=1)
    selects = [s for s in seen if s.lower().startswith("select") and " laws" in s.lower()]
    assert selects, "the search really did query the laws table"
    assert not any("summary_text" in s for s in selects), (
        "summary_text is fetched for a listing that never renders it"
    )


def test_the_home_page_still_gets_its_excerpt(db_session, count_queries):
    """The one listing that *does* show an excerpt must keep the column.

    Two windows rather than one: the listing is allowed to select the column, and
    what must not happen is a *second* statement when the attribute is read.
    """
    publish_laws(db_session, 3)
    with count_queries() as listing:
        rows = laws_crud.list_published(db_session)
    assert any("summary_text" in s for s in listing), "the listing did not select it"

    with count_queries() as touching:
        assert rows[0].summary_text == "texte"
    assert not touching, f"reading the excerpt cost {len(touching)} extra queries"


def test_recording_a_vote_reads_it_back_once(db_session, open_law, count_queries):
    """`get` followed by `refresh` fetched the same row twice, on every vote."""
    from app.crud import votes as votes_crud
    from app.models import VoteChoice

    with count_queries() as seen:
        votes_crud.cast_vote(db_session, open_law, "jeton-anonyme", VoteChoice.FOR)
    reads = [s for s in seen if s.lower().startswith("select") and " votes" in s.lower()]
    assert len(reads) == 1, f"{len(reads)} reads of the vote row: {reads}"
