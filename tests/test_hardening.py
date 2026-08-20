"""Production safety: forgeable secrets, cross-site requests, rate-limit keying."""

import base64
from types import SimpleNamespace

import pytest

from app.config import InsecureConfiguration, Settings, check_production_config, get_settings


def _admin_auth() -> dict:
    s = get_settings()
    token = base64.b64encode(f"{s.admin_username}:{s.admin_password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- The boot guard -----------------------------------------------------------


def test_development_runs_on_defaults():
    """Local work and the test suite must stay frictionless."""
    check_production_config(Settings())


def test_production_refuses_a_placeholder_secret_key():
    """On the shipped key anyone can forge a voter cookie and vote without limit."""
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(Settings(environment="production"))
    assert "SECRET_KEY" in str(exc.value)


def test_production_refuses_a_short_secret_key():
    cfg = Settings(
        environment="production",
        secret_key="tooshort",
        admin_password="long-enough-x",
        cookie_secure=True,
    )
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(cfg)
    assert "32" in str(exc.value)


def test_production_refuses_a_placeholder_admin_password():
    cfg = Settings(
        environment="production",
        secret_key="k" * 50,
        admin_password="change-me",
        cookie_secure=True,
    )
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(cfg)
    assert "ADMIN_PASSWORD" in str(exc.value)


def test_production_refuses_insecure_cookies():
    cfg = Settings(
        environment="production",
        secret_key="k" * 50,
        admin_password="long-enough-x",
        cookie_secure=False,
    )
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(cfg)
    assert "COOKIE_SECURE" in str(exc.value)


def test_a_properly_configured_production_boots():
    check_production_config(
        Settings(
            environment="production",
            secret_key="k" * 50,
            admin_password="a-real-password",
            cookie_secure=True,
        )
    )


def test_the_error_says_how_to_generate_a_key():
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(Settings(environment="production"))
    assert "secrets.token_urlsafe" in str(exc.value)
    assert "DEPLOY.md" in str(exc.value)


# --- Cross-site admin requests ------------------------------------------------


def test_the_archive_route_rejects_a_cross_site_post(client, open_law):
    """The attack: a page the admin visits while authenticated deletes a law."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={**_admin_auth(), "Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    # And the law is still there.
    assert client.get("/admin", headers=_admin_auth()).text.count(open_law.title) >= 1


def test_a_forged_referer_is_rejected_too(client, open_law):
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={**_admin_auth(), "Referer": "https://evil.example/piege.html"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_a_lookalike_host_is_rejected(client, open_law):
    """`testserver.evil.example` must not pass a prefix check on `testserver`."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={**_admin_auth(), "Origin": "http://testserver.evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_the_admins_own_form_still_works(client, open_law):
    r = client.post(
        f"/admin/lois/{open_law.id}/archiver",
        headers={**_admin_auth(), "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_reads_are_never_blocked(client):
    r = client.get("/admin", headers={**_admin_auth(), "Origin": "https://evil.example"})
    assert r.status_code == 200


def test_the_check_runs_before_authentication_is_even_relevant(client, open_law):
    """A cross-site request is refused whether or not it carries credentials."""
    r = client.post(
        f"/admin/lois/{open_law.id}/supprimer",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)


# --- Health probe -------------------------------------------------------------


def test_health_probe_reports_the_database(client):
    r = client.get("/sante")
    assert r.status_code == 200
    assert r.text == "ok"


# --- Vote rate limiting -------------------------------------------------------


def test_a_burst_of_votes_is_refused(client, open_law, monkeypatch):
    """A fresh cookie is a fresh voter, so bursts are the cheapest attack."""
    from app import ratelimit

    ratelimit.reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 3, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False
        ).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [303, 303, 303]
    assert codes[3:] == [429, 429]
    ratelimit.reset()


def test_the_limit_message_does_not_leak_the_threshold(client, open_law, monkeypatch):
    from app import ratelimit

    ratelimit.reset()
    monkeypatch.setattr(get_settings(), "vote_rate_limit", 1, raising=False)
    client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    r = client.post(f"/loi/{open_law.slug}/vote", data={"choice": "for"}, follow_redirects=False)
    assert r.status_code == 429
    assert "Trop de votes" in r.text
    assert "1" not in r.text.split("<h1>")[1].split("</h1>")[0]
    ratelimit.reset()


def test_counters_are_never_persisted():
    """IP addresses are personal data: they live in this process and nowhere else."""
    from app import ratelimit

    ratelimit.reset()
    assert ratelimit._hits == {}


# --- Rate-limit keying --------------------------------------------------------
#
# The limiter keys on a client address. Reverse proxies *append* to
# X-Forwarded-For, so the leftmost entries are whatever the caller wrote: reading
# from that end handed the key to the attacker and made the whole control a no-op.


def test_a_forged_forwarded_for_cannot_buy_a_fresh_bucket(client, open_law, monkeypatch):
    """The attack: one new fake address per request, and the limit never bites."""
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 2, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote",
            data={"choice": "for"},
            # The caller invents a different left-hand entry every time; our own
            # proxy appended the real one on the right.
            headers={"X-Forwarded-For": f"9.9.9.{i}, 10.0.0.1"},
            follow_redirects=False,
        ).status_code
        for i in range(5)
    ]
    assert codes[:2] == [303, 303]
    assert codes[2:] == [429, 429, 429], "the spoofed left-hand entry must not be the key"


