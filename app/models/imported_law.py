"""Laws discovered automatically in the Assemblée nationale open data.

A staging area, not a publication surface. ``scripts/refresh_assemblee.py`` fills
this table daily with every promulgated law found in the official export; an
administrator then reviews each row and, for the ones worth submitting to a vote,
creates the corresponding :class:`~app.models.law.Law` by hand. Nothing here is
ever visible publicly, and no row becomes a law without that human step — the
neutral 4-block presentation is written and validated by a person, as the project
charter requires.

Contains only officially published data (law numbers, titles, JO references) —
no personal data.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow

# Provenance of a row. Only the Assemblée nationale export exists today; the
# column is the seam for a later Légifrance/JORF source keyed on the same NOR.
SOURCE_ASSEMBLEE = "assemblee"

# Review state. A row starts as "nouveau"; the admin either turns it into a law
# ("importe", with law_id set) or sets it aside ("ignore").
STATUS_NEW = "nouveau"
STATUS_IMPORTED = "importe"
STATUS_IGNORED = "ignore"

# Lifecycle of the AI draft, which runs in the background because it downloads
# Journal Officiel archives and calls a model — far longer than a request.
DRAFT_IDLE = ""
DRAFT_PENDING = "en_cours"
DRAFT_READY = "pret"
DRAFT_FAILED = "echec"


class ImportedLaw(Base):
    __tablename__ = "imported_laws"
    __table_args__ = (UniqueConstraint("source", "dossier_uid", name="uq_imported_law_source_uid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_ASSEMBLEE)
    dossier_uid: Mapped[str] = mapped_column(String(32), index=True)

    legislature: Mapped[str] = mapped_column(String(8), default="")
    procedure: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(1000))
    law_title: Mapped[str] = mapped_column(Text, default="")
    law_code: Mapped[str] = mapped_column(String(32))

    # Official publication references.
    promulgated_on: Mapped[date] = mapped_column(Date, index=True)
    jo_date: Mapped[date | None] = mapped_column(Date, default=None)
    jo_number: Mapped[str | None] = mapped_column(String(32), default=None)
    # NOR reference — the join key to Légifrance/JORF.
    nor: Mapped[str | None] = mapped_column(String(32), default=None)
    legifrance_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    dossier_url: Mapped[str] = mapped_column(String(1000), default="")
    # Sittings where the text was debated: [{"reunion_ref", "odj_ref", "date"}].
    # The join key to the comptes rendus, for sourcing attributed arguments.
    seances: Mapped[list] = mapped_column(JSON, default=list)

    # Review state, owned by the admin and never overwritten by the import job.
    status: Mapped[str] = mapped_column(String(16), default=STATUS_NEW)
    law_id: Mapped[int | None] = mapped_column(
        ForeignKey("laws.id", ondelete="SET NULL"), default=None
    )

    # The last AI draft produced for this law, kept so reopening it costs nothing:
    # {summary_text, practical_changes, arguments_for, arguments_against, notes, eli_url}.
    # Generating is billable, so it happens once unless explicitly regenerated.
    draft: Mapped[dict | None] = mapped_column(JSON, default=None)
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # "" | "en_cours" | "pret" | "echec" — drives the polling banner in the admin.
    draft_status: Mapped[str] = mapped_column(String(16), default=DRAFT_IDLE)
    draft_error: Mapped[str | None] = mapped_column(Text, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
