"""Laws found in the Assemblée nationale open data, awaiting review.

``scripts/refresh_assemblee.py`` fills this table daily. An admin reviews each
row and creates a `Law` for those worth a vote, writing the presentation by
hand. Nothing here is public. Only official data, no personal data.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
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

# Row origin. Only the Assemblée nationale for now.
SOURCE_ASSEMBLEE = "assemblee"

# Review status: "nouveau", then "importe" (law_id set) or "ignore".
STATUS_NEW = "nouveau"
STATUS_IMPORTED = "importe"
STATUS_IGNORED = "ignore"

# AI draft status. Drafting runs in the background (JO downloads + model call).
DRAFT_IDLE = ""
DRAFT_PENDING = "en_cours"
DRAFT_READY = "pret"
DRAFT_FAILED = "echec"


class ImportedLaw(Base):
    __tablename__ = "imported_laws"
    __table_args__ = (
        UniqueConstraint("source", "dossier_uid", name="uq_imported_law_source_uid"),
        # One dossier per law, enforced in the database since `scripts.content`
        # can also set law_id. NULLs don't collide, so rows awaiting review are
        # fine. Also serves as the index for `get_by_law_id`.
        UniqueConstraint("law_id", name="uq_imported_law_law_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_ASSEMBLEE)
    # No separate index: lookups go through uq_imported_law_source_uid.
    dossier_uid: Mapped[str] = mapped_column(String(32))

    legislature: Mapped[str] = mapped_column(String(8), default="")
    procedure: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(1000))
    law_code: Mapped[str] = mapped_column(String(32))

    # Official publication references.
    promulgated_on: Mapped[date] = mapped_column(Date, index=True)
    jo_date: Mapped[date | None] = mapped_column(Date, default=None)
    jo_number: Mapped[str | None] = mapped_column(String(32), default=None)
    # NOR, the key to Légifrance/JORF.
    nor: Mapped[str | None] = mapped_column(String(32), default=None)
    legifrance_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    dossier_url: Mapped[str] = mapped_column(String(1000), default="")
    # Sittings where the text was debated: [{"reunion_ref", "odj_ref", "date"}].
    # Used to find quotes in the comptes rendus.
    seances: Mapped[list] = mapped_column(JSON, default=list)

    # Assemblée vote and CMP, copied onto the law (see PARLIAMENT_RECORD).
    an_vote_for: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_against: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_abstain: Mapped[int | None] = mapped_column(Integer, default=None)
    cmp_convened: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Whether the scrutins export was checked (see Law.an_vote_checked).
    an_vote_checked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Set by the admin, never by the import. Indexed for the admin badge count.
    status: Mapped[str] = mapped_column(String(16), default=STATUS_NEW, index=True)
    law_id: Mapped[int | None] = mapped_column(
        ForeignKey("laws.id", ondelete="SET NULL"), default=None
    )

    # Last AI draft, kept because generating one costs money:
    # {summary_text, practical_changes, arguments_for, arguments_against, notes, eli_url}.
    draft: Mapped[dict | None] = mapped_column(JSON, default=None)
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # One of the DRAFT_* values. Drives the polling banner in the admin.
    draft_status: Mapped[str] = mapped_column(String(16), default=DRAFT_IDLE)
    draft_error: Mapped[str | None] = mapped_column(Text, default=None)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Indexed: the dashboard reads max() to spot a stalled import.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    @property
    def is_imported(self) -> bool:
        """Whether a law has been created from this dossier."""
        return self.status == STATUS_IMPORTED

    @property
    def is_ignored(self) -> bool:
        """Whether the admin set this dossier aside."""
        return self.status == STATUS_IGNORED
