"""Authentication routes: /connexion explains the provisional mechanism and its limits.

There is nothing to log in to: the voter token is issued when voting.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.routes.public.common import PUBLIC_PAGE_CACHE
from app.templating import templates

router = APIRouter()


@router.get("/connexion", response_class=HTMLResponse)
def connexion(request: Request):
    # Same content for everyone, so cacheable.
    return templates.TemplateResponse(request, "connexion.html", {}, headers=PUBLIC_PAGE_CACHE)
