"""The open-data exports: one law's votes, streamed as CSV or JSON.

Kept apart from the pages because they are a different kind of response and carry
a different set of concerns — a per-address rate limit, a cap on how many may
stream at once, and a privacy rule about what the time column may say. See
:mod:`app.crud.results` for the reductions applied to the rows themselves.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app import ratelimit
from app.config import get_settings
from app.crud import demographics as demo_crud
from app.crud import results as results_crud
from app.db import get_db
from app.models import Law
from app.routes.public.common import exports_busy, not_found, too_many_exports, visible

router = APIRouter()

#: How much of an export is assembled before a piece of it is handed over.
#:
#: Both generators below used to ``yield`` once per vote, which is the obvious
#: shape and the expensive one. Starlette turns a sync generator into an async
#: one with ``iterate_in_threadpool``, so **every** ``yield`` costs a round trip
#: through ``anyio.to_thread`` — and that is the same default limiter
#: ``app.main``'s lifespan pins to ``db_max_threads`` precisely to keep the
#: site's capacity in step with the connection pool. It then sends one ASGI
#: message per piece, which uvicorn writes as its own chunk: about ten bytes of
#: framing around twenty-five bytes of payload.
#:
#: A votation with a hundred thousand votes therefore took a hundred thousand
#: tokens off the limiter that serves every page, and wrote a hundred thousand
#: chunks. Sixty-four kilobytes' worth at a time makes both about two thousand
#: times rarer, and bounds what one export holds in memory at the same figure —
#: which is the property that mattered in the first place, and is unchanged.
_CHUNK_CHARS = 64 * 1024


def at_capacity(request: Request) -> HTMLResponse | None:
    """The pool guard: whether another export may stream at all, from anyone.

    An export holds a database connection for as long as the client takes to
    read it, so a few slow readers can occupy the whole pool and take the
    ordinary pages down with them — well inside every per-address limit, because
    nothing about any one of those requests is excessive.

    Public because the administration's own export is subject to it too: the cap
    exists to protect the database, and a connection held by an administrator is
    held just as long as one held by anybody else. What the administration is
    exempt from is the *rate* limit below, which is a different question.

    Deliberately records no hit: refusing someone because the server is busy must
    not also spend the allowance they would need to come back in a minute.
    """
    if not ratelimit.slots_free(limit=get_settings().export_max_concurrent):
        return exports_busy(request)
    return None


def _export_refused(request: Request) -> HTMLResponse | None:
    """Why this public export may not start right now, or None if it may.

    Two different limits, in the order that keeps them honest. Capacity is about
    everyone at once (see :func:`at_capacity`); the rate limit is about one
    caller downloading in a loop, where one download is a full scan of a law's
    votes and legitimate use is a handful of files.
    """
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


def csv_response(db: Session, law: Law) -> StreamingResponse:
    """One law's votes, streamed as CSV.

    Shared with the administration, which offers this same file for a law that
    has been archived — the last chance to keep the data before a purge destroys
    it, and a law the public route refuses by design. The reductions applied to
    the rows are the same either way: an archived votation is withdrawn from the
    site, not exempted from the anonymity rules.
    """
    # Counted once and handed down, as the JSON route already did. Without it
    # `iter_export_rows` runs its own COUNT to decide whether the time column may
    # be published at all.
    total = results_crud.count_votes(db, law.id)

    def rows() -> Iterator[str]:
        # The slot is taken here rather than in the route, and given back by the
        # `with`: a slot taken before the response exists would leak whenever a
        # client disconnected before reading, and a leaked slot never comes back.
        with ratelimit.export_slot():
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=["law_id", "choice", "created_at"])
            writer.writeheader()
            for row in results_crud.iter_export_rows(db, law.id, total=total):
                writer.writerow(row)
                # Handed over a bufferful at a time, so a large votation never
                # holds the whole export in memory — and does not pay a thread
                # hop per vote to say so. See `_CHUNK_CHARS`.
                if buf.tell() >= _CHUNK_CHARS:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
            tail = buf.getvalue()
            if tail:
                yield tail

    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="votations-{law.slug}.csv"'},
    )


@router.get("/loi/{slug}/export.csv")
def export_csv(request: Request, slug: str, db: Session = Depends(get_db)):
    # Refused *before* the law is looked up. The limits exist for a caller in a
    # loop, and a loop is exactly where doing an indexed read per refusal is
    # wasted work — the refusal does not need to know whether the slug is real.
    # The consequence is that a request for a law that does not exist now spends
    # allowance too, which is the right way round: a scanner walking export URLs
    # is precisely what should meet the limit.
    refused = _export_refused(request)
    if refused is not None:
        return refused
    law = visible(db, slug)
    if law is None:
        return not_found(request)
    return csv_response(db, law)


@router.get("/loi/{slug}/export.json")
def export_json(request: Request, slug: str, db: Session = Depends(get_db)):
    refused = _export_refused(request)  # see the CSV route for why this comes first
    if refused is not None:
        return refused
    law = visible(db, slug)
    if law is None:
        return not_found(request)

    # One pass over the votes answers both the total and how many carried a
    # profile; the two used to be a COUNT and a COUNT … FILTER over the same rows.
    tally = results_crud.tally(db, law.id)
    total = tally.total

    # Demographic aggregates are included only where they clear cell suppression.
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
        for bd in demo_crud.all_breakdowns(db, law)
        if bd.shown
    ]
    provision = demo_crud.provision_rate(db, law.id, tally=tally)
    envelope = {
        "law": {"id": law.id, "slug": law.slug, "title": law.title},
        "demographics": demographics,
        "profile_provision": {
            "total_votes": provision.total_votes,
            "with_profile": provision.with_profile,
            "percentage": provision.percentage,
        },
        "votes_note": results_crud.export_note(db, law.id, total=total),
        # Dated for the same reason the page is: while the votation is open
        # these are a snapshot, and a file that did not say so would invite
        # exactly the differencing the snapshot exists to stop — this endpoint
        # serves the same numbers, in a form far easier to poll.
        "demographics_as_of": (
            law.demographics_snapshot_at.isoformat()
            if law.is_open() and law.demographics_snapshot_at
            else None
        ),
        "demographics_note": (
            "Répartitions agrégées uniquement, masquées sous le seuil "
            "d'anonymat. Aucune ligne par votant·e. 'reference_pct' = ordre "
            "de grandeur national INSEE (approx.), jamais utilisé pour "
            "repondérer. Tant que la votation est ouverte, ces répartitions "
            "sont arrêtées à la date 'demographics_as_of' et n'avancent que "
            "par paliers : les publier en direct permettrait de déduire le "
            "profil d'une personne en comparant deux téléchargements."
        ),
    }

    def document() -> Iterator[str]:
        """Stream the envelope with the vote rows written one at a time.

        The whole set used to be materialised into a list and then serialised —
        two full copies of every vote in memory at once, on an endpoint anyone
        can request. The rows are the only unbounded part, so they are the part
        that streams — a bufferful at a time, for the reason given at
        :data:`_CHUNK_CHARS`.
        """
        with ratelimit.export_slot():  # see the CSV route for why it is here
            head = json.dumps(envelope, ensure_ascii=False)
            # Splice the vote array into the object rather than building a second one.
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
            # The closing brackets ride along with whatever is left, so the last
            # rows and the end of the document are one piece rather than two.
            pending.append("]}")
            yield "".join(pending)

    return StreamingResponse(
        document(),
        media_type="application/json; charset=utf-8",
        # `law.slug`, not the path parameter — same reason the CSV route above and
        # `laws._back_to` use the column: a slug is what the database holds, not
        # what the caller typed. The two are the same string today because
        # `visible` matched on it, and this value ends up in a response header.
        headers={"Content-Disposition": f'attachment; filename="votations-{law.slug}.json"'},
    )
