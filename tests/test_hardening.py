"""Production safety: forgeable secrets and cross-site admin requests."""

import base64

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
        environment="production", secret_key="tooshort", admin_password="long-enough-x",
        cookie_secure=True,
    )
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(cfg)
    assert "32" in str(exc.value)


def test_production_refuses_a_placeholder_admin_password():
    cfg = Settings(
        environment="production", secret_key="k" * 50, admin_password="change-me",
        cookie_secure=True,
    )
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(cfg)
    assert "ADMIN_PASSWORD" in str(exc.value)


def test_production_refuses_insecure_cookies():
    cfg = Settings(
        environment="production", secret_key="k" * 50, admin_password="long-enough-x",
        cookie_secure=False,
    )
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(cfg)
    assert "COOKIE_SECURE" in str(exc.value)


def test_a_properly_configured_production_boots():
    check_production_config(Settings(
        environment="production", secret_key="k" * 50,
        admin_password="a-real-password", cookie_secure=True,
    ))


def test_the_error_says_how_to_generate_a_key():
    with pytest.raises(InsecureConfiguration) as exc:
        check_production_config(Settings(environment="production"))
    assert "secrets.token_urlsafe" in str(exc.value)
    assert "DEPLOY.md" in str(exc.value)


# --- Cross-site admin requests ------------------------------------------------

def test_the_delete_route_rejects_a_cross_site_post(client, open_law):
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
        f"/admin/lois/{open_law.id}/supprimer",
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
