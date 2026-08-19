"""Behaviour that used to be wrong: timezones, error pages, labels, races."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event

from app.config import get_settings
from app.crud import laws as laws_crud
from app.crud import results as results_crud
from app.crud import votes as votes_crud
from app.models import Law, Vote, VoteChoice

PARIS = ZoneInfo("Europe/Paris")


def _admin_auth() -> dict:
    s = get_settings()
    token = base64.b64encode(f"{s.admin_username}:{s.admin_password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Origin": "http://testserver"}


# --- Scheduling in local time -------------------------------------------------


def test_a_closing_time_means_the_hour_the_admin_typed(client, db_session):
    """The form says 20:00; it used to close at 22:00 Paris."""
    client.post(
        "/admin/lois",
        headers=_admin_auth(),
        data={"title": "Loi horaire", "closes_at": "2026-07-01T20:00", "published": "1"},
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Loi horaire").one()
    assert law.closes_at.astimezone(PARIS).hour == 20
    # July is CEST (UTC+2), so the stored instant is 18:00 UTC.
    assert law.closes_at.astimezone(UTC).hour == 18


def test_winter_and_summer_are_both_right(client, db_session):
    """A fixed offset would be wrong for half the year."""
    client.post(
        "/admin/lois",
        headers=_admin_auth(),
        data={"title": "Loi hiver", "closes_at": "2026-01-15T20:00", "published": "1"},
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Loi hiver").one()
    # January is CET (UTC+1).
    assert law.closes_at.astimezone(UTC).hour == 19
    assert law.closes_at.astimezone(PARIS).hour == 20


def test_the_edit_form_shows_back_what_was_typed(client, db_session):
    client.post(
        "/admin/lois",
        headers=_admin_auth(),
        data={"title": "Aller-retour", "closes_at": "2026-07-01T20:00"},
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Aller-retour").one()
    form = client.get(f"/admin/lois/{law.id}", headers=_admin_auth())
    assert 'value="2026-07-01T20:00"' in form.text
    assert "heure de Paris" in form.text


def test_a_law_opens_at_the_local_hour_it_was_scheduled_for(db_session):
    """The status the public sees follows the same clock as the form."""
    opens = datetime(2026, 7, 1, 20, 0, tzinfo=PARIS)
    law = Law(slug="prog", title="Programmée", published=True, opens_at=opens)
    db_session.add(law)
    db_session.commit()
    assert law.status(now=datetime(2026, 7, 1, 19, 59, tzinfo=PARIS)) == "scheduled"
    assert law.status(now=datetime(2026, 7, 1, 20, 1, tzinfo=PARIS)) == "open"


# --- Malformed input ----------------------------------------------------------


def test_an_unreadable_date_is_a_message_not_a_crash(client, db_session):
    r = client.post(
        "/admin/lois",
        headers={**_admin_auth(), "Accept": "text/html"},
        data={"title": "Loi", "opens_at": "pas-une-date"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Saisie invalide" in r.text
    assert "pas-une-date" in r.text


def test_a_nonsense_import_id_is_rejected_not_fatal(client, db_session):
    r = client.post(
        "/admin/lois",
        headers=_admin_auth(),
        data={"title": "Loi", "import_id": "abc"},
        follow_redirects=False,
    )
    assert r.status_code == 422


# --- Error pages --------------------------------------------------------------


def test_an_unknown_url_returns_the_sites_own_page(client):
    r = client.get("/cette-page-nexiste-pas", headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Page introuvable" in r.text
    assert "Retour à l'accueil" in r.text


def test_an_api_client_still_gets_a_plain_answer(client):
    r = client.get("/cette-page-nexiste-pas", headers={"Accept": "application/json"})
    assert r.status_code == 404
    assert "<html" not in r.text


def test_a_401_keeps_its_challenge_header(client):
    """Turned into an HTML page, the browser would never show a login box."""
    r = client.get("/admin", headers={"Accept": "text/html"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_robots_and_favicon_exist(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /admin" in robots.text
    assert client.get("/favicon.ico").status_code == 200


# --- French labels ------------------------------------------------------------


def test_no_english_status_reaches_a_french_page(client, open_law, closed_law):
    home = client.get("/")
    assert "ouverte" in home.text
    # The machine value survives as a CSS hook, never as visible text.
    assert ">open<" not in home.text
    assert ">closed<" not in home.text

    closed = client.get(f"/loi/{closed_law.slug}")
    assert "close" in closed.text
    assert "Cette votation est closed" not in closed.text


def test_the_admin_import_states_are_french(client, db_session):
    from datetime import date

    from app.models import ImportedLaw

    db_session.add(
        ImportedLaw(
            source="assemblee",
            dossier_uid="D1",
            title="T",
            law_code="2026-1",
            promulgated_on=date(2026, 1, 1),
            dossier_url="https://an.fr",
        )
    )
    db_session.commit()
    r = client.get("/admin/imports", headers=_admin_auth())
    assert "nouvelle" in r.text
    assert ">nouveau<" not in r.text


# --- Paragraphs ---------------------------------------------------------------


def test_a_multi_paragraph_summary_renders_as_paragraphs(client, db_session):
    law = Law(
        slug="paras",
        title="Loi en trois temps",
        published=True,
        summary_text="Premier paragraphe.\n\nDeuxième paragraphe.\n\nTroisième.",
        opens_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(law)
    db_session.commit()
    body = client.get("/loi/paras").text
    section = body.split("<h2>Ce que dit la loi</h2>")[1].split("</section>")[0]
    assert section.count("<p>") == 3
    assert "Deuxième paragraphe." in section


# --- Concurrency --------------------------------------------------------------


def test_voting_twice_at_once_updates_instead_of_failing(db_session, open_law):
    """Read-then-insert raced against itself and hit the unique constraint."""
    token = "meme-navigateur"
    first = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.FOR)
    second = votes_crud.cast_vote(db_session, open_law, token, VoteChoice.AGAINST)
    assert first.id == second.id
    assert second.choice == "against"
    assert db_session.query(Vote).filter_by(law_id=open_law.id).count() == 1


# --- Import review state ------------------------------------------------------


def test_ignoring_an_imported_row_keeps_its_link_to_the_law(db_session, open_law):
    from datetime import date

    from app.crud import imports as imports_crud
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="D2",
        title="T",
        law_code="2026-2",
        promulgated_on=date(2026, 1, 1),
        dossier_url="https://an.fr",
    )
    db_session.add(item)
    db_session.commit()

    imports_crud.set_status(db_session, item, status="importe", law_id=open_law.id)
    assert item.law_id == open_law.id

    imports_crud.set_status(db_session, item, status="ignore")
    assert item.law_id == open_law.id, "ignoring a row must not orphan the law it produced"

    imports_crud.set_status(db_session, item, status="nouveau")
    assert item.law_id == open_law.id


# --- Search ------------------------------------------------------------------


def _publish(db_session, n: int) -> None:
    base = datetime.now(UTC)
    for i in range(n):
        db_session.add(
            Law(
                slug=f"loi-{i}",
                title=f"Loi numéro {i}",
                published=True,
                summary_text="texte",
                created_at=base - timedelta(minutes=i),
                opens_at=base - timedelta(hours=1),
            )
        )
    db_session.commit()


def test_search_pages_in_sql_not_in_python(db_session):
    """A page used to be sliced out of every published law loaded into memory."""
    _publish(db_session, 45)
    seen: list[str] = []
    bind = db_session.get_bind()

    def record(conn, cursor, statement, params, context, many):
        seen.append(" ".join(statement.split()))

    event.listen(bind, "before_cursor_execute", record)
    try:
        rows, total = laws_crud.search(db_session, page=2, page_size=20)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert total == 45
    assert len(rows) == 20
    selects = [s for s in seen if s.upper().startswith("SELECT")]
    assert any("LIMIT" in s for s in selects), "the window must be applied by the database"


def test_search_by_status_filters_correctly(db_session, open_law, closed_law):
    opened, _ = laws_crud.search(db_session, status="open")
    closed, _ = laws_crud.search(db_session, status="closed")
    assert [law.slug for law in opened] == [open_law.slug]
    assert [law.slug for law in closed] == [closed_law.slug]


def test_a_wildcard_typed_in_the_box_is_searched_for_literally(db_session):
    db_session.add(Law(slug="pourcent", title="Loi 100 % renouvelable", published=True))
    db_session.add(Law(slug="autre", title="Loi ordinaire", published=True))
    db_session.commit()
    _, total = laws_crud.search(db_session, query="100 %")
    assert total == 1
    # A bare "%" is a character to look for, not "match everything".
    hits, count = laws_crud.search(db_session, query="%")
    assert count == 1
    assert hits[0].slug == "pourcent"
    # "_" is the other LIKE wildcard: it must not stand for any character.
    assert laws_crud.search(db_session, query="Loi_ordinaire")[1] == 0


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


def test_the_dashboard_tallies_every_law_in_one_query(client, db_session, count_queries):
    _publish(db_session, 10)
    with count_queries() as seen:
        client.get("/admin", headers=_admin_auth())
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


# --- Import freshness ---------------------------------------------------------
#
# The import service retries forever on failure, so a source that moves — and the
# export URL carries the legislature number, so it does move — would stop the
# imports in silence. The dashboard is where that silence becomes visible.


def _stage_import(db_session, *, seen_at):
    from datetime import date

    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DFRESH",
        title="Loi importée",
        law_code="2026-9",
        promulgated_on=date(2026, 1, 1),
        dossier_url="https://an.fr",
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_a_fresh_import_shows_no_warning(client, db_session):
    _stage_import(db_session, seen_at=datetime.now(UTC) - timedelta(hours=1))
    page = client.get("/admin", headers=_admin_auth()).text
    assert "Import automatique en retard" not in page


def test_a_stale_import_is_announced_on_the_dashboard(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "an_import_stale_after_hours", 48, raising=False)
    _stage_import(db_session, seen_at=datetime.now(UTC) - timedelta(hours=72))

    page = client.get("/admin", headers=_admin_auth()).text
    assert "Import automatique en retard" in page
    # It names the service to look at, rather than leaving the reader guessing.
    assert "assemblee-refresh" in page


def test_never_having_imported_counts_as_stale(client, db_session):
    """An import that has never once succeeded is the loudest failure of all."""
    page = client.get("/admin", headers=_admin_auth()).text
    assert "Import automatique en retard" in page
    assert "Aucun import n'a encore abouti" in page
