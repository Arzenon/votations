"""CRUD for laws staged by the Assemblée nationale import job.

The import job owns the factual columns; the administrator owns ``status`` and
``law_id``. :func:`sync` therefore refreshes the former on every run and never
touches the latter, so a row set aside as "ignoré" stays ignored no matter how
many times the daily job sees it again.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.assemblee import PromulgatedLaw
from app.models import SOURCE_ASSEMBLEE, STATUS_NEW, ImportedLaw

# Columns the import job refreshes on every run. `status` and `law_id` are
# deliberately absent: they record the admin's decision, not the source data.
_REFRESHED = (
    "legislature",
    "procedure",
    "title",
    "law_title",
    "law_code",
    "promulgated_on",
    "jo_date",
    "jo_number",
    "nor",
    "legifrance_url",
    "dossier_url",
    "seances",
)


def sync(
    db: Session, *, records: list[PromulgatedLaw], source: str = SOURCE_ASSEMBLEE
) -> tuple[int, int]:
    """Upsert the export into the staging table. Returns (created, refreshed)."""
    if not records:
        return 0, 0

    known = set(
        db.scalars(select(ImportedLaw.dossier_uid).where(ImportedLaw.source == source))
    )
    now = datetime.now(UTC)

    for record in records:
        values = {
            "source": source,
            "dossier_uid": record.dossier_uid,
            "status": STATUS_NEW,
            "first_seen_at": now,
            "last_seen_at": now,
            **{name: getattr(record, name) for name in _REFRESHED},
        }
        stmt = pg_insert(ImportedLaw).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_imported_law_source_uid",
            set_={
                **{name: stmt.excluded[name] for name in _REFRESHED},
                "last_seen_at": stmt.excluded.last_seen_at,
            },
        )
        db.execute(stmt)
    db.commit()

    created = sum(1 for r in records if r.dossier_uid not in known)
    return created, len(records) - created


def list_by_status(db: Session, *, status: str | None = None) -> list[ImportedLaw]:
    """Staged laws, most recently promulgated first."""
    stmt = select(ImportedLaw)
    if status:
        stmt = stmt.where(ImportedLaw.status == status)
    stmt = stmt.order_by(ImportedLaw.promulgated_on.desc(), ImportedLaw.law_code.desc())
    return list(db.scalars(stmt))


def count_new(db: Session) -> int:
    """How many staged laws still await a decision (for the admin badge)."""
    return db.scalar(
        select(func.count()).select_from(ImportedLaw).where(ImportedLaw.status == STATUS_NEW)
    ) or 0


def get_by_id(db: Session, item_id: int) -> ImportedLaw | None:
    return db.get(ImportedLaw, item_id)


def set_status(
    db: Session, item: ImportedLaw, *, status: str, law_id: int | None = None
) -> ImportedLaw:
    item.status = status
    item.law_id = law_id
    db.commit()
    db.refresh(item)
    return item


def save_draft(db: Session, item: ImportedLaw, *, draft: dict) -> ImportedLaw:
    """Keep a generated draft so reopening it never bills a second time."""
    item.draft = draft
    item.drafted_at = datetime.now(UTC)
    db.commit()
    db.refresh(item)
    return item
