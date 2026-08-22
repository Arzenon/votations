"""Behaviour introduced by the August 2026 review.

Each test names the thing that was wrong, so a regression reads as a sentence
rather than as a diff.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from itsdangerous import URLSafeSerializer
from markupsafe import escape

from app import profile_cookie, ratelimit
from app.config import Settings, get_settings
from app.crud import laws as laws_crud
from app.models import DemographicTally, Law, LawRevision


def _basic(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _admin() -> dict:
    s = get_settings()
    return {**_basic(s.admin_username, s.admin_password), "Origin": "http://testserver"}


def _law(db, **kwargs) -> Law:
    defaults = {
        "slug": "loi-x",
        "title": "Loi X",
        "published": True,
        "opens_at": datetime.now(UTC) - timedelta(hours=1),
        "closes_at": datetime.now(UTC) + timedelta(hours=24),
    }
    law = Law(**{**defaults, **kwargs})
    db.add(law)
    db.commit()
    db.refresh(law)
    return law


# --- A1: the administrator throttle actually throttles -------------------------


def test_guessing_is_refused_not_merely_counted(client, monkeypatch):
    """Wrong passwords used to return 401 for ever, at network speed."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    wrong = _basic("admin", "nope")

    codes = [client.get("/admin", headers=wrong).status_code for _ in range(6)]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429, 429], "past the limit, attempts must be refused"


def test_a_throttled_response_does_not_reveal_a_correct_password(client, monkeypatch):
    """429-only-on-success turned the throttle into an oracle."""
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 2, raising=False)
    for _ in range(2):
        client.get("/admin", headers=_basic("admin", "nope"))

    refused_wrong = client.get("/admin", headers=_basic("admin", "still-wrong"))
    refused_right = client.get("/admin", headers=_admin())
    assert refused_wrong.status_code == refused_right.status_code == 429
    assert refused_wrong.text == refused_right.text


def test_a_working_administrator_is_never_slowed_down(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    assert [client.get("/admin", headers=_admin()).status_code for _ in range(10)] == [200] * 10


# --- A2: non-ASCII credentials ------------------------------------------------


def test_an_accented_credential_is_refused_not_a_crash(client):
    """Starlette rejects the header before this, but the guard must be safe alone."""
    r = client.get("/admin", headers=_basic("admin", "mot-de-passé"))
    assert r.status_code == 401


def test_comparing_an_accented_password_does_not_raise():
    """`secrets.compare_digest` on str raises TypeError for non-ASCII.

    With a non-ASCII ADMIN_PASSWORD configured, every login attempt used to be an
    unhandled 500 rather than a refusal — the administrator was locked out with no
    indication why.
    """
    from app.routes.admin.guard import _matches

    assert _matches("admin", "clé-très-sûre") is False
    assert _matches("clé-très-sûre", "clé-très-sûre") is True


def test_a_non_ascii_admin_password_is_refused_at_boot():
    """HTTP Basic cannot carry it here, so it could only ever lock the admin out."""
    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_password="clé-très-sûre")

    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_username="administratrice-éé")


# --- A3: the profile cookie is scoped to one law ------------------------------


def _cells(db, law_id) -> dict[tuple[str, str, str], int]:
    rows = db.query(DemographicTally).filter_by(law_id=law_id).all()
    return {(r.dimension, r.category, r.choice): r.count for r in rows}


