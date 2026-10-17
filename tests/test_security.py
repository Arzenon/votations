"""Cross-site requests, response headers, and stored URLs."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.config import get_settings
from app.crud import laws as laws_crud
from app.main import app
from app.models import Law
from tests.helpers import admin_headers, basic_auth, make_law

# --- Cross-site admin requests ------------------------------------------------


def test_the_archive_route_rejects_a_cross_site_post(client, open_law):
    """Another site can't make a logged-in admin archive a law."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={**admin_headers(), "Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    # And the law is still there.
    assert client.get("/admin", headers=admin_headers()).text.count(open_law.title) >= 1


def test_a_forged_referer_is_rejected_too(client, open_law):
    """A foreign Referer is rejected when there's no Origin.

    Not using `admin_headers()`, whose Origin would take precedence.
    """
    settings = get_settings()
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={
            **basic_auth(settings.admin_username, settings.admin_password),
            "Referer": "https://evil.example/piege.html",
        },
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_a_lookalike_host_is_rejected(client, open_law):
    """`testserver.evil.example` doesn't pass as `testserver`."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={**admin_headers(), "Origin": "http://testserver.evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_the_admins_own_form_still_works(client, open_law):
    r = client.post(
        f"/admin/lois/{open_law.id}/archiver",
        headers={**admin_headers(), "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_reads_are_never_blocked(client):
    r = client.get("/admin", headers={**admin_headers(), "Origin": "https://evil.example"})
    assert r.status_code == 200


def test_the_check_runs_before_authentication_is_even_relevant(client, open_law):
    """A cross-site request is refused with or without credentials."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)


# --- Cross-site voting --------------------------------------------------------


def test_a_cross_site_post_cannot_cast_a_vote(client, open_law):
    """Another site can't cast a vote (SameSite=Lax doesn't prevent a new cookie)."""
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_the_sites_own_ballot_still_votes(client, open_law):
    r = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# --- Admin guard ----------------------------------------------------------------


def test_every_admin_route_refuses_a_request_with_no_credentials(client):
    """Every admin route returns 401 without credentials.

    Checked by calling each route from the schema, not by inspecting the
    dependency tree (nested routers made that silently find nothing).
    """
    paths = {
        path: methods
        for path, methods in client.app.openapi()["paths"].items()
        if path.startswith("/admin")
    }
    assert len(paths) > 10, "the administration really is in the schema"

    leaked = []
    for path, methods in paths.items():
        # Any id: the guard runs before the handler.
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in methods:
            answer = client.request(method.upper(), url, follow_redirects=False)
            if answer.status_code != 401:
                leaked.append(f"{method.upper()} {url} → {answer.status_code}")
    assert not leaked, f"admin routes reachable without credentials: {leaked}"


