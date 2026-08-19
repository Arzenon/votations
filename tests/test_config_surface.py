"""The documented configuration must match the configuration that exists.

`.env.example` is the only place an operator learns what they can set. A setting
that exists but is undocumented is a setting nobody will use; a documented one
that no longer exists is a silent no-op in production.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Settings deliberately left out of the example file.
UNDOCUMENTED_ON_PURPOSE: set[str] = set()

# Documented variables that are read by the shell cron scripts in docker/, not by
# the application — so they legitimately have no `Settings` field.
SHELL_ONLY = {
    "an_refresh_interval",
    "an_retry_interval",
    "insee_refresh_interval",
    "insee_retry_interval",
    "backup_interval",
    "backup_keep_days",
    "seed_on_start",
}


def _documented() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return {m.group(1).lower() for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)}


def test_every_setting_is_documented():
    missing = set(Settings.model_fields) - _documented() - UNDOCUMENTED_ON_PURPOSE
    assert not missing, f"à documenter dans .env.example : {sorted(missing)}"


def test_no_documented_setting_has_disappeared():
    stale = _documented() - set(Settings.model_fields) - SHELL_ONLY
    assert not stale, f"présent dans .env.example mais plus dans Settings : {sorted(stale)}"


def test_the_example_file_carries_no_real_secret():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            assert line.strip() == "ANTHROPIC_API_KEY="
        if line.startswith("SECRET_KEY="):
            assert "change-me" in line
