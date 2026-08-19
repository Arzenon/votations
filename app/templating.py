"""Shared Jinja2 templates instance, filters and context processors."""

import json
from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.auth.base import get_auth_provider
from app.labels import import_status, law_status

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

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
