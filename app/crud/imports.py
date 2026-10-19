"""CRUD for laws staged by the Assemblée nationale import job.

The import job owns the factual columns; the admin owns ``status`` and
``law_id``. `sync` refreshes the former and never touches the latter, so an
ignored row stays ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, String, case, func, or_, select, update
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
    Law,
)

# The Assemblée's record of the vote. Not editable in the admin and not tracked
# in revisions. Everything that copies these columns reads this tuple.
PARLIAMENT_RECORD = (
    "an_vote_for",
    "an_vote_against",
    "an_vote_abstain",
    "cmp_convened",
    "an_vote_checked",
)

# Columns refreshed on every import. `status` and `law_id` are the admin's.
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
    *PARLIAMENT_RECORD,
)

# Read from the scrutins export, not the dossier: only replaced by a row whose
# scrutins were read (an_vote_checked), so a failed or partial download keeps
# the stored counts.
_FROM_SCRUTINS = frozenset({"an_vote_for", "an_vote_against", "an_vote_abstain", "an_vote_checked"})

log = get_logger("imports")


def _text_limits() -> dict[str, int]:
    """Max length of each String column, read from the model."""
    columns = ImportedLaw.__table__.c
    return {
        name: column.type.length
        for name, column in columns.items()
        if isinstance(column.type, String) and column.type.length is not None
    }


_TEXT_LIMITS = _text_limits()


def _too_long(row: dict) -> str | None:
    """The first field too long for its column, or None."""
    for name, limit in _TEXT_LIMITS.items():
        value = row.get(name)
        if isinstance(value, str) and len(value) > limit:
            return f"{name} ({len(value)} caractères, limite {limit})"
    return None


def sync(
    db: Session,
    *,
    records: list[PromulgatedLaw],
    source: str = SOURCE_ASSEMBLEE,
) -> tuple[int, int]:
    """Upsert the export into the staging table. Returns (created, refreshed).

    Everything goes in a single statement, so one oversized value would make
    Postgres reject the whole import. Such rows are skipped and logged instead.
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

    stmt = pg_insert(ImportedLaw)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_imported_law_source_uid",
        set_={
            **{
                name: (
                    case(
                        (stmt.excluded.an_vote_checked, stmt.excluded[name]),
                        else_=getattr(ImportedLaw, name),
                    )
                    if name in _FROM_SCRUTINS
                    else stmt.excluded[name]
                )
                for name in _REFRESHED
            },
            "last_seen_at": stmt.excluded.last_seen_at,
        },
    )
    db.execute(stmt, rows)
    db.commit()

    created = sum(1 for r in accepted if r.dossier_uid not in known)
    return created, len(accepted) - created


# Number of sittings, computed in SQL so the deferred `seances` column isn't
# loaded. json_array_length because the column is json, not jsonb.
_SEANCE_COUNT = func.coalesce(func.json_array_length(ImportedLaw.seances), 0)

# Sort orders offered in the admin (URL values).
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

    ``draft`` and ``seances`` are deferred because the list shows neither.
    Filtering by procedure helps clear out treaty ratifications; sorting by
    sitting count brings up the most debated laws.
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
    """Procedures present in the data with their counts, most common first.

    Counted with the same status filter as the listing.
    """
    stmt = select(ImportedLaw.procedure, func.count()).group_by(ImportedLaw.procedure)
    if status:
        stmt = stmt.where(ImportedLaw.status == status)
    rows = db.execute(stmt.order_by(func.count().desc(), ImportedLaw.procedure)).all()
    return [(procedure or "", count) for procedure, count in rows]


def seance_counts(db: Session, item_ids: list[int]) -> dict[int, int]:
    """Number of sittings for each of these staged laws, in one query."""
    if not item_ids:
        return {}
    rows = db.execute(
        select(ImportedLaw.id, _SEANCE_COUNT).where(ImportedLaw.id.in_(item_ids))
    ).all()
    return {item_id: count or 0 for item_id, count in rows}