def test_a_profile_given_on_one_law_never_moves_another_laws_counters(client, db_session):
    """The cookie held one profile for the whole site while counters are per-law.

    Voting 18-24 on A then 65+ on B, then changing the vote on A, used to
    decrement A's 65+ cell — other people's votes — and strand the real 18-24
    contribution under the old choice.
    """
    a = _law(db_session, slug="loi-a", title="Loi A")
    b = _law(db_session, slug="loi-b", title="Loi B")

    client.post(f"/loi/{a.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    client.post(f"/loi/{b.slug}/vote", data={"choice": "for", "age_bracket": "65+"})

    # Somebody else's vote, so a stray decrement would be visible rather than clamped.
    client_cells_before = _cells(db_session, a.id)
    assert client_cells_before == {("age", "18-24", "for"): 1}

    client.post(f"/loi/{a.slug}/vote", data={"choice": "against"})

    after = _cells(db_session, a.id)
    assert after.get(("age", "18-24", "against")) == 1, "the voter's own cell should move"
    assert after.get(("age", "18-24", "for"), 0) == 0
    assert after.get(("age", "65+", "against"), 0) == 0, "law B's category must not appear on A"
    # And law B is untouched by the change made on law A.
    assert _cells(db_session, b.id) == {("age", "65+", "for"): 1}


def test_the_cookie_remembers_several_laws_at_once(client, db_session):
    a = _law(db_session, slug="loi-c", title="Loi C")
    b = _law(db_session, slug="loi-d", title="Loi D")
    client.post(f"/loi/{a.slug}/vote", data={"choice": "for", "age_bracket": "18-24"})
    client.post(f"/loi/{b.slug}/vote", data={"choice": "for", "age_bracket": "65+"})

    from types import SimpleNamespace

    stored = client.cookies.get(profile_cookie.COOKIE_NAME)
    request = SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: stored})
    assert profile_cookie.read(request, a.id) == {"age": "18-24"}
    assert profile_cookie.read(request, b.id) == {"age": "65+"}


def test_the_cookie_cannot_grow_without_bound():
    """A browser that votes on hundreds of laws must not exceed the 4 kB limit."""
    from types import SimpleNamespace

    response = SimpleNamespace(set_cookie=lambda *a, **k: captured.update(value=a[1]))
    captured: dict = {}
    store: dict = {}
    serializer = URLSafeSerializer(get_settings().secret_key, salt="voter-profile")

    for law_id in range(400):
        store[str(law_id)] = {"age": "18-24", "sexe": "femme", "region": "Île-de-France"}
    request = SimpleNamespace(cookies={profile_cookie.COOKIE_NAME: serializer.dumps(store)})

    profile_cookie.write(request, response, 999, {"age": "25-34"})
    assert len(captured["value"].encode()) <= 4000


# --- A4 / E7: the configuration refuses values that used to fail open ---------


def test_a_typo_in_the_export_resolution_is_refused_at_boot():
    """`EXPORT_TIME_RESOLUTION=days` silently published exact vote timestamps."""
    with pytest.raises(ValueError):
        Settings(export_time_resolution="days")


def test_an_unknown_timezone_is_refused_rather_than_silently_utc():
    with pytest.raises(ValueError):
        Settings(display_timezone="Europe/Paris-ish")


def test_a_negative_anonymity_threshold_is_refused():
    with pytest.raises(ValueError):
        Settings(min_cell_count=0)


def test_production_refuses_a_defeated_anonymity_threshold():
    from app.config import InsecureConfiguration, check_production_config

    settings = Settings(
        environment="production",
        secret_key="x" * 48,
        admin_password="un-mot-de-passe-solide",
        cookie_secure=True,
        min_cell_count=1,
    )
    with pytest.raises(InsecureConfiguration, match="MIN_CELL_COUNT"):
        check_production_config(settings)


# --- A5 / A7: headers the app sets for itself --------------------------------


def test_the_law_page_is_never_stored_in_a_shared_cache(client, open_law):
    """It renders the visitor's own ballot."""
    r = client.get(f"/loi/{open_law.slug}")
    assert r.headers["cache-control"] == "private, no-store"


def test_the_admin_is_never_cached(client):
    r = client.get("/admin", headers=_admin())
    assert "no-store" in r.headers["cache-control"]


def test_the_app_sets_its_own_security_headers(client):
    """They used to exist only in the Caddyfile; another proxy meant none at all."""
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]


