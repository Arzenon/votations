"""Shared Jinja2 templates instance, filters and context processors."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.auth.base import get_auth_provider
from app.config import get_settings
from app.labels import field_label, french_date, french_datetime, import_status, law_status
from app.security import safe_url

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# What the site is, in one sentence. Used as the default meta description — the
# line search engines and messaging apps show under the link — and as the lede on
# the home page. Defined once because it was drifting: the two copies said nearly
# the same thing in not quite the same words.
TAGLINE = (
    "Donnez votre avis sur les lois adoptées par le Parlement : données "
    "ouvertes, aucune donnée personnelle collectée."
)

# Permanent transparency disclaimer, shown on every page (charter requirement).
DISCLAIMER = (
    "Votations Françaises n'est pas un sondage représentatif. Les personnes qui "
    "participent se sélectionnent elles-mêmes : les résultats expriment l'opinion "
    "des personnes inscrites sur la plateforme et ne peuvent pas être généralisés "
    "à la population française."
)


def _auth_context(request: Request) -> dict:
    """Resolve the auth provider per render, not once at import time.

    Binding it into the globals at import made it impossible to swap the provider
    — which is the whole point of the seam, with FranceConnect due in V2.
    """
    return {"auth": get_auth_provider()}


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_auth_context])

templates.env.globals["DISCLAIMER"] = DISCLAIMER
templates.env.globals["TAGLINE"] = TAGLINE


def _digest(name: str) -> str:
    """A short content fingerprint of one static file, or "" if unreadable."""
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


_cached_digest = lru_cache(maxsize=64)(_digest)


def asset_url(name: str) -> str:
    """``/static/<name>`` with a content fingerprint appended.

    The static files are served with a one-year immutable cache, which is only
    safe if the URL changes when the file does. Hashing the bytes keeps that true
    with no build step: a deployed edit produces a new URL, and an unchanged file
    keeps its cached copy.

    The hash is memoised in production only, where the files genuinely cannot
    change under a running server. In development they change constantly, and
    `--reload` restarts on Python edits alone — so a cached fingerprint plus an
    immutable Cache-Control meant an edited stylesheet kept its old URL and the
    browser kept serving the old bytes. The change appeared not to have happened.
    """
    digest = _cached_digest(name) if get_settings().is_production else _digest(name)
    return f"/static/{name}?v={digest}" if digest else f"/static/{name}"


templates.env.globals["asset_url"] = asset_url


def readable_json(value) -> str:
    """Pretty JSON with accents left as accents.

    Jinja's ``tojson`` escapes non-ASCII, so an admin editing French arguments
    would face ``d\\u00e9pistage`` instead of ``dépistage``. The result is a plain
    string, so Jinja still escapes it for HTML on output.
    """
    return json.dumps(value or [], ensure_ascii=False, indent=2)


def paragraphs(value: str | None) -> list[str]:
    """Split a text block into paragraphs on blank lines.

    The presentation blocks are written (and drafted) as several paragraphs
    separated by an empty line; rendering them into a single ``<p>`` collapsed
    every one of them into a wall of text.
    """
    if not value:
        return []
    return [block.strip() for block in value.replace("\r\n", "\n").split("\n\n") if block.strip()]


templates.env.filters["readable_json"] = readable_json
templates.env.filters["paragraphs"] = paragraphs
templates.env.filters["law_status"] = law_status
templates.env.filters["import_status"] = import_status
templates.env.filters["field_label"] = field_label
templates.env.filters["french_date"] = french_date
templates.env.filters["french_datetime"] = french_datetime
# Defence in depth. The admin form refuses a non-http(s) link, but that is one
# gate on one path: seed data, a migration, a direct database edit or a future
# importer could all put a `javascript:` URL in a column that templates render as
# an href. Sanitising again at render means a stored payload is inert wherever it
# came from, and an empty result lets the template drop the link entirely.
templates.env.filters["safe_href"] = safe_url
