"""Page rendering, admin guard, and the connexion placeholder."""

import base64

from app.config import get_settings
from tests.helpers import admin_headers, make_law


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _admin_auth() -> dict:
    s = get_settings()
    return _basic(s.admin_username, s.admin_password)


def test_home_lists_published_law(client, open_law):
    r = client.get("/")
    assert r.status_code == 200
    assert open_law.title in r.text
    assert "sondage représentatif" in r.text  # permanent disclaimer


def test_law_page_shows_four_blocks(client, open_law):
    r = client.get(f"/loi/{open_law.slug}")
    assert r.status_code == 200
    for heading in ["Ce que dit la loi", "Ce que ça change", "partisans", "opposants"]:
        assert heading in r.text


def test_unpublished_law_is_404(client, db_session):
    from app.models import Law

    law = Law(slug="brouillon", title="Brouillon", published=False)
    db_session.add(law)
    db_session.commit()
    assert client.get(f"/loi/{law.slug}").status_code == 404


def test_admin_requires_auth(client):
    assert client.get("/admin").status_code == 401


def test_admin_challenge_carries_a_realm(client):
    """RFC 7617 requires `realm`; without it Firefox never shows a login prompt."""
    # No credentials at all — the challenge comes from FastAPI's HTTPBasic.
    r = client.get("/admin")
    assert r.headers["www-authenticate"] == 'Basic realm="Votations"'
    # Wrong credentials — the challenge comes from require_admin.
    r = client.get("/admin", headers=_basic("admin", "definitely-wrong"))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="Votations"'


def test_admin_accepts_configured_credentials(client):
    r = client.get("/admin", headers=_admin_auth())
    assert r.status_code == 200
    assert "Administration" in r.text