def test_static_assets_are_fingerprinted(client):
    from app.templating import asset_url

    url = asset_url("styles.css")
    assert "?v=" in url
    assert client.get(url).status_code == 200


def test_static_assets_are_cached_hard_in_production_only(client):
    """`immutable` is safe once the files are frozen, and a trap while editing."""
    from app.main import DEV_STATIC_CACHE_CONTROL, STATIC_CACHE_CONTROL
    from app.templating import asset_url

    settings = get_settings()
    assert not settings.is_production, "the suite runs in development"
    assert client.get(asset_url("styles.css")).headers["cache-control"] == (
        DEV_STATIC_CACHE_CONTROL
    )

    previous = settings.environment
    settings.environment = "production"
    try:
        assert "immutable" in STATIC_CACHE_CONTROL
        assert client.get(asset_url("styles.css")).headers["cache-control"] == (
            STATIC_CACHE_CONTROL
        )
    finally:
        settings.environment = previous


# --- A8: the exports stream and are throttled --------------------------------


def test_the_json_export_is_valid_and_streamed(client, db_session, open_law):
    for _ in range(5):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
        client.cookies.clear()

    r = client.get(f"/loi/{open_law.slug}/export.json")
    assert r.status_code == 200
    body = json.loads(r.text)
    assert body["law"]["slug"] == open_law.slug
    assert len(body["votes"]) == 5
    assert set(body["votes"][0]) == {"law_id", "choice", "created_at"}
    assert "votes_note" in body


def test_the_json_export_is_valid_when_a_law_has_no_votes(client, open_law):
    body = json.loads(client.get(f"/loi/{open_law.slug}/export.json").text)
    assert body["votes"] == []


