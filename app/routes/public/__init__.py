"""Public-facing routes.

Split by what a URL is about — the pages that stand on their own, the three that
carry a law's slug, and the exports — because one module had grown to hold the
home page, the ballot, the results, two streaming exports, the sitemap and six
refusal pages. Same reason the administration was split; see
:mod:`app.routes.admin`.
"""

from fastapi import APIRouter, Depends

from app.routes.public import exports, laws, pages
from app.security import require_same_origin

# Voting is a state change a third-party page must not be able to trigger:
# SameSite=Lax withholds an existing voter cookie from a cross-site POST but does
# not stop a fresh one being minted, so any page could otherwise turn its
# visitors into ballots. Declared once here, inherited by all three; it is a
# no-op on reads.
router = APIRouter(dependencies=[Depends(require_same_origin)])
router.include_router(pages.router)
router.include_router(laws.router)
router.include_router(exports.router)

__all__ = ["router"]