def test_a_cross_site_post_is_refused_before_the_credentials_are_read(client, db_session):
    """The origin check runs before the credentials check (403, not 401)."""
    law = make_law(db_session, slug="loi-ordre", title="Loi ordre")
    r = client.post(
        f"/admin/lois/{law.id}/archiver",
        headers={**admin_headers(), "Origin": "https://ailleurs.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_an_accented_credential_is_refused_not_a_crash(client):
    """An accented credential is refused without crashing."""
    r = client.get("/admin", headers=basic_auth("admin", "mot-de-passé"))
    assert r.status_code == 401


def test_comparing_an_accented_password_does_not_raise():
    """Comparing a non-ASCII password doesn't raise (compare_digest does on str)."""
    from app.routes.admin.guard import _matches

    assert _matches("admin", "clé-très-sûre") is False
    assert _matches("clé-très-sûre", "clé-très-sûre") is True


def test_a_post_with_no_origin_is_allowed_in_development(client, open_law):
    """In development, a POST without Origin or Referer is allowed (curl, tests)."""
    assert not get_settings().is_production, "the suite runs in development"
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 303


def test_production_refuses_a_post_that_states_no_origin(client, open_law):
    """In production, a POST without Origin or Referer is refused."""
    settings = get_settings()
    previous = settings.environment
    settings.environment = "production"
    try:
        r = client.post(
            f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False
        )
        assert r.status_code == 403
        # A request with our Origin still works.
        ok = client.post(
            f"/loi/{open_law.slug}/vote",
            data={"choice": "for"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert ok.status_code == 303
    finally:
        settings.environment = previous


def test_a_get_never_needs_an_origin_even_in_production(client, open_law):
    """GETs never need an Origin."""
    settings = get_settings()
    previous = settings.environment
    settings.environment = "production"
    try:
        assert client.get(f"/loi/{open_law.slug}").status_code == 200
    finally:
        settings.environment = previous


# --- What a database error is allowed to say ---------------------------------


def test_a_database_error_never_carries_its_bound_parameters():
    """Database errors don't include parameters (on the vote route: the voter token)."""
    from sqlalchemy.exc import DataError

    from app.db import get_engine

    assert get_engine().hide_parameters, "the engine must not render parameters"

    rendered = str(
        DataError(
            "INSERT INTO votes (law_id, voter_token) VALUES (%(law_id)s, %(voter_token)s)",
            {"law_id": 1, "voter_token": "empreinte-du-jeton"},
            Exception("value too long"),
            hide_parameters=get_engine().hide_parameters,
        )
    )
    assert "empreinte-du-jeton" not in rendered
    # The SQL statement is still there.
    assert "INSERT INTO votes" in rendered


# --- Security headers --------------------------------------------------------


def test_an_unhandled_error_still_carries_the_security_headers(monkeypatch):
    """A 500 page still has the security headers and no-store."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(laws_crud, "list_published", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/", headers={"Accept": "text/html"})

    assert r.status_code == 500
    assert r.headers["cache-control"] == "private, no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    # A normal page, not a stack trace.
    assert "panne simulée" not in r.text


def test_hsts_is_sent_only_where_the_site_is_already_https():
    """HSTS is only sent when cookie_secure is set."""
    from app.main import default_headers

    settings = get_settings()
    assert not settings.cookie_secure, "the suite runs without TLS"
    assert not any(name == "Strict-Transport-Security" for name, _ in default_headers())

    previous = settings.cookie_secure
    settings.cookie_secure = True
    try:
        sent = dict(default_headers())
        assert "max-age=31536000" in sent["Strict-Transport-Security"]
    finally:
        settings.cookie_secure = previous


def test_a_normal_response_carries_no_hsts_in_development(client):
    assert "strict-transport-security" not in client.get("/").headers


def test_the_app_sets_its_own_security_headers(client):
    """The app sets the security headers itself, not just Caddy."""
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]


# --- Cache headers set by the app ----------------------------------------------


def test_the_law_page_is_never_stored_in_a_shared_cache(client, open_law):
    """The law page shows the visitor's ballot, so it's never cached."""
    r = client.get(f"/loi/{open_law.slug}")
    assert r.headers["cache-control"] == "private, no-store"


def test_the_admin_is_never_cached(client):
    r = client.get("/admin", headers=admin_headers())
    assert "no-store" in r.headers["cache-control"]


def test_static_assets_are_fingerprinted(client):
    from app.templating import asset_url

    url = asset_url("styles.css")
    assert "?v=" in url
    assert client.get(url).status_code == 200


def test_static_assets_are_cached_hard_in_production_only(client):
    """Static files are cached as immutable in production only."""
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


# --- Reconnaissance surface ---------------------------------------------------


def test_the_api_schema_is_public_in_development(client):
    assert client.get("/openapi.json").status_code == 200


def test_production_hides_the_api_schema():
    """No API docs in production (they list the admin routes)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = get_settings()
    saved = (
        settings.environment,
        settings.secret_key,
        settings.admin_password,
        settings.cookie_secure,
    )
    # A production app also has to satisfy the boot guard.
    settings.environment = "production"
    settings.secret_key = "k" * 50
    settings.admin_password = "un-vrai-mot-de-passe"
    settings.cookie_secure = True
    try:
        prod = TestClient(create_app())
        for path in ("/openapi.json", "/docs", "/redoc"):
            assert prod.get(path).status_code == 404, path
    finally:
        (
            settings.environment,
            settings.secret_key,
            settings.admin_password,
            settings.cookie_secure,
        ) = saved


# --- Stored links -------------------------------------------------------------


def test_a_javascript_url_is_refused_not_silently_blanked(client, db_session):
    """A javascript: URL is refused with an error, not silently blanked."""

    r = client.post(
        "/admin/lois",
        headers={**admin_headers(), "Origin": "http://testserver"},
        data={
            "title": "Loi piégée",
            "published": "1",
            "legifrance_url": "javascript:alert(1)",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "http(s)" in r.text
    assert db_session.query(Law).filter_by(title="Loi piégée").first() is None


def test_a_javascript_argument_source_is_refused_too(client, db_session):

    r = client.post(
        "/admin/lois",
        headers={**admin_headers(), "Origin": "http://testserver"},
        data={
            "title": "Loi piégée 2",
            "published": "1",
            "arguments_for_text": "t",
            "arguments_for_author": "a",
            "arguments_for_source": "javascript:alert(1)",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert db_session.query(Law).filter_by(title="Loi piégée 2").first() is None


def test_a_stored_javascript_url_still_never_renders(client, db_session):
    """A javascript: URL already in the database is never rendered."""

    law = Law(
        slug="loi-heritee",
        title="Loi héritée",
        published=True,
        legifrance_url="javascript:alert(1)",
        arguments_for=[{"text": "t", "author": "a", "source": "javascript:alert(1)"}],
    )
    db_session.add(law)
    db_session.commit()
    page = client.get(f"/loi/{law.slug}")
    assert page.status_code == 200
    assert "javascript:" not in page.text


def test_ordinary_links_are_kept(client, db_session):

    client.post(
        "/admin/lois",
        headers={**admin_headers(), "Origin": "http://testserver"},
        data={
            "title": "Loi normale",
            "published": "1",
            "legifrance_url": "https://www.legifrance.gouv.fr/eli/loi/2026/1/1",
            "arguments_for_text": "t",
            "arguments_for_author": "a",
            "arguments_for_source": "https://an.fr/cr",
        },
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Loi normale").one()
    assert law.legifrance_url.startswith("https://")
    assert law.arguments_for[0]["source"] == "https://an.fr/cr"


# --- What may be cached, and by whom ------------------------------------------


def test_the_pages_that_are_the_same_for_everyone_may_be_cached(client):
    """Pages that are the same for everyone are cacheable."""
    for path in ("/", "/recherche", "/connexion", "/vie-privee", "/mentions-legales"):
        cache = client.get(path).headers["cache-control"]
        assert cache == "public, max-age=60", f"{path} carries {cache!r}"


def test_an_open_votation_is_cached_only_briefly(client, open_law):
    """Open vote results are cached briefly, so voters see their own ballot."""
    cache = client.get(f"/loi/{open_law.slug}/resultats").headers["cache-control"]
    assert cache == "public, max-age=30"


def test_a_closed_votation_is_cached_properly(client, closed_law):
    """Closed vote results are cached longer."""
    cache = client.get(f"/loi/{closed_law.slug}/resultats").headers["cache-control"]
    assert cache == "public, max-age=300"


def test_the_ballot_is_still_never_cached(client, open_law):
    """The law page is never cached."""
    assert client.get(f"/loi/{open_law.slug}").headers["cache-control"] == "private, no-store"


def test_a_missing_law_is_not_cached_as_a_result(client):
    """A 404 isn't cached like a results page."""
    r = client.get("/loi/pas-de-loi/resultats")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "private, no-store"
