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
    __table_args__ = (
        UniqueConstraint("source", "dossier_uid", name="uq_imported_law_source_uid"),
        # One dossier, one votation — as a fact about the table rather than as a
        # check on one route.
        #
        # `create_law` already refuses to build a second law from a staged row
        # that has one, and that guard is the right place to *explain* the rule to
        # an administrator. It is not the only way in: `scripts.content` re-applies
        # the review decisions carried in a snapshot, and nothing there stopped two
        # dossiers claiming the same law. `get_by_law_id` would then return
        # whichever row the planner handed back first — silently, since
        # `Session.scalar` does not object to a second row — and the AI draft
        # shown on a law's form could have belonged to another dossier.
        #
        # A plain unique constraint rather than a partial index, because SQL
        # treats NULLs as distinct: every staged row still awaiting a decision
        # carries NULL here and none of them collide. It also gives `law_id` the
        # btree index it never had, which is what `get_by_law_id` reads on every
        # visit to a law's form and what the `ON DELETE SET NULL` walks on a purge.
        UniqueConstraint("law_id", name="uq_imported_law_law_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_ASSEMBLEE)
    # Not indexed on its own: every query that looks a dossier up gives the source
    # as well, which is what `uq_imported_law_source_uid` above is keyed on. The
    # standalone index was never chosen by the planner.
    dossier_uid: Mapped[str] = mapped_column(String(32))

    legislature: Mapped[str] = mapped_column(String(8), default="")
    procedure: Mapped[str] = mapped_column(String(100), default="")
    title: Mapped[str] = mapped_column(String(1000))
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

    #: How the Assemblée settled it, and whether a commission mixte paritaire was
    #: convened. Refreshed by the daily job like the other factual columns, and
    #: copied onto the votation when an administrator creates one.
    an_vote_for: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_against: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_abstain: Mapped[int | None] = mapped_column(Integer, default=None)
    cmp_convened: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Review state, owned by the admin and never overwritten by the import job.
    # Indexed: every admin page counts the rows still awaiting a decision.
    status: Mapped[str] = mapped_column(String(16), default=STATUS_NEW, index=True)
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
    # Indexed: the dashboard takes max() of this on every render to decide
    # whether the automatic import has gone quiet.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    @property
    def is_imported(self) -> bool:
        """Whether this dossier has already been turned into a votation.

        Here rather than as ``status == 'importe'`` in the template that branches
        on it, for the same reason :attr:`~app.models.law.Law.is_public` exists:
        the spelling of the stored value belongs to this module, and a page that
        repeats it is a copy that no rename would follow.
        """
        return self.status == STATUS_IMPORTED

    @property
    def is_ignored(self) -> bool:
        """Whether an administrator has set this dossier aside.

        Here for the same reason as :attr:`is_imported`, and it was noticeable
        that only one of the pair existed: the imports table asked this one by
        writing « ignore » out again, two lines below a branch that asked the
        other one properly.
        """
        return self.status == STATUS_IGNORED
