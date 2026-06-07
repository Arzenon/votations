"""Authentication routes.

For the MVP this is a placeholder: a /connexion page explaining the provisional
anonymous mechanism, with a (disabled) connect button reserved for the future
FranceConnect integration. The transparency charter requires that we state how
authentication works and what its limits are.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter()


@router.get("/connexion", response_class=HTMLResponse)
def connexion(request: Request):
    return templates.TemplateResponse(request, "connexion.html", {})