def test_exports_are_rate_limited_far_below_voting(client, open_law, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_rate_limit", 2, raising=False)
    ratelimit.reset()
    codes = [client.get(f"/loi/{open_law.slug}/export.csv").status_code for _ in range(4)]
    assert codes[:2] == [200, 200]
    assert codes[2:] == [429, 429]


# --- B1 / B4 / B5: the form refuses instead of failing quietly ---------------


def test_malformed_argument_json_is_refused_not_silently_wiped(client, db_session):
    law = _law(db_session, slug="loi-args", arguments_for=[{"text": "gardé"}])
    r = client.post(
        f"/admin/lois/{law.id}",
        headers=_admin(),
        data={"title": "Loi X", "arguments_for": '[{"text": "cassé",}]'},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "JSON illisible" in r.text
    db_session.refresh(law)
    assert law.arguments_for == [{"text": "gardé"}], "the existing arguments must survive"


def test_a_closing_time_before_the_opening_is_refused(client):
    r = client.post(
        "/admin/lois",
        headers=_admin(),
        data={
            "title": "Loi impossible",
            "opens_at": "2026-09-01T10:00",
            "closes_at": "2026-08-01T10:00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "postérieure" in r.text


def test_an_over_long_title_is_a_message_not_a_500(client):
    r = client.post(
        "/admin/lois",
        headers=_admin(),
        data={"title": "x" * 501},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "limite" in r.text


def test_an_empty_title_is_refused(client):
    r = client.post("/admin/lois", headers=_admin(), data={"title": "   "}, follow_redirects=False)
    assert r.status_code == 400


# --- B2: the public record of edits made after publication -------------------


def test_editing_a_published_law_is_recorded_and_shown(client, db_session):
    law = _law(db_session, slug="loi-rev", summary_text="Version initiale.")
    client.post(
        f"/admin/lois/{law.id}",
        headers=_admin(),
        data={"title": law.title, "summary_text": "Version corrigée.", "published": "1"},
        follow_redirects=False,
    )
    recorded = db_session.query(LawRevision).filter_by(law_id=law.id).all()
    fields = {r.field for r in recorded}
    assert "summary_text" in fields
    entry = next(r for r in recorded if r.field == "summary_text")
    assert entry.old_value == "Version initiale."
    assert entry.new_value == "Version corrigée."

    page = client.get(f"/loi/{law.slug}/resultats")
    assert "Version initiale." in page.text
    assert "Ce que dit la loi" in page.text


def test_editing_an_unpublished_draft_is_not_recorded(client, db_session):
    law = _law(db_session, slug="loi-brouillon", published=False, summary_text="a")
    client.post(
        f"/admin/lois/{law.id}",
        headers=_admin(),
        data={"title": law.title, "summary_text": "b"},
        follow_redirects=False,
    )
    assert db_session.query(LawRevision).filter_by(law_id=law.id).count() == 0


def test_an_unmodified_law_says_so_plainly(client, open_law):
    page = client.get(f"/loi/{open_law.slug}/resultats")
    assert "Aucune modification depuis la publication" in page.text


def test_the_law_page_points_at_the_history_when_there_is_one(client, db_session):
    law = _law(db_session, slug="loi-modifiee", summary_text="a")
    laws_crud.update(db_session, law, summary_text="b")
    page = client.get(f"/loi/{law.slug}")
    assert "historique des modifications" in page.text.lower()


# --- B3: archiving replaces deletion -----------------------------------------


def test_archiving_hides_a_law_without_destroying_its_votes(client, db_session, open_law):
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})

    r = client.post(f"/admin/lois/{open_law.id}/archiver", headers=_admin(), follow_redirects=False)
    assert r.status_code == 303

    assert client.get(f"/loi/{open_law.slug}").status_code == 404
    assert client.get(f"/loi/{open_law.slug}/resultats").status_code == 404
    assert client.get(f"/loi/{open_law.slug}/export.csv").status_code == 404
    assert open_law.slug not in client.get("/").text

    db_session.refresh(open_law)
    assert open_law.archived_at is not None
    from app.crud import results as results_crud

    assert results_crud.count_votes(db_session, open_law.id) == 1, "votes are kept"


def test_an_archived_law_can_be_restored(client, db_session, open_law):
    client.post(f"/admin/lois/{open_law.id}/archiver", headers=_admin(), follow_redirects=False)
    client.post(f"/admin/lois/{open_law.id}/restaurer", headers=_admin(), follow_redirects=False)
    assert client.get(f"/loi/{open_law.slug}").status_code == 200


def test_deleting_asks_on_a_page_the_server_renders(client, open_law):
    """The confirmation used to be a JS attribute; with JS off it did nothing."""
    client.post(f"/admin/lois/{open_law.id}/archiver", headers=_admin(), follow_redirects=False)
    page = client.get(f"/admin/lois/{open_law.id}/supprimer", headers=_admin())
    assert page.status_code == 200
    assert "<form" in page.text
    assert "irréversible" in page.text


def test_a_law_cannot_be_purged_before_it_is_archived(client, db_session, open_law):
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer", headers=_admin(), follow_redirects=False
    )
    assert r.status_code == 409
    assert db_session.get(Law, open_law.id) is not None


def test_purging_an_archived_law_removes_it_for_good(client, db_session, open_law):
    law_id = open_law.id
    client.post(f"/admin/lois/{law_id}/archiver", headers=_admin(), follow_redirects=False)
    r = client.post(f"/admin/lois/{law_id}/supprimer", headers=_admin(), follow_redirects=False)
    assert r.status_code == 303
    assert db_session.get(Law, law_id) is None


# --- B7 / C8: cheaper public pages -------------------------------------------


def test_an_absurd_page_number_is_clamped(client, db_session):
    _law(db_session, slug="loi-p", title="Loi paginée")
    r = client.get("/recherche?page=999999999")
    assert r.status_code == 200
    assert "Page 1 / 1" in r.text or "Loi paginée" in r.text


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


# --- C1: htmx only where it is used ------------------------------------------


def test_the_home_page_does_not_load_htmx(client):
    assert "htmx" not in client.get("/").text


def test_the_law_page_does_load_htmx(client, open_law):
    assert "htmx" in client.get(f"/loi/{open_law.slug}").text


# --- D1: the draft banner stops nagging, but only after it has been seen -------


def test_a_failed_draft_is_shown_until_it_is_dismissed(client, db_session):
    """A failure must be seen once, then stop nagging — without a GET clearing it.

    Clearing it while *rendering* the form meant an ordinary GET destroyed the
    only record of why the draft had not happened, so a link prefetch or a
    browser preload could consume the explanation before anybody read it.
    """
    from app.crud import imports as imports_crud
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DUID-ECHEC",
        title="Dossier",
        law_code="2026-9",
        promulgated_on=datetime.now(UTC).date(),
        dossier_url="https://an.fr/d",
    )
    db_session.add(item)
    db_session.commit()
    imports_crud.fail_draft(db_session, item, message="le texte officiel est introuvable")

    page = f"/admin/lois/nouvelle?import_id={item.id}"
    assert "introuvable" in client.get(page, headers=_admin()).text, (
        "the administrator must see why it failed"
    )
    assert "introuvable" in client.get(page, headers=_admin()).text, (
        "and a second GET must not have eaten the message"
    )

    dismissed = client.post(
        f"/admin/imports/{item.id}/brouillon/masquer",
        headers=_admin(),
        follow_redirects=False,
    )
    assert dismissed.status_code == 303
    assert "introuvable" not in client.get(page, headers=_admin()).text, (
        "once acknowledged, it must not be told again for ever"
    )


def test_dismissing_a_draft_failure_needs_the_admin_guard(client, db_session):
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DUID-ECHEC-AUTH",
        title="Dossier",
        law_code="2026-11",
        promulgated_on=datetime.now(UTC).date(),
        dossier_url="https://an.fr/d",
    )
    db_session.add(item)
    db_session.commit()
    r = client.post(f"/admin/imports/{item.id}/brouillon/masquer", follow_redirects=False)
    assert r.status_code == 401


