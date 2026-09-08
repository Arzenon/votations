"""Administration routes.

Guarded by HTTP Basic against env-configured credentials for the MVP. This is a
provisional measure: V3 opens this form to every authenticated user, by whatever
mechanism authenticates them by then. Which mechanism that is is deliberately not
promised here — see :mod:`app.auth.base`.

Split by resource — the guard, the form, the laws, the staged imports — because
one module had grown to hold authentication, timezone parsing, French date
formatting, form assembly and twelve routes across two resources.
"""

from fastapi import APIRouter, Depends

from app.routes.admin import imports, laws
from app.routes.admin.forms import InvalidFormValue
from app.routes.admin.guard import AUTH_REALM, require_admin
from app.security import require_same_origin

# Both guards are declared here, once, rather than on each of the fourteen routes
# below them.
#
# The cross-site check always was — it is a no-op on reads, so there is no route
# it does not belong on. `require_admin` was not: every route repeated
# `_: str = Depends(require_admin)` in its signature, and all fourteen of them
# did have it. That is the problem, not the evidence against it — an
# authentication that has to be remembered fourteen times is one that will
# eventually be remembered thirteen, on whichever route is added next, and
# nothing would say so. Declared here it is a property of the URL space rather
# than of each handler, and `test_every_admin_route_is_behind_the_guard` holds it
# there.
#
# Order matters and is the one already in force: a request from another site is
# refused before its credentials are looked at.
#
# The prefix is applied on include rather than on this router: the dashboard sits
# at "/admin" exactly, and FastAPI refuses a route whose prefix and path are both
# empty at the moment of inclusion.
router = APIRouter(dependencies=[Depends(require_same_origin), Depends(require_admin)])
router.include_router(laws.router, prefix="/admin")
router.include_router(imports.router, prefix="/admin")

__all__ = ["AUTH_REALM", "InvalidFormValue", "require_admin", "router"]
