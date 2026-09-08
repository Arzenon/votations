"""Page rendering, admin guard, and the connexion placeholder."""

import base64
import re

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


def test_connexion_leads_with_no_account_needed(client):
    """The page must not read as a gate — voting needs nothing.

    It used to end on a disabled « Se connecter - bientôt disponible » button,
    which answered « do I need an account? » with the opposite of the truth. The
    page still names FranceConnect, but as something wanted and unavailable.
    """
    r = client.get("/connexion")
    assert r.status_code == 200
    assert "Vous n'avez pas besoin de compte pour voter" in r.text
    assert "FranceConnect" in r.text
    assert "bientôt disponible" not in r.text
    assert "disabled" not in r.text


def test_no_login_link_in_the_header(client, open_law):
    """« Se connecter » in a nav advertises a gate this site does not have."""
    for path in ("/", "/connexion", f"/loi/{open_law.slug}"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Se connecter" not in r.text, path
        # The footer still reaches the same page, under its honest name.
        assert "À propos de l'authentification" in r.text, path


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


def _loads_htmx(page: str) -> bool:
    """Whether the page pulls in the 50 kB library, not whether it mentions it.

    Every page carries the `htmx-config` meta — it is in the shared base, so that
    the two pages which *do* load htmx cannot be given it without its settings.
    Matching on the bare word therefore stopped telling these two tests apart.
    """
    return "<script" in page and "htmx.min.js" in page


def test_the_home_page_does_not_load_htmx(client):
    assert not _loads_htmx(client.get("/").text)


def test_the_law_page_does_load_htmx(client, open_law):
    assert _loads_htmx(client.get(f"/loi/{open_law.slug}").text)


def test_htmx_is_configured_not_to_evaluate_anything(client, open_law):
    """Both switches off, on the page that loads the library.

    The Content-Security-Policy already refuses what they permit, which is why
    this is worth a test rather than a comment: the day somebody has to loosen
    the CSP, nothing else would notice htmx quietly regaining the ability to
    evaluate attribute expressions and run <script> tags out of a swapped
    fragment.
    """
    page = client.get(f"/loi/{open_law.slug}").text
    assert '<meta name="htmx-config"' in page
    assert '"allowEval":false' in page
    assert '"allowScriptTags":false' in page


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


# --- La feuille de style ------------------------------------------------------


def _stylesheet_selectors() -> list[str]:
    """The top-level selectors of styles.css, read the way a browser reads them.

    A browser does not parse CSS declaration by declaration: it takes everything
    up to the next ``{`` as a selector, whatever that text happens to be. So a
    rule that loses its selector — the residue of a deleted component — does not
    fail loudly. Its orphaned declarations are silently glued onto the *next*
    rule's selector, and that next rule is the one that disappears.

    This returns what the browser would end up with, so the two assertions below
    can be about that rather than about the source text.
    """
    from app.templating import STATIC_DIR

    css = re.sub(r"/\*.*?\*/", "", (STATIC_DIR / "styles.css").read_text(), flags=re.S)
    selectors: list[str] = []
    depth = 0
    prelude = ""
    for char in css:
        if char == "{":
            depth += 1
            if depth == 1:
                selectors.append(prelude.strip())
                prelude = ""
        elif char == "}":
            depth -= 1
            assert depth >= 0, "styles.css ferme une accolade jamais ouverte"
            prelude = ""
        elif depth == 0:
            prelude += char
    assert depth == 0, "styles.css laisse une accolade ouverte"
    return selectors


def test_no_rule_in_the_stylesheet_has_lost_its_selector():
    """Deleting a component must take its whole rule, braces included.

    Removing « les réserves » once left two declaration bodies and their closing
    braces behind with no selector in front of them. Nothing failed: ruff does
    not read CSS, no Python test touches it, and the page still rendered. What it
    cost was the rule that happened to come next — the site-wide
    ``:focus-visible`` ring, swallowed into an unusable selector and dropped, so
    the whole site quietly lost its keyboard focus outline.

    A stray declaration always shows up here as a selector carrying a ``;`` or a
    ``}``, because that is exactly what the browser scooped up on its way to the
    next ``{``.
    """
    for selector in _stylesheet_selectors():
        assert ";" not in selector and "}" not in selector, (
            f"« {selector[:80]}… » n'est pas un sélecteur : une déclaration a "
            "perdu le sien, et la règle suivante est perdue avec elle"
        )


def test_the_keyboard_focus_ring_survives_parsing():
    """The rule the orphaned declarations above happened to swallow.

    Named on its own because the check above proves the file parses, not that
    this particular rule is still in it — and this is the one whose loss nothing
    else would report: a missing focus ring looks like the browser's default.
    """
    assert ":focus-visible" in _stylesheet_selectors()


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


def _labelled(n: int) -> str:
    """A block of `n` publics written in the shape the drafting prompt asks for."""
    return "\n\n".join(
        f"Pour le public {i} : voici ce qui change pour vous." for i in range(1, n + 1)
    )


def test_labelled_publics_become_rows_and_none_is_hidden(client, db_session):
    """The fold by count exists because a wall of prose cannot be scanned.

    Once each paragraph carries the public it addresses, the wall is gone: every
    public is named on the page, in one row, and the reader opens the one that is
    theirs. Nothing sits behind « N autres publics concernés » any more — which is
    the charter's own preference, since that counter was the last place where the
    narrower publics were less visible than the broad ones.
    """
    law = make_law(db_session, slug="loi-etiquetee", practical_changes=_labelled(12))
    page = client.get(f"/loi/{law.slug}").text

    for i in range(1, 13):
        assert f"<summary>Pour le public {i}</summary>" in page, f"public {i} sans ligne"
    assert "autres publics concernés" not in page, "plus rien derrière un compteur"
    assert page.count('<details class="public">') == 12
    assert "12 publics concernés" in page, "le compte est exact, donc il est annoncé"


def test_a_paragraph_without_a_label_stays_visible_among_the_rows(client, db_session):
    """No label is ever invented, and the failure is left where a reviewer sees it.

    A wrong label is worse than none: it would tell a reader « this one is not
    about you » on the platform's authority. So the paragraph renders plainly, in
    place, and looks different from its neighbours — which is how the person who
    validates the law before publication finds out the drafting slipped.
    """
    law = make_law(
        db_session,
        slug="loi-mixte",
        practical_changes=(
            "Pour les employeurs : vous devez maintenir le contrat.\n\n"
            "Si vous êtes locataire, propriétaire ou occupant, la règle change.\n\n"
            "Pour les communes : la dotation est révisée."
        ),
    )
    page = client.get(f"/loi/{law.slug}").text
    assert page.count('<details class="public">') == 2
    assert "Si vous êtes locataire, propriétaire ou occupant" in page, "rien n'est perdu"
    assert "<summary>Si vous êtes locataire" not in page, "pas d'étiquette inventée"
    # Two rows above a block that addresses three publics: the count would be wrong,
    # so it is not published at all rather than published short.
    assert "publics concernés" not in page


def test_the_rows_need_no_javascript(client, db_session):
    """`<details>`, like every other fold on this page."""
    law = make_law(db_session, slug="loi-lignes-sans-js", practical_changes=_labelled(6))
    page = client.get(f"/loi/{law.slug}").text
    start = page.index('<details class="public">')
    row = page[start : page.index("</details>", start)]
    assert "<summary>" in row
    assert "onclick" not in row and "hx-" not in row


def test_the_ballot_can_be_reached_from_the_top_of_the_page(client, db_session):
    """A reader arriving from a shared link already knowing what they think.

    The presentation stays the page — the link skips it, it does not shorten it.
    """
    law = make_law(db_session, slug="loi-ancre", practical_changes=_labelled(4))
    page = client.get(f"/loi/{law.slug}").text
    assert 'href="#voter"' in page
    assert 'id="voter"' in page
    assert page.index('href="#voter"') < page.index('id="voter"'), "l'ancre précède la cible"


def test_what_the_law_says_folds_only_when_the_encart_carries_the_opening(client, db_session):
    """Folding it on a one-paragraph summary would hide the only summary there is."""
    long_law = make_law(
        db_session, slug="loi-deux-paras", summary_text="Premier.\n\nSecond paragraphe."
    )
    folded = client.get(f"/loi/{long_law.slug}").text
    assert '<details class="bloc-repliable">' in folded

    short_law = make_law(db_session, slug="loi-un-para", summary_text="Tout tient ici.")
    plain = client.get(f"/loi/{short_law.slug}").text
    assert '<details class="bloc-repliable">' not in plain
    assert "Tout tient ici." in plain


# --- L'ordre de la page d'accueil ---------------------------------------------


def _dated(db_session, slug, day, **kw):
    from datetime import date

    return make_law(
        db_session,
        slug=slug,
        title=f"Loi {slug}",
        promulgated_on=date.fromisoformat(day) if day else None,
        **kw,
    )


def test_the_home_page_is_ordered_by_promulgation_not_by_data_entry(client, db_session):
    """It used to be ordered by `created_at` — when the administrator pressed save.

    Three laws entered in one evening therefore appeared in the order they were
    typed, which is a fact about somebody's working schedule and not about the
    legislation. The promulgation date was on the page all along, but only inside
    `official_ref` as prose, where it could be printed and never sorted on.
    """
    _dated(db_session, "ancienne", "2026-01-15")
    _dated(db_session, "recente", "2026-08-20")
    _dated(db_session, "moyenne", "2026-04-02")

    page = client.get("/").text
    order = [page.index(f"/loi/{s}") for s in ("recente", "moyenne", "ancienne")]
    assert order == sorted(order), "la plus récemment promulguée doit venir en premier"


def test_a_law_with_no_promulgation_date_does_not_open_the_site(client, db_session):
    """Postgres sorts nulls *first* descending, so this needs saying explicitly.

    A law written by hand has no staged row and therefore no date. Without NULLS
    LAST it would outrank every dated law and head the home page.
    """
    _dated(db_session, "datee", "2026-03-01")
    _dated(db_session, "sans-date", None)

    page = client.get("/").text
    assert page.index("/loi/datee") < page.index("/loi/sans-date")


def test_the_cards_carry_the_date_the_page_is_ordered_by(client, db_session):
    """An order the reader cannot see reads as no order at all."""
    _dated(db_session, "loi-datee", "2026-06-24")
    page = client.get("/").text
    assert "Promulguée le 24 juin 2026" in page


def test_the_cards_show_participation(client, db_session, open_law):
    """Shown, and deliberately not sorted on — see the ordering test above."""
    assert "Aucun vote" in client.get("/").text
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
    page = client.get("/").text
    assert "1 vote" in page and "1 votes" not in page


# --- Les bandes ----------------------------------------------------------------


def test_one_band_renders_as_a_plain_list(client, db_session):
    """What launch looks like: a year-long window puts every votation in one band.

    The grouping must not announce itself before it has anything to group — a
    heading over the only section on the page is a line that says nothing.
    """
    _dated(db_session, "ouverte-a", "2026-08-01")
    _dated(db_session, "ouverte-b", "2026-07-01")
    page = client.get("/").text
    assert "Dernières votations" in page
    assert "Votations ouvertes" not in page
    assert "Votations closes" not in page


def test_an_open_votation_is_never_crowded_out_by_closed_ones(client, db_session):
    """The band limit is per band, and this is why it has to be.

    The page used to take the fifty most recently promulgated laws and group them
    afterwards. That is one query instead of three, and it means that once fifty
    laws are published, an open votation promulgated before the newest fifty
    simply is not on the home page — not even under « Votations ouvertes », the
    one band a visitor has something to do with. Fifty closed ones would be.

    Asserted with a limit of two rather than fifty: the bug is about the limit
    being reached, and reaching the real one takes fifty laws to say what three
    say just as well.
    """
    from datetime import UTC, datetime, timedelta

    from app.crud import laws as laws_crud

    now = datetime.now(UTC)
    closed = {"opens_at": now - timedelta(days=30), "closes_at": now - timedelta(days=1)}
    # Two recent closed votations, and one open one promulgated long before them.
    _dated(db_session, "close-recent-a", "2026-08-20", **closed)
    _dated(db_session, "close-recent-b", "2026-08-19", **closed)
    _dated(
        db_session,
        "ouverte-ancienne",
        "2026-01-05",
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(days=30),
    )

    bands = dict(laws_crud.published_by_state(db_session, limit=2))
    assert [law.slug for law in bands["open"]] == ["ouverte-ancienne"], (
        "the open band is filled from the open laws, not from whatever is left of a page-wide limit"
    )
    assert len(bands["closed"]) == 2


def test_a_law_appears_in_exactly_one_band(client, db_session):
    """Three queries, one instant — or a votation can be listed twice.

    The bands are derived from the voting window, so they partition the laws only
    if all three are decided against the same "now". Three queries each reading
    the clock would put a votation closing between the first and the third into
    both « ouvertes » and « closes ». This pins the case that is easiest to get
    wrong: a window that ends at this very moment.
    """
    from datetime import UTC, datetime, timedelta

    from app.crud import laws as laws_crud

    now = datetime.now(UTC)
    _dated(
        db_session,
        "sur-le-fil",
        "2026-08-01",
        opens_at=now - timedelta(days=1),
        closes_at=now,
    )
    bands = laws_crud.published_by_state(db_session)
    listed = [law.slug for _, laws in bands for law in laws]
    assert listed.count("sur-le-fil") == 1, f"listée dans plusieurs bandes : {bands}"


def test_several_bands_are_titled_and_ordered_by_what_you_can_do(client, db_session):
    """Vote first, then what is coming, then what is decided."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    _dated(
        db_session,
        "close",
        "2026-08-01",
        opens_at=now - timedelta(days=30),
        closes_at=now - timedelta(days=1),
    )
    _dated(
        db_session,
        "ouverte",
        "2026-07-01",
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(days=30),
    )
    _dated(
        db_session,
        "bientot",
        "2026-06-01",
        opens_at=now + timedelta(days=5),
        closes_at=now + timedelta(days=40),
    )

    page = client.get("/").text
    for heading in ("Votations ouvertes", "Bientôt ouvertes", "Votations closes"):
        assert heading in page
    assert (
        page.index("Votations ouvertes")
        < page.index("Bientôt ouvertes")
        < page.index("Votations closes")
    )
    # And a law sits under its own band, not merely somewhere on the page.
    assert page.index("Votations ouvertes") < page.index("/loi/ouverte") < page.index("Bientôt")


# --- Découpe du texte d'une loi -------------------------------------------------


def test_a_paragraph_that_is_not_addressed_to_anyone_is_left_alone():
    """The filter has to be safe on prose that was not written in that shape."""
    from app.templating import audience_split

    assert audience_split("Pour les employeurs : vous devez déclarer.") == (
        "Pour les employeurs",
        "vous devez déclarer.",
    )
    # A sentence that happens to reach a colon is not a label.
    assert audience_split("La règle est simple, et elle tient en peu de mots : agir.") == (
        "",
        "La règle est simple, et elle tient en peu de mots : agir.",
    )
    # Nor is something far too long to be a public.
    trop_long = (
        "Les personnes qui exploitent une installation classée pour la "
        "protection de l'environnement : elles déclarent."
    )
    assert audience_split(trop_long)[0] == ""
    # Nor a paragraph with no colon at all.
    assert audience_split("Rien de particulier ici.") == ("", "Rien de particulier ici.")


def test_the_opening_paragraph_is_separated_from_the_rest():
    from app.templating import summary_split

    assert summary_split("Un.\n\nDeux.\n\nTrois.") == ("Un.", ["Deux.", "Trois."])
    assert summary_split("Seul paragraphe.") == ("", ["Seul paragraphe."])
    assert summary_split("") == ("", [])
