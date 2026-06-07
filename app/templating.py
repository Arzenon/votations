"""Shared Jinja2 templates instance and globals."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.auth.base import get_auth_provider

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Permanent transparency disclaimer, shown on every page (charter requirement).
DISCLAIMER = (
    "Votations Françaises n'est pas un sondage représentatif. Les personnes qui "
    "participent se sélectionnent elles-mêmes : les résultats expriment l'opinion "
    "des personnes inscrites sur la plateforme et ne peuvent pas être généralisés "
    "à la population française."
)

templates.env.globals["DISCLAIMER"] = DISCLAIMER
templates.env.globals["auth"] = get_auth_provider()
