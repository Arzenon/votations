"""What each page tells a machine about itself.

Metadata is the part of a page nobody looks at, which is exactly why it drifts:
a `<title>` stays the site's name for a year, a canonical points at the wrong
URL, a structured-data block goes on describing a field somebody removed. None
of it fails visibly. So it is asserted here instead.
"""

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
    """The page's JSON-LD, parsed.

    That this parses at all is half the test: the block is built as a Python dict
    and rendered through Jinja's ``tojson``, so a value that escaped its quoting
    would show up here as a decode error rather than as a silent break-out.
    """
    block = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert block, "aucun bloc de données structurées sur la page"
    return json.loads(block.group(1))


def nodes_of(html: str, kind: str) -> list[dict]:
    return [n for n in structured_data(html)["@graph"] if n["@type"] == kind]


def test_the_canonical_url_drops_the_query_string(client, db_session):
    """Each page of a modification history is the same page, not a new one.

    Left alone, `?revisions=` offered a crawler one near-identical URL per page
    of a table that sits at the bottom of the results — the rest of the page,
    which is what anybody would rank, being identical every time.
    """
    law = make_law(db_session, slug="loi-canon", title="Loi canon")
    page = client.get(f"/loi/{law.slug}/resultats?revisions=3").text
    assert canonical(page) == "http://testserver/loi/loi-canon/resultats"


def test_og_url_says_exactly_what_the_canonical_says(client):
    """Two vocabularies, one address — or a shared link counts as two links."""
    page = client.get("/vie-privee").text
    assert canonical(page) == "http://testserver/vie-privee"
    assert og_url(page) == canonical(page)


def test_every_public_page_states_where_it_lives(client, db_session):
    law = make_law(db_session, slug="loi-partout", title="Loi partout")
    for path in ("/", "/connexion", "/mentions-legales", f"/loi/{law.slug}"):
        assert canonical(client.get(path).text) == f"http://testserver{path}", path


# --- What must not be indexed -------------------------------------------------


def test_the_search_page_refuses_indexing_but_keeps_its_links(client):
    """`?q=` is an unbounded space of thin pages; the links on it are not."""
    assert robots(client.get("/recherche?q=retraites&page=2").text) == "noindex, follow"


def test_the_pages_that_are_indexed_say_nothing_about_robots(client, db_session):
    """A `noindex` that spread would be silent and total."""
    law = make_law(db_session, slug="loi-indexee", title="Loi indexée")
    for path in ("/", "/vie-privee", f"/loi/{law.slug}", f"/loi/{law.slug}/resultats"):
        assert robots(client.get(path).text) is None, path


def test_the_sitemap_does_not_offer_a_page_it_refuses_to_index(client):
    """Listing a noindex page in a sitemap is a contradiction, reported as one."""
    assert "/recherche" not in client.get("/sitemap.xml").text


# --- Structured data ----------------------------------------------------------


def test_the_home_page_declares_the_site_and_where_a_search_goes(client):
    site = nodes_of(client.get("/").text, "WebSite")[0]
    assert site["url"] == "http://testserver/"
    target = site["potentialAction"]["target"]["urlTemplate"]
    assert target == "http://testserver/recherche?q={search_term_string}"


def test_a_law_is_described_as_legislation(client, db_session):
    """The facts the Journal Officiel publishes, said in schema.org's words."""
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
    """A hand-written law has no promulgation date and may have no reference.

    `"legislationDate": null` states that the value is known to be nothing, which
    is not what an absent staged row means.
    """
    law = make_law(db_session, slug="loi-nue", title="Loi nue")
    node = nodes_of(client.get(f"/loi/{law.slug}").text, "Legislation")[0]
    assert "legislationDate" not in node
    assert "legislationIdentifier" not in node
    assert "sameAs" not in node


def test_the_results_page_declares_the_open_data_as_a_dataset(client, db_session):
    """The charter's promise — the raw figures are downloadable — said to a machine."""
    law = make_law(db_session, slug="loi-donnees", title="Loi données")
    dataset = nodes_of(client.get(f"/loi/{law.slug}/resultats").text, "Dataset")[0]

    assert dataset["isAccessibleForFree"] is True
    assert dataset["about"]["@id"] == "http://testserver/loi/loi-donnees#loi"
    formats = {d["encodingFormat"]: d["contentUrl"] for d in dataset["distribution"]}
    assert formats == {
        "text/csv": "http://testserver/loi/loi-donnees/export.csv",
        "application/json": "http://testserver/loi/loi-donnees/export.json",
    }
    # No licence is claimed anywhere: « accès libre » in the footer is not the
    # name of one, and inventing it would be a legal claim made by a template.
    assert "license" not in dataset


def test_the_results_page_marks_up_only_what_it_shows(client, db_session):
    """The rule the whole module rests on, asserted where it was first broken.

    The results page shows a votation's figures, not the law's presentation. A
    full `Legislation` node there carried « Ce que dit la loi » into the markup of
    a page that never displays it — and, because that text is also the newest
    entry in the modification history, put page one's content on page two of a
    table that is paginated precisely to avoid shipping it whole.
    """
    law = make_law(
        db_session,
        slug="loi-visible",
        title="Loi visible",
        summary_text="Un résumé que cette page n'affiche pas.",
    )
    page = client.get(f"/loi/{law.slug}/resultats").text

    node = nodes_of(page, "Legislation")[0]
    assert node["name"] == "Loi visible", "le titre, lui, est bien affiché"
    assert "description" not in node
    assert "Un résumé que cette page n'affiche pas" not in page


def test_a_title_cannot_break_out_of_the_structured_data(client, db_session):
    """`tojson` escapes `<`, so a law cannot close the script block that quotes it.

    The admin form does not filter the title — it is prose, and prose contains
    what it contains — so this is the only thing standing between a pasted title
    and a script tag on a public page.
    """
    hostile = 'Loi </script><script>alert(1)</script> suite'
    law = make_law(db_session, slug="loi-hostile", title=hostile)
    page = client.get(f"/loi/{law.slug}").text

    assert "<script>alert(1)</script>" not in page
    assert structured_data(page), "et le bloc reste du JSON lisible"
    assert nodes_of(page, "Legislation")[0]["name"] == hostile


# --- HEAD ---------------------------------------------------------------------


def test_head_is_answered_wherever_get_is(client, db_session):
    """Link checkers and uptime probes ask this way; every URL answered 405.

    `/connexion` and the sitemap matter more than they look: they arrive through
    `include_router`, which is how forty of this site's routes arrive, and an
    earlier fix that walked `app.routes` reached only the handful declared on the
    application itself — while passing on `/robots.txt`.
    """
    law = make_law(db_session, slug="loi-tete", title="Loi tête")
    for path in ("/", "/robots.txt", "/sitemap.xml", "/connexion", f"/loi/{law.slug}"):
        assert client.request("HEAD", path).status_code == 200, path


def test_head_does_not_open_a_route_that_writes(client, open_law):
    """Only reads gained a method."""
    assert client.request("HEAD", f"/loi/{open_law.slug}/vote").status_code == 405
