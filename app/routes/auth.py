"""Authentication routes.

One page, /connexion, explaining the provisional anonymous mechanism and its
limits — which the transparency charter requires be stated rather than implied.

No connect control is rendered, and that is a decision rather than an omission:
``AuthProvider.login_enabled`` is False and nothing draws a button behind it. A
disabled one reading « bientôt disponible » stood here and was removed, because
it announced a gate this site does not have on the one page whose job is to say
that voting needs no account.
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
