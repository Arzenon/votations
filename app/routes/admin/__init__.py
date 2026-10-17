"""Admin routes, protected by HTTP Basic for now (see `app.auth.base`)."""

from fastapi import APIRouter, Depends

from app.routes.admin import imports, laws
from app.routes.admin.forms import InvalidFormValue
from app.routes.admin.guard import AUTH_REALM, require_admin
from app.security import require_same_origin

# Guards declared once for all admin routes, so a new route can't forget them.
# The origin check runs before the credentials check. The prefix is set on
# include because FastAPI rejects an empty prefix + empty path ("/admin" itself).
router = APIRouter(dependencies=[Depends(require_same_origin), Depends(require_admin)])
router.include_router(laws.router, prefix="/admin")
router.include_router(imports.router, prefix="/admin")

__all__ = ["AUTH_REALM", "InvalidFormValue", "require_admin", "router"]
