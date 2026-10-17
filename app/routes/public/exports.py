"""Open data exports: a law's votes streamed as CSV or JSON.

Rate limited per address and capped in concurrency. See `app.crud.results` for
what the rows contain.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app import ratelimit, seo
from app.config import get_settings
from app.crud import demographics as demo_crud
from app.crud import results as results_crud
from app.db import get_db
from app.models import Law
from app.routes.public.common import exports_busy, not_found, too_many_exports, visible
from app.security import is_head

router = APIRouter()

# Rows are buffered and yielded in chunks of this size. Each yield of a sync
# generator costs a thread pool round trip and an HTTP chunk, so yielding per
# vote was very slow.
_CHUNK_CHARS = 64 * 1024

_CSV_TYPE = "text/csv; charset=utf-8"
_JSON_TYPE = "application/json; charset=utf-8"


def _headers_only(media_type: str, headers: dict[str, str]) -> Response:
    """Response to a HEAD request: headers only, without scanning the votes."""
    return Response(status_code=200, media_type=media_type, headers=headers)


def at_capacity(request: Request) -> HTMLResponse | None:
    """A 503 page if too many exports are streaming, else None.

    Each export holds a DB connection while the client reads. Also applies to
    the admin export. Doesn't count against the caller's rate limit.
    """
    if not ratelimit.slots_free(limit=get_settings().export_max_concurrent):
        return exports_busy(request)
    return None


def _export_refused(request: Request) -> HTMLResponse | None:
    """A refusal page (capacity, then rate limit), or None."""
    refused = at_capacity(request)
    if refused is not None:
        return refused
    settings = get_settings()
    if ratelimit.too_many(
        request,
        limit=settings.export_rate_limit,
        window_seconds=settings.export_rate_window_seconds,
        bucket="export",
    ):
        return too_many_exports(request)
    return None


def _download_headers(law: Law, extension: str) -> dict[str, str]:
    """Content-Disposition header, built from the stored slug."""
    return {"Content-Disposition": f'attachment; filename="votations-{law.slug}.{extension}"'}


def csv_response(db: Session, law: Law, *, head: bool = False) -> Response:
    """A law's votes as streamed CSV.

    Also used by the admin for archived laws, with the same anonymity rules.
    """
    if head:
        return _headers_only(_CSV_TYPE, _download_headers(law, "csv"))

    total = results_crud.count_votes(db, law.id)

    def rows() -> Iterator[str]:
        # Taken inside the generator so a client disconnecting early can't leak it.
        with ratelimit.export_slot():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=["law_id", "choice", "created_at"])
            writer.writeheader()
            for row in results_crud.iter_export_rows(db, law.id, total=total):
                writer.writerow(row)
                if buf.tell() >= _CHUNK_CHARS:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
            tail = buf.getvalue()
            if tail:
                yield tail

    return StreamingResponse(rows(), media_type=_CSV_TYPE, headers=_download_headers(law, "csv"))


@router.get("/loi/{slug}/export.csv")
def export_csv(request: Request, slug: str, db: Session = Depends(get_db)):
    # Limits are checked before the lookup, so unknown slugs count too.
    refused = _export_refused(request)
    if refused is not None:
        return refused
    law = visible(db, slug)
    if law is None:
        return not_found(request)
    return csv_response(db, law, head=is_head(request))


@router.get("/loi/{slug}/export.json")
def export_json(request: Request, slug: str, db: Session = Depends(get_db)):
    refused = _export_refused(request)
    if refused is not None:
        return refused
    law = visible(db, slug)
    if law is None:
        return not_found(request)
    if is_head(request):
        return _headers_only(_JSON_TYPE, _download_headers(law, "json"))

    tally = results_crud.tally(db, law.id)
    total = tally.total

    # Only breakdowns that pass the suppression threshold.
    demographics = [
        {
            "dimension": bd.dimension,
            "rows": [
                {
                    "category": r.category,
                    "counts": r.counts,
                    "share_pct": r.share,
                    "reference_pct": r.reference,
                }
                for r in bd.rows
            ],
        }
        for bd in demo_crud.all_breakdowns(db, law, profiled=tally.with_profile)
        if bd.shown
    ]
    # Same snapshot date as the results page.
    as_of = demo_crud.published_as_of(db, law, profiled=tally.with_profile)
    provision = demo_crud.provision_rate(db, law.id, tally=tally)
    envelope = {
        "law": {"id": law.id, "slug": law.slug, "title": law.title},
        "demographics": demographics,
        "profile_provision": {
            "total_votes": provision.total_votes,
            "with_profile": provision.with_profile,
            "percentage": provision.percentage,
        },
        "licence": {"name": seo.DATA_LICENCE_NAME, "url": seo.DATA_LICENCE_URL},
        "votes_note": results_crud.export_note(db, law.id, total=total),
        "demographics_as_of": as_of.isoformat() if as_of else None,
        "demographics_note": (
            "Répartitions agrégées uniquement, masquées sous le seuil "
            "d'anonymat. Aucune ligne par votant·e. 'reference_pct' = ordre "
            "de grandeur national INSEE (approx.), jamais utilisé pour "
            "repondérer. Lorsque 'demographics_as_of' est renseigné, ces "
            "répartitions y sont arrêtées et n'avancent que par paliers : les "
            "publier en direct permettrait de déduire le profil d'une personne "
            "en comparant deux téléchargements."
        ),
    }

    def document() -> Iterator[str]:
        """Stream the JSON envelope, with the votes written in chunks."""
        with ratelimit.export_slot():
            head = json.dumps(envelope, ensure_ascii=False)
            # Insert the votes array before the closing brace.
            yield head[:-1] + ', "votes": ['
            pending: list[str] = []
            size = 0
            first = True
            for row in results_crud.iter_export_rows(db, law.id, total=total):
                piece = ("" if first else ",") + json.dumps(row, ensure_ascii=False)
                first = False
                pending.append(piece)
                size += len(piece)
                if size >= _CHUNK_CHARS:
                    yield "".join(pending)
                    pending.clear()
                    size = 0
            pending.append("]}")
            yield "".join(pending)

    return StreamingResponse(
        document(), media_type=_JSON_TYPE, headers=_download_headers(law, "json")
    )