def test_admin_can_create_law(client, db_session):
    from app.models import Law

    r = client.post(
        "/admin/lois",
        data={"title": "Nouvelle loi créée", "published": "1"},
        headers=_admin_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db_session.query(Law).filter_by(title="Nouvelle loi créée").count() == 1


def test_connexion_shows_disabled_button(client):
    r = client.get("/connexion")
    assert r.status_code == 200
    assert "FranceConnect" in r.text
    assert "bientôt disponible" in r.text


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


def test_an_unreadable_form_field_is_a_page_not_json(client):
    """FastAPI's own handler answers a browser form post with pydantic JSON."""
    r = client.post(
        "/admin/lois",
        headers={"Accept": "text/html", **admin_headers()},
        data={"title": "Loi", "import_id": "pas-un-nombre"},
    )
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert "Saisie invalide" in r.text
    # The pydantic detail names internal field paths; it must not be echoed.
    assert "import_id" not in r.text


# --- Health probe -------------------------------------------------------------


def test_health_probe_reports_the_database(client):
    r = client.get("/sante")
    assert r.status_code == 200
    assert r.text == "ok"


def test_robots_and_favicon_exist(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /admin" in robots.text
    assert client.get("/favicon.ico").status_code == 200


# --- C1: htmx only where it is used ------------------------------------------


def test_the_home_page_does_not_load_htmx(client):
    assert "htmx" not in client.get("/").text


def test_the_law_page_does_load_htmx(client, open_law):
    assert "htmx" in client.get(f"/loi/{open_law.slug}").text


# --- Pages légales ------------------------------------------------------------


def test_the_privacy_page_lists_both_cookies_by_name(client):
    page = client.get("/vie-privee").text
    assert "vf_voter" in page
    assert "vf_profil" in page
    assert "article 82" in page  # pourquoi il n'y a pas de bandeau


def test_the_legal_pages_are_linked_from_every_page(client, open_law):
    for url in ("/", f"/loi/{open_law.slug}", "/connexion"):
        page = client.get(url).text
        assert 'href="/vie-privee"' in page
        assert 'href="/mentions-legales"' in page


def test_a_missing_host_is_shown_as_missing(client, monkeypatch):
    """The host is the one item article 6-III still makes mandatory here.

    The publisher's own name and address are not published — article 6-III-2
    allows that for a non-professional publisher whose host holds them — so the
    only thing left that can be *missing* is the host.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "legal_host", "", raising=False)
    page = client.get("/mentions-legales").text
    assert "Cette page est incomplète" in page
    assert "Hébergeur non renseigné" in page


def test_a_named_host_removes_the_warning(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "legal_host", "OVH SAS, Roubaix", raising=False)
    page = client.get("/mentions-legales").text
    assert "Cette page est incomplète" not in page
    assert "OVH SAS, Roubaix" in page
    assert "non renseigné" not in page


def test_the_page_explains_why_the_publisher_is_not_named(client, monkeypatch):
    """An unexplained blank reads as evasion on a transparency-first site.

    The publisher's identity is withheld under article 6-III-2, which is a
    permitted choice and not an omission — so the page says so, and says that the
    host can identify the publisher if a court asks.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "legal_host", "OVH SAS, Roubaix", raising=False)
    page = client.get("/mentions-legales").text
    assert "non professionnel" in page
    assert "6-III-2" in page
    assert "autorité judiciaire" in page


def test_the_legal_page_never_invents_an_identity(client, monkeypatch):
    """Nothing plausible may appear where a real name is missing."""
    settings = get_settings()
    for field in ("legal_contact_email", "legal_host"):
        monkeypatch.setattr(settings, field, "", raising=False)
    page = client.get("/mentions-legales").text
    for invented in ("example.org", "SAS", "@"):
        assert invented not in page.split("<footer")[0]


def test_the_open_data_attribution_survives(client):
    """Licence Ouverte requires the source to be credited.

    This is not LCEN and does not go away with the identity fields: it is the
    licence under which the Assemblée nationale, DILA and INSEE data may be used
    at all, and this page is where it is credited.
    """
    page = client.get("/mentions-legales").text
    for source in ("Assemblée nationale", "information légale et administrative", "INSEE"):
        assert source in page
    assert "Licence Ouverte" in page


def test_the_sitemap_lists_published_laws_only(client, db_session, open_law):
    hidden = make_law(db_session, slug="loi-cachee", title="Cachée", published=False)
    body = client.get("/sitemap.xml").text
    assert body.startswith("<?xml")
    assert f"/loi/{open_law.slug}</loc>" in body
    assert f"/loi/{open_law.slug}/resultats</loc>" in body
    assert hidden.slug not in body
    assert "/vie-privee</loc>" in body


def test_an_archived_law_leaves_the_sitemap(client, db_session, open_law):
    client.post(
        f"/admin/lois/{open_law.id}/archiver", headers=admin_headers(), follow_redirects=False
    )
    assert open_law.slug not in client.get("/sitemap.xml").text


def test_robots_points_at_the_sitemap(client):
    assert "Sitemap: http://testserver/sitemap.xml" in client.get("/robots.txt").text


# --- Carte de partage ---------------------------------------------------------


def test_the_share_card_is_declared_with_an_absolute_url(client):
    """Open Graph ignores a relative image URL."""
    page = client.get("/").text
    assert 'property="og:image" content="http://testserver/static/og.png?v=' in page
    assert 'name="twitter:card" content="summary_large_image"' in page
    assert 'property="og:image:alt"' in page


def test_the_share_card_exists_and_is_the_right_size(client):
    from app.templating import STATIC_DIR

    png = STATIC_DIR / "og.png"
    assert png.exists()
    header = png.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    assert (width, height) == (1200, 630)


# --- « Ce que ça change en pratique » : replié plutôt que raccourci ------------


def _practical(n: int) -> str:
    """A block addressing `n` publics, in the order the drafting prompt asks for."""
    return "\n\n".join(
        f"Si vous êtes public numéro {i}, voici ce qui change." for i in range(1, n + 1)
    )


def test_a_short_practical_block_is_shown_whole(client, db_session):
    """Folding two paragraphs behind a click asks the reader to work for nothing."""
    law = make_law(db_session, slug="loi-courte", practical_changes=_practical(6))
    page = client.get(f"/loi/{law.slug}").text
    assert "public numéro 6" in page
    assert "autres publics concernés" not in page
    assert '<details class="more-audiences">' not in page


def test_a_long_practical_block_keeps_the_broadest_publics_in_view(client, db_session):
    """The wall this replaces: twenty publics, all of them above the fold.

    A law amending sixty articles across one sector genuinely concerns that many
    people, and the charter forbids choosing which of them goes unmentioned — so
    the block cannot be made shorter by dropping anyone. It can stop being a wall.
    The prompt orders the publics most-concerned-first, which is what makes taking
    the first few from the top a principled cut rather than an arbitrary one.
    """
    law = make_law(db_session, slug="loi-longue", practical_changes=_practical(20))
    page = client.get(f"/loi/{law.slug}").text

    for i in (1, 2, 3, 4, 5):
        assert f"public numéro {i}," in page, "the broadest publics stay in view"
    assert "15 autres publics concernés" in page, "and the reader is told how many follow"


def test_nothing_is_dropped_by_the_fold(client, db_session):
    """The whole basis on which folding is allowed at all.

    Shortening the block would mean deciding whose situation goes unmentioned,
    which is the one editorial act this platform exists not to make. Folding is
    only defensible because every paragraph is still on the page.
    """
    law = make_law(db_session, slug="loi-repliee", practical_changes=_practical(20))
    page = client.get(f"/loi/{law.slug}").text
    for i in range(1, 21):
        assert f"public numéro {i}," in page, f"public {i} disparu de la page"


def test_the_fold_needs_no_javascript(client, db_session):
    """`<details>` and not a script: the public side works with JS switched off."""
    law = make_law(db_session, slug="loi-sans-js", practical_changes=_practical(20))
    page = client.get(f"/loi/{law.slug}").text
    start = page.index('<details class="more-audiences">')
    fold = page[start : page.index("</details>", start)]
    assert "<summary>" in fold, "a real disclosure control, not a styled div"
    assert "onclick" not in fold and "hx-" not in fold
