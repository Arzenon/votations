"""Page metadata: canonical URLs, robots, structured data, HEAD."""

from __future__ import annotations

import json
import re

from tests.helpers import make_law

# --- Canonical and og:url -----------------------------------------------------


def _tag(html: str, pattern: str) -> str | None:
    found = re.search(pattern, html)
    return found.group(1) if found else None


def canonical(html: str) -> str | None:
    return _tag(html, r'<link rel="canonical" href="([^"]+)"')


def og_url(html: str) -> str | None:
    return _tag(html, r'<meta property="og:url" content="([^"]+)"')


def robots(html: str) -> str | None:
    return _tag(html, r'<meta name="robots" content="([^"]+)"')


def structured_data(html: str) -> dict:
    """The page's JSON-LD, parsed (a parse error would mean broken escaping)."""
    block = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert block, "no structured data block on the page"
    return json.loads(block.group(1))


def nodes_of(html: str, kind: str) -> list[dict]:
    return [n for n in structured_data(html)["@graph"] if n["@type"] == kind]


def test_the_canonical_url_drops_the_query_string(client, db_session):
    """The canonical URL has no query string (e.g. ?revisions=)."""
    law = make_law(db_session, slug="loi-canon", title="Loi canon")
    page = client.get(f"/loi/{law.slug}/resultats?revisions=3").text
    assert canonical(page) == "http://testserver/loi/loi-canon/resultats"


def test_og_url_says_exactly_what_the_canonical_says(client):
    """og:url equals the canonical URL."""
    page = client.get("/vie-privee").text
    assert canonical(page) == "http://testserver/vie-privee"
    assert og_url(page) == canonical(page)


def test_every_public_page_states_where_it_lives(client, db_session):
    law = make_law(db_session, slug="loi-partout", title="Loi partout")
    for path in ("/", "/connexion", "/mentions-legales", f"/loi/{law.slug}"):
        assert canonical(client.get(path).text) == f"http://testserver{path}", path


# --- What must not be indexed -------------------------------------------------


def test_the_search_page_refuses_indexing_but_keeps_its_links(client):
    """The search page is noindex, follow."""
    assert robots(client.get("/recherche?q=retraites&page=2").text) == "noindex, follow"


def test_the_pages_that_are_indexed_say_nothing_about_robots(client, db_session):
    """Indexed pages have no robots meta."""
    law = make_law(db_session, slug="loi-indexee", title="Loi indexée")
    for path in ("/", "/vie-privee", f"/loi/{law.slug}", f"/loi/{law.slug}/resultats"):
        assert robots(client.get(path).text) is None, path


def test_the_sitemap_does_not_offer_a_page_it_refuses_to_index(client):
    """The sitemap doesn't list the noindex search page."""
    assert "/recherche" not in client.get("/sitemap.xml").text


# --- Structured data ----------------------------------------------------------


def test_the_home_page_declares_the_site_and_where_a_search_goes(client):
    site = nodes_of(client.get("/").text, "WebSite")[0]
    assert site["url"] == "http://testserver/"
    target = site["potentialAction"]["target"]["urlTemplate"]
    assert target == "http://testserver/recherche?q={search_term_string}"


def test_a_law_is_described_as_legislation(client, db_session):
    """A law is described as schema.org Legislation."""
    from datetime import date

    law = make_law(
        db_session,
        slug="loi-structuree",
        title="Loi structurée",
        summary_text="Ce que dit la loi.",
        official_ref="LOI n° 2026-532 du 24 juin 2026",
        legifrance_url="https://www.legifrance.gouv.fr/loi",
        promulgated_on=date(2026, 6, 24),
    )
    node = nodes_of(client.get(f"/loi/{law.slug}").text, "Legislation")[0]

    assert node["name"] == "Loi structurée"
    assert node["legislationIdentifier"] == "LOI n° 2026-532 du 24 juin 2026"
    assert node["legislationDate"] == "2026-06-24"
    assert node["sameAs"] == "https://www.legifrance.gouv.fr/loi"
    assert node["url"] == "http://testserver/loi/loi-structuree"