def count_new(db: Session) -> int:
    """Staged laws awaiting a decision (admin badge)."""
    return (
        db.scalar(
            select(func.count()).select_from(ImportedLaw).where(ImportedLaw.status == STATUS_NEW)
        )
        or 0
    )


def last_import_at(db: Session) -> datetime | None:
    """When the import job last saw the export.

    Shown on the dashboard so a silently failing import gets noticed (the
    export URL changes with each legislature).
    """
    return db.scalar(select(func.max(ImportedLaw.last_seen_at)))


def propagate_parliament_record(db: Session) -> int:
    """Copy the latest vote record onto existing laws. Returns rows updated.

    The Assemblée can publish a scrutin after the law was created here, so this
    runs nightly. IS DISTINCT FROM skips unchanged rows, which matters because
    `laws.updated_at` feeds the sitemap's lastmod.
    """
    changed = or_(
        *(
            getattr(Law, name).is_distinct_from(getattr(ImportedLaw, name))
            for name in PARLIAMENT_RECORD
        )
    )
    # An ORM UPDATE returns a CursorResult, but execute() is typed as Result.
    result = cast(
        CursorResult,
        db.execute(
            update(Law)
            .where(ImportedLaw.law_id == Law.id, changed)
            .values({name: getattr(ImportedLaw, name) for name in PARLIAMENT_RECORD})
        ),
    )
    db.commit()
    return result.rowcount or 0


def get_by_id(db: Session, item_id: int) -> ImportedLaw | None:
    return db.get(ImportedLaw, item_id)


def get_by_law_id(db: Session, law_id: int) -> ImportedLaw | None:
    """The staged row a law was created from, if any (for the AI draft on edit)."""
    return db.scalar(select(ImportedLaw).where(ImportedLaw.law_id == law_id))


def set_status(db: Session, item: ImportedLaw, *, status: str) -> ImportedLaw:
    """Set the review status."""
    item.status = status
    db.commit()
    db.refresh(item)
    return item


def mark_draft_pending(db: Session, item: ImportedLaw) -> bool:
    """Claim this row for a draft run. Returns True if this caller got it.

    The check is in the UPDATE's WHERE so that two clicks can't both start a
    (paid) model call.
    """
    claimed = db.execute(
        update(ImportedLaw)
        .where(ImportedLaw.id == item.id, ImportedLaw.draft_status != DRAFT_PENDING)
        .values(draft_status=DRAFT_PENDING, draft_error=None)
        .returning(ImportedLaw.id)
    ).scalar_one_or_none()
    db.commit()
    # Refresh either way: the caller renders the current state.
    db.refresh(item)
    return claimed is not None


def save_draft(db: Session, item: ImportedLaw, *, draft: dict) -> ImportedLaw:
    """Store a generated draft so it isn't paid for twice."""
    item.draft = draft
    item.drafted_at = utcnow()
    item.draft_status = DRAFT_READY
    item.draft_error = None
    db.commit()
    db.refresh(item)
    return item


def fail_draft(db: Session, item: ImportedLaw, *, message: str) -> ImportedLaw:
    """Record why a draft run failed, for display on the form."""
    item.draft_status = DRAFT_FAILED
    item.draft_error = message[:2000]
    db.commit()
    db.refresh(item)
    return item


def clear_draft_state(db: Session, item: ImportedLaw) -> ImportedLaw:
    """Reset the draft status once its outcome has been shown."""
    item.draft_status = DRAFT_READY if item.draft else DRAFT_IDLE
    item.draft_error = None
    db.commit()
    db.refresh(item)
    return item


INTERRUPTED_MESSAGE = (
    "La génération a été interrompue par un redémarrage du service. "
    "Relancez-la, ou rédigez les blocs à la main."
)


def release_interrupted_drafts(db: Session) -> int:
    """Mark drafts still "running" as failed. Returns how many.

    A restart kills the drafting threads and leaves rows stuck in the pending
    state, which the UI can't get out of. Called once at startup, when nothing
    can be running yet.
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
