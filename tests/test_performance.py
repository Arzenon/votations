"""Work the pages must *not* do.

Asserted by counting the statements a request sends, because these are the
regressions nobody notices until a votation gets popular: a page that reads every
vote to render a ballot, or a listing that fetches prose to draw a table of
titles.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

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


def test_the_home_page_gets_a_cut_excerpt_not_the_whole_summary(db_session, count_queries):
    """The one listing that shows an excerpt asks the database to cut it.

    `summary_text` has no width in the schema and the admin form accepts 20 000
    characters, so selecting it to render two lines made the cost of the busiest
    page depend on the longest text anyone had ever pasted. `left()` bounds it.

    Two windows rather than one: what must not happen is a *second* statement
    when the attribute is read — an excerpt that costs a query per card would be
    a worse trade than the one it replaced.
    """
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
    """The template adds an ellipsis by length, so the cut must overshoot by one."""
    long_summary = "x" * (laws_crud.EXCERPT_CHARS + 500)
    make_law(db_session, slug="loi-longue", title="Loi longue", summary_text=long_summary)
    row = next(r for r in laws_crud.list_published(db_session) if r.slug == "loi-longue")
    assert row.excerpt is not None
    assert len(row.excerpt) == laws_crud.EXCERPT_CHARS + 1, (
        "one character past what is displayed is what tells the page to trail off"
    )


def test_the_home_page_trails_off_at_the_length_the_database_cut(client, db_session):
    """And the page must cut at the same number the SQL did.

    It held that number as a literal, twice — once to slice and once to decide
    the ellipsis — against a constant that lives in Python. The overshoot of one
    only means anything while the two agree.
    """
    cut = laws_crud.EXCERPT_CHARS
    make_law(db_session, slug="loi-longue", title="Loi longue", summary_text="a" * (cut + 500))
    make_law(db_session, slug="loi-courte", title="Loi courte", summary_text="b" * (cut - 5))

    body = client.get("/").text
    assert "a" * cut + "…" in body, "a summary longer than the cut trails off at the cut"
    assert "a" * (cut + 1) not in body, "and the overshoot character is never displayed"
    assert "b" * (cut - 5) in body and "b" * (cut - 5) + "…" not in body, "short: no ellipsis"


def test_recording_a_vote_reads_it_back_once(db_session, open_law, count_queries):
    """`get` followed by `refresh` fetched the same row twice, on every vote."""
    from app.crud import votes as votes_crud
    from app.models import VoteChoice

    with count_queries() as seen:
        votes_crud.cast_vote(db_session, open_law, "jeton-anonyme", VoteChoice.FOR)
    reads = [s for s in seen if s.lower().startswith("select") and " votes" in s.lower()]
    assert len(reads) == 1, f"{len(reads)} reads of the vote row: {reads}"


def test_the_results_page_walks_the_votes_once_for_both_counts(
    client, db_session, open_law, count_queries
):
    """The tally and the profile rate are two questions about the same rows.

    They were two scans of every vote on the law, on a page that already makes
    three. One `GROUP BY` with a filtered count answers both.
    """
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
    """Counted in the same pass, so `provision_rate` needs no query of its own."""
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
    """It used to ask "is `loi-x-2` taken? `-3`? `-4`?", one round trip each."""
    for n in range(6):
        make_law(db_session, slug="loi-x" if n == 0 else f"loi-x-{n + 1}", title=f"Loi X {n}")

    with count_queries() as seen:
        slug = laws_crud.unique_slug(db_session, "Loi X")
    assert slug == "loi-x-7"
    assert len(seen) == 1, f"{len(seen)} queries to pick one slug"


def test_a_very_long_title_leaves_room_for_its_suffix(db_session):
    """`slugify` cut at the column's width, leaving none for the `-2`."""
    # Long enough that the slug needs trimming, short enough for `laws.title`
    # (500) — the admin form refuses anything longer anyway.
    title = "Loi " + "extraordinairement longue " * 16
    first = laws_crud.unique_slug(db_session, title)
    make_law(db_session, slug=first, title=title)
    second = laws_crud.unique_slug(db_session, title)

    assert second != first
    width = Law.__table__.c["slug"].type.length
    assert len(second) <= width, "the second slug does not fit the column"


def test_the_home_page_counts_every_law_s_votes_in_one_query(client, db_session, count_queries):
    """Participation on the cards must not cost one query per card.

    The administration's dashboard already learned this — `tallies_for` groups the
    whole page's counts into a single scan — and the home page now shows the same
    figure, so it uses the same call rather than a tally per law.
    """
    from tests.helpers import publish_laws

    publish_laws(db_session, 12)
    with count_queries() as seen:
        client.get("/")
    counted = [q for q in seen if "count" in q.lower() and " votes" in q.lower()]
    assert len(counted) <= 1, f"{len(counted)} requêtes de comptage : {counted}"