def test_a_pending_draft_is_never_cleared_by_opening_the_form(client, db_session):
    """Clearing it mid-run would stop the polling banner while the job continues."""
    from app.crud import imports as imports_crud
    from app.models import ImportedLaw

    item = ImportedLaw(
        source="assemblee",
        dossier_uid="DUID-ENCOURS",
        title="Dossier",
        law_code="2026-10",
        promulgated_on=datetime.now(UTC).date(),
        dossier_url="https://an.fr/d",
    )
    db_session.add(item)
    db_session.commit()
    imports_crud.mark_draft_pending(db_session, item)

    page = client.get(f"/admin/lois/nouvelle?import_id={item.id}", headers=_admin())
    assert "en cours de génération" in page.text
    db_session.refresh(item)
    assert item.draft_status == "en_cours"


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
    from app.crud import votes as votes_crud
    from app.models import VoteChoice

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


def test_missing_legal_identity_is_shown_as_missing(client, monkeypatch):
    """A plausible placeholder would be worse than an empty field on this page."""
    settings = get_settings()
    for field in ("legal_editor", "legal_editor_address", "legal_host"):
        monkeypatch.setattr(settings, field, "", raising=False)
    page = client.get("/mentions-legales").text
    assert "Cette page est incomplète" in page
    assert "non renseigné" in page


def test_a_filled_legal_identity_is_shown_without_the_warning(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "legal_editor", "Association Votations", raising=False)
    monkeypatch.setattr(settings, "legal_editor_address", "1 rue de la Paix, Paris", raising=False)
    monkeypatch.setattr(settings, "legal_publication_director", "A. Dupont", raising=False)
    monkeypatch.setattr(settings, "legal_contact_email", "contact@example.org", raising=False)
    monkeypatch.setattr(settings, "legal_host", "OVH SAS, Roubaix", raising=False)

    page = client.get("/mentions-legales").text
    assert "Cette page est incomplète" not in page
    assert "Association Votations" in page
    assert "OVH SAS, Roubaix" in page
    assert "non renseigné" not in page


