"""Administration routes.

Guarded by HTTP Basic against env-configured credentials for the MVP. This is a
provisional measure; V3 replaces it with FranceConnect-based roles.

Split by resource — the guard, the form, the laws, the staged imports — because
one module had grown to hold authentication, timezone parsing, French date
formatting, form assembly and twelve routes across two resources.
"""

from fastapi import APIRouter, Depends

from app.routes.admin import imports, laws
from app.routes.admin.forms import InvalidFormValue
from app.routes.admin.guard import AUTH_REALM, require_admin
from app.security import require_same_origin

# Every admin route inherits the cross-site check; it is a no-op on reads.
# The prefix is applied on include rather than on this router: the dashboard sits
# at "/admin" exactly, and FastAPI refuses a route whose prefix and path are both
# empty at the moment of inclusion.
router = APIRouter(dependencies=[Depends(require_same_origin)])
router.include_router(laws.router, prefix="/admin")
router.include_router(imports.router, prefix="/admin")

__all__ = ["AUTH_REALM", "InvalidFormValue", "require_admin", "router"]
