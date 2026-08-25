"""What the pages say, and how they say it.

Wording, ordering and markup that the charter is specific about: symmetry between
the two sides, a disclaimer on every page, régions a French reader can scan, and
tables a screen reader can navigate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from markupsafe import escape

from app.config import get_settings
from app.crud import votes as votes_crud
from app.models import Law, VoteChoice
from tests.helpers import admin_headers, make_law

# --- Éditorial : ordre des régions et cohérence des chaînes -------------------


def test_regions_are_offered_in_alphabetical_order(client, open_law):
    """Fourteen régions are scanned for one's own, not read top to bottom."""
    from app.models import DIMENSION_ORDER, DIMENSION_REGION

    shown = DIMENSION_ORDER[DIMENSION_REGION]
    assert shown[0] == "Auvergne-Rhône-Alpes"
    assert shown[-1] == "Provence-Alpes-Côte d'Azur"
    # The accented name files under I, not after Z as a raw sort would put it.
    assert shown.index("Île-de-France") == shown.index("Hauts-de-France") + 1

    # And that is the order the ballot actually renders. Compared against the
    # escaped form: "Provence-Alpes-Côte d'Azur" reaches the page as d&#39;Azur.
    page = client.get(f"/loi/{open_law.slug}").text
    positions = [page.index(f'value="{escape(r)}"') for r in shown]
    assert positions == sorted(positions)


def test_reordering_regions_never_moves_a_stored_value():
    """Display order is presentation; the values are in the counters and exports."""
    from app.models import Region

    assert {r.value for r in Region} == {
        "Île-de-France",
        "Auvergne-Rhône-Alpes",
        "Hauts-de-France",
        "Nouvelle-Aquitaine",
        "Occitanie",
        "Grand Est",
        "Provence-Alpes-Côte d'Azur",
        "Pays de la Loire",
        "Bretagne",
        "Normandie",
        "Bourgogne-Franche-Comté",
        "Centre-Val de Loire",
        "Corse",
        "Outre-mer",
    }


def test_the_tagline_is_written_once(client):
    """The meta description and the home lede used to be two near-identical copies."""
    from app.templating import TAGLINE

    home = client.get("/").text
    # Three renders from one constant: <meta name="description">, the og:description
    # that mirrors it, and the lede. Previously two of these were separate prose.
    assert home.count(str(escape(TAGLINE))) == 3


def test_inclusive_spelling_is_uniform(client, open_law, db_session):
    """One form throughout: ·e·s in the plural."""

    votes_crud.cast_vote(db_session, open_law, "jeton", VoteChoice.FOR)
    pages = [client.get("/").text, client.get(f"/loi/{open_law.slug}/resultats").text]
    for page in pages:
        assert "votant·es" not in page
        assert "participant·es" not in page


def test_the_representativeness_warning_appears_once_per_page(client, open_law):
    """It is in the banner at the top; repeating it at the foot read as insistence."""
    from app.templating import DISCLAIMER

    for url in ("/", f"/loi/{open_law.slug}", f"/loi/{open_law.slug}/resultats"):
        assert client.get(url).text.count(str(escape(DISCLAIMER))) == 1


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


# --- Accessibilité ------------------------------------------------------------


def test_result_bars_are_hidden_from_screen_readers(client, closed_law):
    """The bar is a picture of the number beside it; announcing both repeats it."""
    page = client.get(f"/loi/{closed_law.slug}").text
    assert 'class="bar" aria-hidden="true"' in page


def test_data_tables_name_their_rows_and_columns(client, open_law, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 1, raising=False)
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert 'scope="col"' in page
    assert 'scope="row"' in page


def test_links_that_open_a_new_tab_say_so(client, db_session):
    law = make_law(
        db_session,
        slug="loi-liens",
        official_ref="LOI n° 2026-1",
        legifrance_url="https://example.org/loi",
        arguments_for=[{"text": "t", "author": "A", "source": "https://example.org/s"}],
    )
    page = client.get(f"/loi/{law.slug}").text
    assert "(nouvel onglet)" in page


# --- Instants are shown in the zone they were entered in ----------------------


def test_a_closing_date_is_shown_in_paris_not_in_utc(client, db_session):
    """A vote closing just after midnight used to announce the previous day.

    `closes_at` is a `timestamptz` and comes back in UTC, so formatting it
    directly published 14/01 for a votation the administrator had scheduled to
    close on the 15th at 00:30 Paris. Everything a reader sees goes through the
    French date filters, which convert first.
    """
    law = make_law(
        db_session,
        slug="loi-minuit",
        title="Loi de minuit",
        # 00:30 on 15 January in Paris is 23:30 on the 14th in UTC.
        closes_at=datetime(2027, 1, 14, 23, 30, tzinfo=UTC),
        opens_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    body = client.get("/").text
    assert "15 janvier 2027" in body
    assert "14 janvier 2027" not in body
    assert law.slug in body


def test_the_dashboard_shows_the_hour_the_administrator_typed(client, db_session):
    """The form takes Paris time; the dashboard used to read it back in UTC."""
    make_law(
        db_session,
        slug="loi-vingt-heures",
        title="Loi de 20 h",
        closes_at=datetime(2027, 6, 30, 18, 0, tzinfo=UTC),  # 20:00 in Paris, summer
    )
    body = client.get("/admin", headers=admin_headers()).text
    assert "30/06/2027 à 20:00" in body
    assert "30/06/2027 à 18:00" not in body
