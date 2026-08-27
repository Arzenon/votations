"""Cross-site requests, response headers, and stored URLs that must stay inert.

Three defences that have nothing in common except that each one fails silently:
a missing origin check, a header the reverse proxy stopped adding, and a
`javascript:` link sitting in a column nobody re-reads.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.crud import laws as laws_crud
from app.main import app
from app.models import Law
from tests.helpers import admin_headers, basic_auth

# --- Cross-site admin requests ------------------------------------------------


def test_the_archive_route_rejects_a_cross_site_post(client, open_law):
    """The attack: a page the admin visits while authenticated deletes a law."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={**admin_headers(), "Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    # And the law is still there.
    assert client.get("/admin", headers=admin_headers()).text.count(open_law.title) >= 1


def test_a_forged_referer_is_rejected_too(client, open_law):
    """Referer is the fallback, so it only decides when there is no Origin.

    Deliberately not built on `admin_headers()`: that carries a valid `Origin`,
    which wins over Referer and would make this pass for the wrong reason.
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
    """`testserver.evil.example` must not pass a prefix check on `testserver`."""
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
    """A cross-site request is refused whether or not it carries credentials."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)


# --- Cross-site voting --------------------------------------------------------


def test_a_cross_site_post_cannot_cast_a_vote(client, open_law):
    """SameSite=Lax withholds an existing cookie, but not a freshly minted one."""
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


# --- A2: non-ASCII credentials ------------------------------------------------


def test_an_accented_credential_is_refused_not_a_crash(client):
    """Starlette rejects the header before this, but the guard must be safe alone."""
    r = client.get("/admin", headers=basic_auth("admin", "mot-de-passé"))
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


# --- Security headers --------------------------------------------------------


def test_an_unhandled_error_still_carries_the_security_headers(monkeypatch):
    """500s are served above our middleware and used to arrive bare.

    Caddy puts the CSP back; nothing puts `no-store` back. That left the one page
    most likely to be generated under stress as the only cacheable one.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(laws_crud, "list_published", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/", headers={"Accept": "text/html"})

    assert r.status_code == 500
    assert r.headers["cache-control"] == "private, no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    # And it must still be a page of this site, not a stack trace.
    assert "panne simulée" not in r.text


def test_hsts_is_sent_only_where_the_site_is_already_https():
    """Pinning a developer's browser to https for a year is not a kindness."""
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
    """They used to exist only in the Caddyfile; another proxy meant none at all."""
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]


# --- A5 / A7: headers the app sets for itself --------------------------------


def test_the_law_page_is_never_stored_in_a_shared_cache(client, open_law):
    """It renders the visitor's own ballot."""
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


# --- Reconnaissance surface ---------------------------------------------------


def test_the_api_schema_is_public_in_development(client):
    assert client.get("/openapi.json").status_code == 200


def test_production_hides_the_api_schema():
    """The schema enumerates every administration route."""
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
    """V3 opens this form to every authenticated user; an href is executable."""

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
    """Defence in depth: a row that predates the form check must stay inert."""

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
    """Home and search carry nothing about who is reading them.

    The site's default is `private, no-store`, which is right for the law page
    and the administration and wrong here: forbidding every cache means a popular
    votation recomputes the same aggregates once per reader.
    """
    # Every page that is the same for everyone, not just the two that had it:
    # the legal notices, the privacy page and the authentication page carry
    # nothing about the reader either, and were being recomputed and re-sent on
    # every single view — while `app/routes/public/pages.py` said in its own
    # docstring that they all opted in.
    for path in ("/", "/recherche", "/connexion", "/vie-privee", "/mentions-legales"):
        cache = client.get(path).headers["cache-control"]
        assert cache == "public, max-age=60", f"{path} carries {cache!r}"


def test_an_open_votation_is_cached_only_briefly(client, open_law):
    """One of the readers is somebody who has just voted.

    A minute of caching would let their own browser answer « voir les résultats »
    from a copy taken before their ballot — which reads as the vote not having
    counted, on a platform whose premise is that you can check the number.
    """
    cache = client.get(f"/loi/{open_law.slug}/resultats").headers["cache-control"]
    assert cache == "public, max-age=30"


def test_a_closed_votation_is_cached_properly(client, closed_law):
    """Nothing moves once the vote is over, so the page can be held longer."""
    cache = client.get(f"/loi/{closed_law.slug}/resultats").headers["cache-control"]
    assert cache == "public, max-age=300"


def test_the_ballot_is_still_never_cached(client, open_law):
    """The law page renders this visitor's own vote, so it opts out of all of it."""
    assert client.get(f"/loi/{open_law.slug}").headers["cache-control"] == "private, no-store"


def test_a_missing_law_is_not_cached_as_a_result(client):
    """A 404 must not inherit the results page's caching.

    An unpublished law that is published a minute later would otherwise keep
    answering 404 from whatever cache took the first answer.
    """
    r = client.get("/loi/pas-de-loi/resultats")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "private, no-store"
