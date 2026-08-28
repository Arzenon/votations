"""What the pages say, and how they say it.

Wording, ordering and markup that the charter is specific about: symmetry between
the two sides, a disclaimer on every page, régions a French reader can scan, and
tables a screen reader can navigate.
"""

from __future__ import annotations

import re
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
    db_session.commit()
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


# --- Saying who wrote the presentation (charter: state the limits) ------------


def _flat(html: str) -> str:
    """The page with its whitespace collapsed.

    These are assertions about prose, and prose in a template is wrapped to fit
    the file. Comparing against the raw HTML made every re-wrap a test failure
    about nothing — the sentence is the same sentence whether or not a newline
    fell in the middle of it.
    """
    return re.sub(r"\s+", " ", html)


def test_the_law_page_says_how_its_presentation_was_written(client, open_law):
    """The charter asks for the platform's limits to be permanent and prominent.

    The banner on every page covers who *votes* here. This covers who *wrote* the
    text being voted on — drafted with AI help from the official text, validated
    by one volunteer who is not a jurist. A reader deciding a vote on that basis
    is entitled to know it without going looking.
    """
    page = _flat(client.get(f"/loi/{open_law.slug}").text)

    assert "Qui a écrit cette présentation" in page
    assert escape("ni juriste, ni spécialiste du sujet") in page
    assert escape("(assistée par IA)") in page
    assert escape("référer aux textes officiels mis en lien") in page
    assert escape("cités mot pour mot") in page, "the arguments are quoted, not written"
    assert escape("député") in page, "the ambition is stated, not just the limitation"


def test_the_note_is_a_collapsed_disclosure_asking_the_question(client, open_law):
    """Folded away to save the page, behind the question it answers.

    The summary deliberately does *not* summarise the answer. « une IA, relue par
    une personne bénévole » was tried and removed: it reads as though the whole
    page were machine-written, when the arguments are quotations selected from
    real speeches and checked against them. A compressed answer that misdescribes
    the thing is worse than a question the reader has to open.
    """
    page = _flat(client.get(f"/loi/{open_law.slug}").text)

    assert '<details class="provenance">' in page, "not a native disclosure"
    assert '<details class="provenance" open' not in page, "it should start closed"

    summary = page.split("<summary>")[1].split("</summary>")[0].strip()
    assert summary == escape("Qui a écrit cette présentation ?"), (
        f"the visible line must be the question alone, got {summary!r}"
    )


def test_the_note_needs_no_javascript(client, open_law):
    """`<details>` is HTML, not a script: it opens with scripting switched off.

    The public pages are built for that, and this one is load-bearing — a
    limitation nobody can unfold is a limitation nobody was told.
    """
    page = client.get(f"/loi/{open_law.slug}").text
    assert "<details" in page
    assert "provenance" not in page.split("<body")[0], "no script drives it"


def test_the_note_sits_before_the_ballot(client, open_law):
    """It is the last thing read before voting, not a footnote after it."""
    page = _flat(client.get(f"/loi/{open_law.slug}").text)
    assert page.index("Qui a écrit cette présentation") < page.index("<h2>Voter</h2>")


def test_an_error_can_be_reported_when_a_contact_is_configured(client, open_law, monkeypatch):
    """A page that admits it may be wrong has to say where to say so."""
    settings = get_settings()
    monkeypatch.setattr(settings, "legal_contact_email", "contact@exemple.fr", raising=False)
    assert "mailto:contact@exemple.fr" in client.get(f"/loi/{open_law.slug}").text


def test_no_contact_no_broken_offer(client, open_law, monkeypatch):
    """Left unset, the offer disappears rather than pointing at nothing."""
    monkeypatch.setattr(get_settings(), "legal_contact_email", "", raising=False)
    page = _flat(client.get(f"/loi/{open_law.slug}").text)
    assert "mailto:" not in page
    assert "Qui a écrit cette présentation" in page, "the note itself must remain"
