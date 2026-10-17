"""Public routes: standalone pages, law pages and exports."""

from fastapi import APIRouter, Depends

from app.routes.public import exports, laws, pages
from app.security import require_same_origin

# Same-origin check for POSTs (voting), see `require_same_origin`.
router = APIRouter(dependencies=[Depends(require_same_origin)])
router.include_router(pages.router)
router.include_router(laws.router)
router.include_router(exports.router)

__all__ = ["router"]