def test_the_real_client_address_still_separates_voters(client, open_law, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 1, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1, raising=False)

    first = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for"},
        headers={"X-Forwarded-For": "203.0.113.7"},
        follow_redirects=False,
    )
    other = client.post(
        f"/loi/{open_law.slug}/vote",
        data={"choice": "for"},
        headers={"X-Forwarded-For": "203.0.113.8"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert other.status_code == 303, "a genuinely different address gets its own budget"


def test_without_a_declared_proxy_the_header_is_ignored(client, open_law, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vote_rate_limit", 2, raising=False)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0, raising=False)

    codes = [
        client.post(
            f"/loi/{open_law.slug}/vote",
            data={"choice": "for"},
            headers={"X-Forwarded-For": f"9.9.9.{i}"},
            follow_redirects=False,
        ).status_code
        for i in range(4)
    ]
    assert codes[2:] == [429, 429]


def test_the_tracking_table_is_bounded(monkeypatch):
    """A flood of forged keys must not grow the table without bound."""
    from app import ratelimit

    ratelimit.reset()
    monkeypatch.setattr(ratelimit, "_MAX_TRACKED", 50)

    def request_from(address: str):
        """The two attributes the limiter reads off a request."""
        return SimpleNamespace(headers={}, client=SimpleNamespace(host=address))

    refused = sum(
        ratelimit.too_many(
            request_from(f"10.0.{i // 256}.{i % 256}"), limit=100, window_seconds=300
        )
        for i in range(400)
    )
    assert len(ratelimit._hits) <= 50
    # Past the cap the least recently active entries are evicted, and the caller
    # is still admitted. Refusing instead — which is what this used to do — turned
    # a flood into a site-wide outage for every address not already tracked, which
    # is precisely the outcome an attacker was after.
    assert refused == 0, "a full table must evict, not refuse legitimate voters"
    ratelimit.reset()


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


# --- Administrator brute force ------------------------------------------------


def test_repeated_bad_passwords_are_throttled(client, monkeypatch):
    """A static credential pair with no lockout is guessable at network speed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_rate_limit", 3, raising=False)
    wrong = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}

    codes = [client.get("/admin", headers=wrong).status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401]

    # And the correct password is refused too while that address is guessing,
    # so the throttle cannot be sidestepped by landing on the right one.
    assert client.get("/admin", headers=_admin_auth()).status_code == 429


def test_a_working_administrator_is_never_slowed_down(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_rate_limit", 3, raising=False)
    codes = [client.get("/admin", headers=_admin_auth()).status_code for _ in range(10)]
    assert codes == [200] * 10


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
    from app.models import Law

    r = client.post(
        "/admin/lois",
        headers={**_admin_auth(), "Origin": "http://testserver"},
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
    from app.models import Law

    r = client.post(
        "/admin/lois",
        headers={**_admin_auth(), "Origin": "http://testserver"},
        data={
            "title": "Loi piégée 2",
            "published": "1",
            "arguments_for": '[{"text":"t","author":"a","source":"javascript:alert(1)"}]',
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert db_session.query(Law).filter_by(title="Loi piégée 2").first() is None


def test_a_stored_javascript_url_still_never_renders(client, db_session):
    """Defence in depth: a row that predates the form check must stay inert."""
    from app.models import Law

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
    from app.models import Law

    client.post(
        "/admin/lois",
        headers={**_admin_auth(), "Origin": "http://testserver"},
        data={
            "title": "Loi normale",
            "published": "1",
            "legifrance_url": "https://www.legifrance.gouv.fr/eli/loi/2026/1/1",
            "arguments_for": '[{"text":"t","author":"a","source":"https://an.fr/cr"}]',
        },
        follow_redirects=False,
    )
    law = db_session.query(Law).filter_by(title="Loi normale").one()
    assert law.legifrance_url.startswith("https://")
    assert law.arguments_for[0]["source"] == "https://an.fr/cr"
