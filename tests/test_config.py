"""Settings: what the app refuses to start with, and what `.env.example` lists."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import InsecureConfiguration, Settings, check_production_config

# --- The boot guard -----------------------------------------------------------


def test_development_runs_on_defaults():
    """Development works with the defaults."""
    check_production_config(Settings())


def test_production_refuses_a_placeholder_secret_key():
    """Production refuses the placeholder SECRET_KEY."""
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


def test_a_non_ascii_admin_password_is_refused_at_boot():
    """A non-ASCII admin password is refused (HTTP Basic can't carry it)."""
    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_password="clé-très-sûre")

    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_username="administratrice-éé")


# --- Invalid values are refused ------------------------------------------------


def test_a_typo_in_the_export_resolution_is_refused_at_boot():
    """A typo like EXPORT_TIME_RESOLUTION=days is refused."""
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


# --- .env.example -----------------------------------------------------------------
#
# Only lists what an operator has to decide, no more and no less.

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"
PROD_COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.prod.yml"


# Settings the operator must set. Must match `.env.example`.
ESSENTIAL = {
    # No safe default.
    "secret_key",
    "admin_username",
    "admin_password",
    # Govern what the platform publishes about its voters.
    "min_bucket_volume",
    "min_cell_count",
    "export_time_resolution",
    # Legal notices (the host is mandatory).
    "legal_contact_email",
    "legal_host",
    # Optional feature.
    "anthropic_api_key",
}

# Variables read by docker compose, not by the app.
DEPLOYMENT_ONLY = {
    "site_domain",
    "postgres_password",
}

# Set by docker-compose.prod.yml's `environment:` (which overrides .env), so
# they must not be in the example.
IMPOSED_BY_THE_STACK = {
    "ENVIRONMENT",
    "COOKIE_SECURE",
    "DATABASE_URL",
    "TRUSTED_PROXY_HOPS",
}

#: Everything `.env.example` is expected to carry.
EXPECTED = ESSENTIAL | DEPLOYMENT_ONLY


def _documented() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return {m.group(1).lower() for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)}


def test_every_essential_setting_really_exists():
    """Every setting listed here exists."""
    unknown = ESSENTIAL - set(Settings.model_fields)
    assert not unknown, f"listés comme essentiels mais absents de Settings : {sorted(unknown)}"


def test_the_deployment_variables_are_not_application_settings():
    """DEPLOYMENT_ONLY variables aren't app settings."""
    disguised = DEPLOYMENT_ONLY & set(Settings.model_fields)
    assert not disguised, (
        "ce sont des réglages de l'application : ils relèvent d'ESSENTIAL, "
        f"pas de DEPLOYMENT_ONLY : {sorted(disguised)}"
    )


def test_the_example_documents_everything_it_should():
    missing = EXPECTED - _documented()
    assert not missing, f"à documenter dans .env.example : {sorted(missing)}"


def test_the_example_documents_nothing_else():
    """`.env.example` contains nothing else."""
    extra = _documented() - EXPECTED
    assert not extra, (
        "ces réglages ont une valeur par défaut qui marche : documentez-les dans "
        f"app/config.py plutôt que dans .env.example : {sorted(extra)}"
    )


def test_the_settings_the_stack_imposes_stay_out_of_the_example():
    """Settings imposed by the prod compose file are really set there."""
    compose = PROD_COMPOSE.read_text(encoding="utf-8")
    for name in sorted(IMPOSED_BY_THE_STACK):
        assert re.search(rf"^\s+{name}:", compose, re.MULTILINE), (
            f"{name} n'est plus imposé par docker-compose.prod.yml : il redevient "
            "un choix d'opérateur, à documenter dans .env.example et à lister "
            "dans ESSENTIAL"
        )
        assert name.lower() not in _documented(), (
            f"{name} est imposé par docker-compose.prod.yml, dont le bloc "
            "`environment` l'emporte sur `.env` : le documenter ici laisse croire "
            "qu'on peut le changer là"
        )


def test_the_example_file_carries_no_real_secret():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            assert line.strip() == "ANTHROPIC_API_KEY="
        if line.startswith("SECRET_KEY="):
            assert "change-me" in line


# --- Carrying a signing key across a move -------------------------------------


def test_a_fallback_key_is_offered_to_the_verifier_oldest_first():
    settings = Settings(
        secret_key="la-nouvelle", secret_key_fallbacks="la-vieille, l-avant-vieille"
    )
    assert settings.secret_key_fallbacks == ["la-vieille", "l-avant-vieille"]
    # itsdangerous signs with the last and verifies with any.
    assert settings.signing_keys[-1] == "la-nouvelle"


def test_no_fallback_is_the_ordinary_case():
    assert Settings(secret_key="seule").signing_keys == ["seule"]


def test_production_refuses_a_placeholder_among_the_fallbacks():
    """A placeholder among the fallback keys is refused too."""
    settings = Settings(
        environment="production",
        secret_key="x" * 40,
        secret_key_fallbacks="dev-insecure-secret-change-me",
        admin_password="mot-de-passe-solide",
        cookie_secure=True,
    )
    with pytest.raises(InsecureConfiguration, match="SECRET_KEY_FALLBACKS"):
        check_production_config(settings)


def test_production_refuses_a_short_fallback():
    settings = Settings(
        environment="production",
        secret_key="x" * 40,
        secret_key_fallbacks="trop-courte",
        admin_password="mot-de-passe-solide",
        cookie_secure=True,
    )
    with pytest.raises(InsecureConfiguration, match="shorter than 32"):
        check_production_config(settings)


def test_production_refuses_a_rotation_that_rotated_nothing():
    settings = Settings(
        environment="production",
        secret_key="x" * 40,
        secret_key_fallbacks="x" * 40,
        admin_password="mot-de-passe-solide",
        cookie_secure=True,
    )
    with pytest.raises(InsecureConfiguration, match="repeats SECRET_KEY"):
        check_production_config(settings)


def test_a_real_rotation_boots():
    settings = Settings(
        environment="production",
        secret_key="n" * 40,
        secret_key_fallbacks="v" * 40,
        admin_password="mot-de-passe-solide",
        cookie_secure=True,
    )
    check_production_config(settings)