def test_a_law_with_nothing_to_say_says_nothing_rather_than_null(client, db_session):
    """Missing values are omitted, not null."""
    law = make_law(db_session, slug="loi-nue", title="Loi nue")
    node = nodes_of(client.get(f"/loi/{law.slug}").text, "Legislation")[0]
    assert "legislationDate" not in node
    assert "legislationIdentifier" not in node
    assert "sameAs" not in node


def test_the_results_page_declares_the_open_data_as_a_dataset(client, db_session):
    """The results page declares the open data as a Dataset."""
    law = make_law(db_session, slug="loi-donnees", title="Loi données")
    dataset = nodes_of(client.get(f"/loi/{law.slug}/resultats").text, "Dataset")[0]

    assert dataset["isAccessibleForFree"] is True
    assert dataset["about"]["@id"] == "http://testserver/loi/loi-donnees#loi"
    formats = {d["encodingFormat"]: d["contentUrl"] for d in dataset["distribution"]}
    assert formats == {
        "text/csv": "http://testserver/loi/loi-donnees/export.csv",
        "application/json": "http://testserver/loi/loi-donnees/export.json",
    }
    assert dataset["license"] == "https://www.etalab.gouv.fr/licence-ouverte-open-licence/"


def test_the_results_page_marks_up_only_what_it_shows(client, db_session):
    """The results page markup doesn't include the law summary, which it doesn't show."""
    law = make_law(
        db_session,
        slug="loi-visible",
        title="Loi visible",
        summary_text="Un résumé que cette page n'affiche pas.",
    )
    page = client.get(f"/loi/{law.slug}/resultats").text

    node = nodes_of(page, "Legislation")[0]
    assert node["name"] == "Loi visible", "the title is shown"
    assert "description" not in node
    assert "Un résumé que cette page n'affiche pas" not in page


def test_a_title_cannot_break_out_of_the_structured_data(client, db_session):
    """A title containing </script> can't break out of the JSON-LD block."""
    hostile = "Loi </script><script>alert(1)</script> suite"
    law = make_law(db_session, slug="loi-hostile", title=hostile)
    page = client.get(f"/loi/{law.slug}").text

    assert "<script>alert(1)</script>" not in page
    assert structured_data(page), "and the block is still valid JSON"
    assert nodes_of(page, "Legislation")[0]["name"] == hostile


# --- HEAD ---------------------------------------------------------------------


def test_head_is_answered_wherever_get_is(client, db_session):
    """HEAD works wherever GET does, including routes from included routers."""
    law = make_law(db_session, slug="loi-tete", title="Loi tête")
    for path in ("/", "/robots.txt", "/sitemap.xml", "/connexion", f"/loi/{law.slug}"):
        assert client.request("HEAD", path).status_code == 200, path


def test_head_does_not_open_a_route_that_writes(client, open_law):
    """HEAD doesn't work on POST routes."""
    assert client.request("HEAD", f"/loi/{open_law.slug}/vote").status_code == 405


def test_the_voting_window_is_dated_in_the_zone_the_site_publishes_in():
    """temporalCoverage uses Paris dates, not UTC."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import seo
    from app.models import Law

    paris = ZoneInfo("Europe/Paris")
    law = Law(
        slug="loi-minuit",
        title="Loi de minuit",
        opens_at=datetime(2026, 7, 1, 0, 30, tzinfo=paris),
        closes_at=datetime(2026, 8, 1, 0, 30, tzinfo=paris),
    )
    dataset = seo.votation_dataset("https://exemple.test", law)
    assert dataset["temporalCoverage"] == "2026-07-01/2026-08-01"


def test_an_open_votation_declares_an_open_ended_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import seo
    from app.models import Law

    law = Law(
        slug="loi-ouverte",
        title="Loi ouverte",
        opens_at=datetime(2026, 7, 1, 0, 30, tzinfo=ZoneInfo("Europe/Paris")),
    )
    assert seo.votation_dataset("https://exemple.test", law)["temporalCoverage"] == "2026-07-01/"
