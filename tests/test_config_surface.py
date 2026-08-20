"""`.env.example` must list what an operator has to decide — and nothing else.

The file used to document all forty-odd settings, which made it a reference
nobody read to the end: the five values that actually have to be filled in were
buried among pool sizes and refresh cadences. It now carries only the settings
with no safe default — secrets, whatever depends on the installation, and the
dials that govern what the platform publishes.

Everything else keeps its default and is documented where it is defined, in
`app/config.py`. Both halves are asserted here, because both fail quietly: a
missing essential is a value somebody forgets to set, and an extra one is how the
file grows back into a wall of text.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

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
