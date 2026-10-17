"""Shared Jinja2 templates instance, filters and context processors."""

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.auth.base import get_auth_provider
from app.config import get_settings
from app.labels import (
    CHOICE_LABELS,
    field_label,
    french_date,
    french_datetime,
    home_band,
    import_status,
    law_status,
)
from app.security import safe_url
from app.textdiff import field_change

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Default meta description and home page lede.
TAGLINE = (
    "Donnez votre avis sur les lois adoptées par le Parlement : données "
    "ouvertes, aucune donnée personnelle collectée."
)

# The permanent disclaimer. The short version is the banner on every page (the
# long one took a fifth of a phone screen); the full version goes on pages that
# show figures.
DISCLAIMER_SHORT = (
    "Votations Françaises n'est pas un sondage représentatif : les personnes qui "
    "participent se sélectionnent elles-mêmes."
)

DISCLAIMER = (
    "Votations Françaises n'est pas un sondage représentatif. Les personnes qui "
    "participent se sélectionnent elles-mêmes : les résultats expriment l'opinion "
    "des personnes inscrites sur la plateforme et ne peuvent pas être généralisés "
    "à la population française."
)


def _auth_context(request: Request) -> dict:
    """Resolve the auth provider on each render, so it can be swapped."""
    return {"auth": get_auth_provider()}


def _page_context(request: Request) -> dict:
    """Absolute origin and canonical URL, for tags that need full URLs.

    The origin comes from the request (the domain isn't configured); uvicorn
    runs with --proxy-headers so it's https in production. The canonical URL
    drops the query string so paginated variants aren't indexed separately.
    """
    origin = str(request.base_url).rstrip("/")
    return {"site_origin": origin, "canonical_url": f"{origin}{request.url.path}"}


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR), context_processors=[_auth_context, _page_context]
)

# Don't check templates for changes on every render in production.
templates.env.auto_reload = not get_settings().is_production

templates.env.globals["DISCLAIMER"] = DISCLAIMER
templates.env.globals["DISCLAIMER_SHORT"] = DISCLAIMER_SHORT
templates.env.globals["TAGLINE"] = TAGLINE
templates.env.globals["CHOICE_LABELS"] = CHOICE_LABELS
# A global rather than a filter since it takes two values.
templates.env.globals["field_change"] = field_change


def _digest(name: str) -> str:
    """Short content hash of a static file, or "" if unreadable."""
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


_cached_digest = lru_cache(maxsize=64)(_digest)


def asset_url(name: str) -> str:
    """``/static/<name>?v=<hash>``, for cache busting.

    Static files are cached for a year, so the URL must change with the
    content. The hash is only memoised in production, since files change in
    development.
    """
    digest = _cached_digest(name) if get_settings().is_production else _digest(name)
    return f"/static/{name}?v={digest}" if digest else f"/static/{name}"


templates.env.globals["asset_url"] = asset_url


def paragraphs(value: str | None) -> list[str]:
    """Split a text block into paragraphs on blank lines."""
    if not value:
        return []
    return [block.strip() for block in value.replace("\r\n", "\n").split("\n\n") if block.strip()]


# Paragraphs of "Ce que ça change en pratique" shown before folding. The prompt
# orders them by number of people concerned.
VISIBLE_AUDIENCES = 5

# Don't fold just one or two paragraphs.
_WORTH_FOLDING = 3


def practical_split(value: str | None) -> tuple[list[str], list[str]]:
    """Split "Ce que ça change en pratique" into (shown, folded).

    A large law can concern many groups, and none can be left out, so the
    rest is folded instead.
    """
    blocks = paragraphs(value)
    if len(blocks) < VISIBLE_AUDIENCES + _WORTH_FOLDING:
        return blocks, []
    return blocks[:VISIBLE_AUDIENCES], blocks[VISIBLE_AUDIENCES:]


def practical_layout(value: str | None) -> tuple[list[tuple[str, str]], list[str]]:
    """Layout for "Ce que ça change en pratique": (rows, folded).

    If paragraphs start with an audience label, each becomes a collapsible
    row (label, text); unlabelled ones get an empty label. Otherwise falls back
    to `practical_split`.
    """
    rows = [audience_split(para) for para in paragraphs(value)]
    if any(label for label, _ in rows):
        return rows, []
    shown, folded = practical_split(value)
    return [("", para) for para in shown], folded


def summary_split(value: str | None) -> tuple[str, list[str]]:
    """Split "Ce que dit la loi" into (first paragraph, rest).

    The first paragraph goes in the box at the top, unless it's the only one.
    """
    blocks = paragraphs(value)
    if len(blocks) < 2:
        return "", blocks
    return blocks[0], blocks[1:]


# Max length of an audience label. Real labels go up to 55 characters
# ("Si vous travaillez dans un secteur scientifique protégé"). Punctuation is
# the main filter; this catches long clauses without any.
MAX_AUDIENCE_CHARS = 75

# A label never contains these. Commas are allowed: "Pour les établissements
# de santé, EHPAD et services médico-sociaux" is a valid label.
_NOT_IN_A_LABEL = ".?!;"


def audience_split(paragraph: str) -> tuple[str, str]:
    """Split a paragraph into (audience label, rest) on " : ".

    e.g. "Pour les employeurs : ...". Returns ``("", paragraph)`` when there's
    no label.
    """
    head, colon, rest = paragraph.partition(" : ")
    if not colon or len(head) > MAX_AUDIENCE_CHARS or not rest.strip():
        return "", paragraph
    if any(mark in head for mark in _NOT_IN_A_LABEL):
        return "", paragraph
    return head, rest


templates.env.filters["paragraphs"] = paragraphs
templates.env.filters["summary_split"] = summary_split
templates.env.filters["audience_split"] = audience_split
templates.env.filters["practical_split"] = practical_split
templates.env.filters["practical_layout"] = practical_layout
templates.env.filters["law_status"] = law_status
templates.env.filters["home_band"] = home_band
templates.env.filters["import_status"] = import_status
templates.env.filters["field_label"] = field_label
templates.env.filters["french_date"] = french_date
templates.env.filters["french_datetime"] = french_datetime
# Re-check URLs at render time: they might not have gone through the admin form.
templates.env.filters["safe_href"] = safe_url
