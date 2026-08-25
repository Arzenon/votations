"""Authentication routes.

For the MVP this is a placeholder: a /connexion page explaining the provisional
anonymous mechanism, with a (disabled) connect button reserved for the future
FranceConnect integration. The transparency charter requires that we state how
authentication works and what its limits are.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.routes.public.common import PUBLIC_PAGE_CACHE
from app.templating import templates

router = APIRouter()


@router.get("/connexion", response_class=HTMLResponse)
def connexion(request: Request):
    # The same bytes for every visitor, like the home and search pages: it
    # explains the provisional mechanism and carries nothing about who is
    # reading. The site-wide default is `private, no-store`, which is right for
    # the pages that render a ballot and wrong for this one.
    return templates.TemplateResponse(request, "connexion.html", {}, headers=PUBLIC_PAGE_CACHE)