# --- The memo: never staler than the page already says it may be --------------


def test_the_home_page_does_not_recount_inside_its_own_cache_window(
    client, db_session, count_queries
):
    """One query is not the same thing as a cheap one.

    Counting the participation of every listed law walks every vote ever cast on
    any of them, and nothing in the shipped stack caches on the app's behalf. The
    page already tells its reader the figures may be a minute old; serving them
    from memory for that same minute tells nobody anything new.
    """
    publish_laws(db_session, 5)
    client.get("/")  # warms it

    with count_queries() as seen:
        client.get("/")
    counted = [q for q in seen if "count" in q.lower() and " votes" in q.lower()]
    assert counted == [], f"la page a recompté les votes : {counted}"


def test_a_ballot_makes_the_home_page_count_that_law_again(client, db_session, open_law):
    """The window is the backstop, not the mechanism.

    A vote drops the law it was cast on, so the figure a reader sees is not
    merely fresh-ish but current — and a busy site re-counts the one votation
    being voted on rather than all of them.
    """
    assert "Aucun vote" in client.get("/").text
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    assert "1 vote" in client.get("/").text


def test_an_open_votation_recounts_its_results_every_time(client, open_law, count_queries):
    """The one page that must not be memoised, and why.

    Its thirty seconds of caching are aimed at one reader's own browser and were
    chosen so somebody who has just voted still sees their ballot. A memo shared
    by every reader would spend that budget on somebody else's request.
    """
    client.get(f"/loi/{open_law.slug}/resultats")
    with count_queries() as seen:
        client.get(f"/loi/{open_law.slug}/resultats")
    assert [q for q in seen if "from votes" in q.lower()], (
        "les résultats d'une votation ouverte doivent être comptés en direct"
    )


def test_a_closed_votation_reads_its_results_from_the_memo(client, closed_law, count_queries):
    """Nothing about a closed votation can move, so both scans are repetition."""
    client.get(f"/loi/{closed_law.slug}/resultats")
    with count_queries() as seen:
        client.get(f"/loi/{closed_law.slug}/resultats")
    touched = [q for q in seen if "from votes" in q.lower()]
    assert touched == [], f"une votation close a été recomptée : {touched}"


def test_the_memo_window_is_the_one_the_page_promises():
    """Two numbers that must be the same, in two files that cannot see each other."""
    from app.models import STATUS_CLOSED, STATUS_OPEN
    from app.routes.public.common import (
        CLOSED_RESULTS_CACHE,
        PUBLIC_PAGE_CACHE,
        PUBLIC_PAGE_MAX_AGE,
        results_memo_age,
    )

    assert PUBLIC_PAGE_CACHE["Cache-Control"] == f"public, max-age={PUBLIC_PAGE_MAX_AGE}"
    assert results_memo_age(STATUS_OPEN) == 0.0
    assert CLOSED_RESULTS_CACHE["Cache-Control"] == (
        f"public, max-age={results_memo_age(STATUS_CLOSED):.0f}"
    )


# --- The public listings must stay on ix_laws_home ----------------------------


def test_the_public_visibility_filter_can_use_its_partial_index():
    """`published`, never `published IS true` — and this is why.

    Postgres applies a partial index only where it can *prove* the query's WHERE
    implies the index predicate, and it cannot prove that of ``IS true``. The
    explicit spelling therefore disqualified `ix_laws_home` silently: identical
    results, identical rows, and every public listing back to reading the whole
    table and sorting it — 26.8 ms against 0.3 ms at fifty thousand laws.

    Tidying `_published` back to `.is_(True)` is exactly the change a reviewer
    would wave through, so it is asserted here rather than left to a comment.
    """
    from sqlalchemy import select

    from app.crud.laws import _published
    from app.models import Law

    where = str(_published(select(Law.id)).compile(compile_kwargs={"literal_binds": True}))
    assert "laws.published AND" in where
    assert "published IS true" not in where.lower()


def test_the_index_the_listings_rely_on_is_declared():
    """Its two halves live in different files; neither works without the other."""
    from sqlalchemy import text

    from app.models import Law

    index = next(i for i in Law.__table__.indexes if i.name == "ix_laws_home")
    where = index.dialect_options["postgresql"]["where"]
    assert str(text("published AND archived_at IS NULL")) == str(where)


def test_a_closed_votation_with_a_snapshot_costs_no_extra_scan(
    client, closed_law, db_session, count_queries
):
    """Deciding whether the last step may be published must be free.

    A closed votation that took a snapshot while it was open has to compare its
    profiled count against the one recorded at that snapshot — see
    `demo_crud.publishes_live_counters`. That number is `Tally.with_profile`,
    which this page already counts, so both the breakdowns and their date are
    decided from it. Asking the database again would have put a second scan back
    on the page the memo exists to keep off.
    """
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