def test_the_legal_pages_never_invent_an_identity(client, monkeypatch):
    """Nothing plausible may appear where a real name is missing."""
    settings = get_settings()
    for field in (
        "legal_editor",
        "legal_editor_address",
        "legal_publication_director",
        "legal_contact_email",
        "legal_host",
    ):
        monkeypatch.setattr(settings, field, "", raising=False)
    page = client.get("/mentions-legales").text
    for invented in ("example.org", "SAS", "@", "rue "):
        assert invented not in page.split("<footer")[0]


# --- Courbe d'évolution -------------------------------------------------------


def _bucket(label, **counts):
    from app.crud.results import EvolutionBucket

    full = {"for": 0, "against": 0, "abstain": 0, **counts}
    return EvolutionBucket(label=label, counts=full)


def test_the_chart_stays_inside_its_plot_area():
    """A smoothed curve must never draw a day below zero votes."""
    import re

    from app.charts import build_evolution_chart

    # A spike between two quiet days is what makes a naive spline overshoot.
    chart = build_evolution_chart(
        [
            _bucket("2026-05-01", **{"for": 1}),
            _bucket("2026-05-02", **{"for": 40}),
            _bucket("2026-05-03", **{"for": 1}),
            _bucket("2026-05-04", **{"for": 2}),
        ]
    )
    assert chart is not None
    for serie in chart.series:
        ys = [float(v) for v in re.findall(r"[-\d.]+ ([-\d.]+)", serie.line)]
        assert min(ys) >= chart.plot_top - 0.01, "la courbe sort par le haut"
        assert max(ys) <= chart.plot_bottom + 0.01, "la courbe passe sous zéro"


def test_the_chart_axis_stops_on_a_round_number():
    from app.charts import build_evolution_chart

    chart = build_evolution_chart([_bucket("2026-05-01", **{"for": 37})])
    assert chart.max_value == 40
    assert [v for _y, v in chart.y_ticks] == [0, 10, 20, 30, 40]


def test_the_chart_thins_its_date_labels():
    """Whatever the span, the dates must stay far enough apart to be read."""
    from app.charts import build_evolution_chart

    for days in (2, 7, 20, 30, 45, 120):
        chart = build_evolution_chart([_bucket(f"2026-05-{(i % 28) + 1:02}") for i in range(days)])
        assert len(chart.x_ticks) <= 8, f"{days} jours : trop d'étiquettes"
        gaps = [
            chart.x_ticks[i + 1][0] - chart.x_ticks[i][0] for i in range(len(chart.x_ticks) - 1)
        ]
        assert not gaps or min(gaps) > 40, f"{days} jours : étiquettes trop serrées"
    assert chart.x_ticks[0][1] == "01/05"


def test_the_chart_describes_itself_for_a_screen_reader():
    from app.charts import build_evolution_chart

    chart = build_evolution_chart(
        [_bucket("2026-05-01", **{"for": 3}), _bucket("2026-05-02", **{"for": 5})]
    )
    assert "01/05" in chart.description
    assert "Pour : 8 votes" in chart.description


def test_the_results_page_draws_the_curve(client, open_law, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "min_bucket_volume", 2, raising=False)
    for _ in range(4):
        client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"})
        client.cookies.clear()

    page = client.get(f"/loi/{open_law.slug}/resultats").text
    assert "<svg" in page
    assert 'role="img"' in page
    assert "Évolution des votes, jour par jour" in page
    # The figures stay available underneath, not replaced by the picture.
    assert "Voir les chiffres jour par jour" in page


# --- Aperçu d'une loi non publiée --------------------------------------------


