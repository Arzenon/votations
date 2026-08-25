"""Settings: what the application refuses to boot on, and what an operator must decide.

Several of these settings *are* the privacy guarantees the charter makes, so a
typo in one of them has to stop the process rather than quietly serve something
else. The rest of the file is the other half of that: `.env.example` must list
every value with no safe default, and nothing that has one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import InsecureConfiguration, Settings, check_production_config

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


def test_a_non_ascii_admin_password_is_refused_at_boot():
    """HTTP Basic cannot carry it here, so it could only ever lock the admin out."""
    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_password="clé-très-sûre")

    with pytest.raises(ValueError, match="ASCII"):
        Settings(admin_username="administratrice-éé")


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


# --- `.env.example` is the other half of the configuration surface -------------
#
# The file used to document all forty-odd settings, which made it a reference
# nobody read to the end: the five values that actually have to be filled in were
# buried among pool sizes and refresh cadences. It now carries only the settings
# with no safe default, and both halves of that are asserted below — a missing
# essential is a value somebody forgets to set, and an extra one is how the file
# grows back into a wall of text.

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# The settings an operator must decide on. Adding one here without adding it to
# `.env.example` fails the test, and vice versa — that is the point.
ESSENTIAL = {
    # No safe default: the app refuses to boot on the shipped values.
    "environment",
    "secret_key",
    "admin_username",
    "admin_password",
    "cookie_secure",
    # Depends on where and how it runs.
    "database_url",
    "trusted_proxy_hops",
    # Govern what the platform publishes about its voters.
    "min_bucket_volume",
    "min_cell_count",
    "export_time_resolution",
    # Legally required of any French site open to the public.
    "legal_editor",
    "legal_editor_address",
    "legal_publication_director",
    "legal_contact_email",
    "legal_host",
    # The switch for an optional feature; absent, the feature is simply gone.
    "anthropic_api_key",
}


def _documented() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return {m.group(1).lower() for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)}


def test_every_essential_setting_really_exists():
    """Catches a setting renamed or removed without updating this list."""
    unknown = ESSENTIAL - set(Settings.model_fields)
    assert not unknown, f"listés comme essentiels mais absents de Settings : {sorted(unknown)}"


def test_the_example_documents_every_essential_setting():
    missing = ESSENTIAL - _documented()
    assert not missing, f"à documenter dans .env.example : {sorted(missing)}"


def test_the_example_documents_nothing_else():
    """Anything with a working default belongs in app/config.py, not here.

    This is the half that keeps the file short. Without it, every new setting
    gets added "for completeness" and the file drifts back to being unreadable.
    """
    extra = _documented() - ESSENTIAL
    assert not extra, (
        "ces réglages ont une valeur par défaut qui marche : documentez-les dans "
        f"app/config.py plutôt que dans .env.example — {sorted(extra)}"
    )


def test_the_example_file_carries_no_real_secret():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            assert line.strip() == "ANTHROPIC_API_KEY="
        if line.startswith("SECRET_KEY="):
            assert "change-me" in line
