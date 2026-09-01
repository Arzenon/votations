"""CRUD for laws staged by the Assemblée nationale import job.

The import job owns the factual columns; the administrator owns ``status`` and
``law_id``. :func:`sync` therefore refreshes the former on every run and never
touches the latter, so a row set aside as "ignoré" stays ignored no matter how
many times the daily job sees it again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, String, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, defer

from app.assemblee import PromulgatedLaw
from app.crud import Page, count_matching, paginate
from app.db import utcnow
from app.logging_config import get_logger
from app.models import (
    DRAFT_FAILED,
    DRAFT_IDLE,
    DRAFT_PENDING,
    DRAFT_READY,
    SOURCE_ASSEMBLEE,
    STATUS_NEW,
    ImportedLaw,
)

# Columns the import job refreshes on every run. `status` and `law_id` are
# deliberately absent: they record the admin's decision, not the source data.
_REFRESHED = (
    "legislature",
    "procedure",
    "title",
    "law_code",
    "promulgated_on",
    "jo_date",
    "jo_number",
    "nor",
    "legifrance_url",
    "dossier_url",
    "seances",
)

# Sentinel telling `set_status` to leave `law_id` exactly as it is.
_KEEP = object()

log = get_logger("imports")


def _text_limits() -> dict[str, int]:
    """The declared width of every ``String`` column :func:`sync` writes.

    Read off the model rather than copied from it, for the same reason the admin
    form reads its own limits off ``laws``: a copy is correct exactly until
    somebody widens a column.
    """
    columns = ImportedLaw.__table__.c
    return {
        name: column.type.length
        for name, column in columns.items()
        if isinstance(column.type, String) and column.type.length is not None
    }


_TEXT_LIMITS = _text_limits()


def _too_long(row: dict) -> str | None:
    """The first field this row could not fit in its column, or None."""
    for name, limit in _TEXT_LIMITS.items():
        value = row.get(name)
        if isinstance(value, str) and len(value) > limit:
            return f"{name} ({len(value)} caractères, limite {limit})"
    return None


def sync(
    db: Session, *, records: list[PromulgatedLaw], source: str = SOURCE_ASSEMBLEE
) -> tuple[int, int]:
    """Upsert the export into the staging table. Returns (created, refreshed).

    Rows the columns cannot hold are skipped and logged rather than sent to the
    database. The whole export goes in one statement — which is what makes the
    daily job a single round trip — and that is exactly why a single over-long
    title could not be allowed through: Postgres refuses the *statement*, not the
    row, so one malformed dossier would have thrown away every good one with it.
    The job then exits non-zero and the container retries, so a value the source
    is not going to change on our account would have stopped the import for good.

    Skipping is the honest repair. The row is named in the logs, the other few
    hundred laws land, and an administrator can create that one by hand.
    """
    if not records:
        return 0, 0

    known = set(db.scalars(select(ImportedLaw.dossier_uid).where(ImportedLaw.source == source)))
    now = utcnow()

    rows = []
    accepted: list[PromulgatedLaw] = []
    for record in records:
        row = {
            "source": source,
            "dossier_uid": record.dossier_uid,
            "status": STATUS_NEW,
            "first_seen_at": now,
            "last_seen_at": now,
            **{name: getattr(record, name) for name in _REFRESHED},
        }
        oversized = _too_long(row)
        if oversized is not None:
            log.warning(
                "dossier %s ignoré : %s - loi à saisir à la main si elle doit être soumise au vote",
                record.dossier_uid,
                oversized,
            )
            continue
        rows.append(row)
        accepted.append(record)

    if not rows:
        return 0, 0

    # One statement for the whole export rather than one round trip per law.
    stmt = pg_insert(ImportedLaw)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_imported_law_source_uid",
        set_={
            **{name: stmt.excluded[name] for name in _REFRESHED},
            "last_seen_at": stmt.excluded.last_seen_at,
        },
    )
    db.execute(stmt, rows)
    db.commit()

    created = sum(1 for r in accepted if r.dossier_uid not in known)
    return created, len(accepted) - created


#: How many public sittings a staged law was debated in, computed in SQL.
#:
#: `seances` is a deferred JSON column and must stay that way — see
#: :func:`list_by_status` — so the count is asked of the database rather than
#: taken by loading the list into Python. `json_array_length`, not the `jsonb`
#: form: the column is `json`, and casting every row to `jsonb` to count its
#: entries would parse the very payload the deferral exists to avoid.
_SEANCE_COUNT = func.coalesce(func.json_array_length(ImportedLaw.seances), 0)

#: Sorts the administration offers. Keyed by the value in the URL so an unknown
#: one falls back rather than 500s.
ORDER_BY_DATE = "date"
ORDER_BY_SEANCES = "seances"


def list_by_status(
    db: Session,
    *,
    status: str | None = None,
    procedure: str | None = None,
    order: str = ORDER_BY_DATE,
    page: int = 1,
    page_size: int = 50,
) -> Page[ImportedLaw]:
    """Staged laws, most recently promulgated first.

    ``draft`` and ``seances`` are deferred: a stored draft is a whole AI
    presentation — two prose blocks and up to a dozen quotes — and the list shows
    neither. Fetching them for every row made the "tous" view megabytes of JSON to
    render a table of titles and dates.

    ``procedure`` and ``order`` exist for triage. Seventy staged laws is not a
    to-do list, it is a corpus to choose from: seventeen of them are treaty
    ratifications nobody would put to a vote, and the ones worth a votation are
    the ones that were argued over. Filtering by procedure collapses the first
    group into one screen to clear; ordering by sitting count surfaces the second.
    """
    stmt = select(ImportedLaw)
    if status:
        stmt = stmt.where(ImportedLaw.status == status)
    if procedure:
        stmt = stmt.where(ImportedLaw.procedure == procedure)

    total = count_matching(db, stmt)
    number, pages, offset = paginate(page, total, page_size)

    ordering: tuple[ColumnElement[Any], ...]
    if order == ORDER_BY_SEANCES:
        # Most debated first, then most recent — a stable order for equal counts.
        ordering = (_SEANCE_COUNT.desc(), ImportedLaw.promulgated_on.desc())
    else:
        ordering = (ImportedLaw.promulgated_on.desc(), ImportedLaw.law_code.desc())

    rows = list(
        db.scalars(
            stmt.options(defer(ImportedLaw.draft), defer(ImportedLaw.seances))
            .order_by(*ordering)
            .offset(offset)
            .limit(page_size)
        )
    )
    return Page(rows=rows, total=total, number=number, count=pages)


def procedures(db: Session, *, status: str | None = None) -> list[tuple[str, int]]:
    """Every procedure present, with how many rows carry it, commonest first.

    Read from the data rather than hard-coded: the labels come from the Assemblée
    nationale's export and are theirs to change. Counted under the same status
    filter as the listing, so the number beside each option is what choosing it
    will actually show.
    """
    stmt = select(ImportedLaw.procedure, func.count()).group_by(ImportedLaw.procedure)
    if status:
        stmt = stmt.where(ImportedLaw.status == status)
    rows = db.execute(stmt.order_by(func.count().desc(), ImportedLaw.procedure)).all()
    return [(procedure or "", count) for procedure, count in rows]


def seance_counts(db: Session, item_ids: list[int]) -> dict[int, int]:
    """How many public sittings each of these laws was debated in, in one query.

    Separate from :func:`list_by_status` so the listing keeps returning plain
    ``ImportedLaw`` rows and every caller of it stays unchanged — the same shape
    as ``results.tallies_for``.
    """
    if not item_ids:
        return {}
    rows = db.execute(
        select(ImportedLaw.id, _SEANCE_COUNT).where(ImportedLaw.id.in_(item_ids))
    ).all()
    return {item_id: count or 0 for item_id, count in rows}


def count_new(db: Session) -> int:
    """How many staged laws still await a decision (for the admin badge)."""
    return (
        db.scalar(
            select(func.count()).select_from(ImportedLaw).where(ImportedLaw.status == STATUS_NEW)
        )
        or 0
    )


def last_import_at(db: Session) -> datetime | None:
    """When the import job last saw the export.

    The job runs in its own container and retries forever on failure, so a source
    that moves — the export URL carries the legislature number and changes once
    per legislature — would otherwise stop the imports silently. The dashboard
    shows this so the silence is visible.
    """
    return db.scalar(select(func.max(ImportedLaw.last_seen_at)))


def get_by_id(db: Session, item_id: int) -> ImportedLaw | None:
    return db.get(ImportedLaw, item_id)


def get_by_law_id(db: Session, law_id: int) -> ImportedLaw | None:
    """The staged row a law was created from, if it came from one.

    The link is set once, when the law is created (see the admin's create
    route), and is what lets the edit form still offer the AI draft. Without it
    the whole drafting feature was reachable only from the creation form and
    disappeared the moment the law was saved — including the cached draft that
    had already been paid for.
    """
    return db.scalar(select(ImportedLaw).where(ImportedLaw.law_id == law_id))


def set_status(
    db: Session, item: ImportedLaw, *, status: str, law_id: int | object | None = _KEEP
) -> ImportedLaw:
    """Set the review state. ``law_id`` is left untouched unless given explicitly.

    Ignoring and then restoring a row used to clear its link to the law it had
    produced, leaving an "importée" row with nothing to point at.
    """
    item.status = status
    if law_id is not _KEEP:
        item.law_id = law_id  # type: ignore[assignment]
    db.commit()
    db.refresh(item)
    return item


def mark_draft_pending(db: Session, item: ImportedLaw) -> bool:
    """Claim this row for a background draft run. True if this caller won it.

    The claim is the ``WHERE``, not a read followed by a write. Checking the
    status in Python and then setting it let two administrators clicking in the
    same second — or one double-click, or a retried POST — each see a row at rest
    and each schedule a run. Both would then download the Journal Officiel
    archives and call the model, and the second would overwrite the first's
    draft: a billed request nobody asked for, on the one feature that costs money
    per press.

    Losing the claim is not an error. It means the run this caller wanted is
    already happening, which is what they wanted.
    """
    claimed = db.execute(
        update(ImportedLaw)
        .where(ImportedLaw.id == item.id, ImportedLaw.draft_status != DRAFT_PENDING)
        .values(draft_status=DRAFT_PENDING, draft_error=None)
        .returning(ImportedLaw.id)
    ).scalar_one_or_none()
    db.commit()
    # Refreshed either way: the caller renders the banner from this object, and
    # it must show the run that is happening whether or not this caller started it.
    db.refresh(item)
    return claimed is not None


def save_draft(db: Session, item: ImportedLaw, *, draft: dict) -> ImportedLaw:
    """Keep a generated draft so reopening it never bills a second time."""
    item.draft = draft
    item.drafted_at = utcnow()
    item.draft_status = DRAFT_READY
    item.draft_error = None
    db.commit()
    db.refresh(item)
    return item


def fail_draft(db: Session, item: ImportedLaw, *, message: str) -> ImportedLaw:
    """Record why a background draft run gave up, for display on the form."""
    item.draft_status = DRAFT_FAILED
    item.draft_error = message[:2000]
    db.commit()
    db.refresh(item)
    return item


def clear_draft_state(db: Session, item: ImportedLaw) -> ImportedLaw:
    """Return a row to rest once its outcome has been shown."""
    item.draft_status = DRAFT_READY if item.draft else DRAFT_IDLE
    item.draft_error = None
    db.commit()
    db.refresh(item)
    return item


#: Shown on a row whose draft the process was still running when it stopped.
INTERRUPTED_MESSAGE = (
    "La génération a été interrompue par un redémarrage du service. "
    "Relancez-la, ou rédigez les blocs à la main."
)


def release_interrupted_drafts(db: Session) -> int:
    """Fail every draft still marked as running. Returns how many were released.

    ``en_cours`` means "a thread in *this* process is working on it", and no
    thread survives the process. A redeploy — `shutdown_drafting` refuses what is
    queued and abandons what is already inside a download or a model call, which
    is the deliberate trade that lets the container stop at once — or a crash
    therefore leaves rows claiming a run that nobody is performing. Every exit
    from that state is closed from the outside: the banner polls for
    an outcome that will never be written, « masquer » only clears a *failed*
    draft, and « Régénérer » refuses to reschedule a row it believes is already
    running. The row was stuck for good, on the one screen whose whole job is to
    tell the administrator what happened.

    Called once at startup, before the first request, which is the only moment
    the answer is unambiguous: nothing can be running yet, so anything marked as
    running belongs to a process that is gone.
    """
    interrupted = list(
        db.scalars(select(ImportedLaw).where(ImportedLaw.draft_status == DRAFT_PENDING))
    )
    for item in interrupted:
        item.draft_status = DRAFT_FAILED
        item.draft_error = INTERRUPTED_MESSAGE
    if interrupted:
        db.commit()
    return len(interrupted)