def test_an_unpublished_law_can_be_previewed_by_the_admin(client, db_session):
    law = _law(db_session, slug="loi-brouillon-apercu", title="Brouillon", published=False)
    assert client.get(f"/loi/{law.slug}").status_code == 404

    page = client.get(f"/admin/lois/{law.id}/apercu", headers=_admin())
    assert page.status_code == 200
    assert "Brouillon" in page.text
    assert "Aperçu" in page.text
    assert "n'est pas publiée" in page.text


def test_the_preview_shows_the_ballot_but_cannot_vote(client, db_session):
    law = _law(db_session, slug="loi-apercu-vote", published=False)
    page = client.get(f"/admin/lois/{law.id}/apercu", headers=_admin()).text
    assert "Votre vote" in page, "le bulletin doit être visible tel qu'il sera"
    assert "disabled" in page

    # And the real vote route still refuses, preview or not.
    assert client.post(f"/loi/{law.slug}/vote", data={"choice": "for"}).status_code == 404


def test_the_preview_needs_the_admin_password(client, db_session):
    law = _law(db_session, slug="loi-apercu-prive", published=False)
    assert client.get(f"/admin/lois/{law.id}/apercu").status_code == 401


# --- Recherche plein texte ----------------------------------------------------


def test_search_finds_a_plural_from_the_singular(client, db_session):
    _law(db_session, slug="loi-retraites", title="Réforme des retraites", summary_text="Texte.")
    assert laws_crud.search(db_session, query="retraite")[1] == 1
    assert laws_crud.search(db_session, query="retraites")[1] == 1


def test_search_matches_a_word_still_being_typed(client, db_session):
    _law(db_session, slug="loi-logement", title="Encadrement des loyers", summary_text="Texte.")
    assert laws_crud.search(db_session, query="loye")[1] == 1
    assert laws_crud.search(db_session, query="encadr")[1] == 1


def test_search_still_finds_a_law_by_its_official_number(client, db_session):
    _law(db_session, slug="loi-num", title="Un texte", official_ref="LOI n° 2026-214")
    assert laws_crud.search(db_session, query="2026-214")[1] == 1


def test_search_requires_every_word(client, db_session):
    _law(db_session, slug="loi-a1", title="Réforme des retraites", summary_text="Texte.")
    _law(db_session, slug="loi-b1", title="Réforme du logement", summary_text="Texte.")
    assert laws_crud.search(db_session, query="réforme")[1] == 2
    assert laws_crud.search(db_session, query="réforme retraite")[1] == 1


def test_search_ignores_punctuation_a_visitor_types(client, db_session):
    """`to_tsquery` would treat these as syntax and fail the whole query."""
    _law(db_session, slug="loi-ponct", title="Loi sur l'eau", summary_text="Texte.")
    for query in ("l'eau", "eau !", "(eau)", "eau & | :"):
        assert laws_crud.search(db_session, query=query)[1] == 1, query


# --- Sitemap ------------------------------------------------------------------


def test_the_sitemap_lists_published_laws_only(client, db_session, open_law):
    hidden = _law(db_session, slug="loi-cachee", title="Cachée", published=False)
    body = client.get("/sitemap.xml").text
    assert body.startswith("<?xml")
    assert f"/loi/{open_law.slug}</loc>" in body
    assert f"/loi/{open_law.slug}/resultats</loc>" in body
    assert hidden.slug not in body
    assert "/vie-privee</loc>" in body


def test_an_archived_law_leaves_the_sitemap(client, db_session, open_law):
    client.post(f"/admin/lois/{open_law.id}/archiver", headers=_admin(), follow_redirects=False)
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
    law = _law(
        db_session,
        slug="loi-liens",
        official_ref="LOI n° 2026-1",
        legifrance_url="https://example.org/loi",
        arguments_for=[{"text": "t", "author": "A", "source": "https://example.org/s"}],
    )
    page = client.get(f"/loi/{law.slug}").text
    assert "(nouvel onglet)" in page
