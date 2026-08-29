"""The Law model — a piece of legislation submitted to a citizens' vote.

Presentation follows the strict neutral 4-block structure mandated by the
project charter: what the law says, what it changes in practice, arguments for,
arguments against. Arguments are stored as lists of {text, source, author} so
that every claim is sourced and attributed, and symmetry can be enforced.
"""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, query_expression

from app.db import Base, as_utc, utcnow

# Derived publication states. Stored nowhere: always computed from `published`
# and the voting window, so a law opens and closes on the clock alone.
# What the public search matches on. One template rendered two ways so the index
# and the query can never drift apart: CREATE INDEX reads the columns unqualified,
# while a query qualifies them to stay unambiguous if a join is ever added.
# Postgres matches expression indexes on the parsed tree, so both forms hit it.
#
# `coalesce` and `||` rather than `concat_ws`: an index expression must be
# IMMUTABLE, and `concat_ws` is not. The two-argument `to_tsvector` is — the
# one-argument form reads a session setting and would not be.
_SEARCH_BODY = "coalesce({p}title, '') || ' ' || coalesce({p}summary_text, '')"
SEARCH_VECTOR_SQL = f"to_tsvector('french'::regconfig, {_SEARCH_BODY.format(p='')})"
SEARCH_VECTOR_QUERY_SQL = f"to_tsvector('french'::regconfig, {_SEARCH_BODY.format(p='laws.')})"

STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


class Law(Base):
    __tablename__ = "laws"
    __table_args__ = (Index("ix_laws_fts", text(SEARCH_VECTOR_SQL), postgresql_using="gin"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))

    # Official source (Journal Officiel / Légifrance).
    official_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    legifrance_url: Mapped[str | None] = mapped_column(String(1000), default=None)

    #: The day Parliament's text became law, and the order the home page reads in.
    #:
    #: It was already on the page before it was a column — inside `official_ref`,
    #: as prose: « LOI n° 2026-532 du 24 juin 2026 ». A date that exists only
    #: inside a display string can be shown and never sorted on, which is how the
    #: home page came to be ordered by `created_at`: not a fact about the law, nor
    #: about the vote, but the moment an administrator happened to press save.
    #:
    #: Nullable because it has to be. A law written by hand has no staged row to
    #: take it from, and the charter allows one. Every query that orders on this
    #: therefore says NULLS LAST — Postgres would otherwise put the undated ones
    #: first, and a hand-written law would open the site.
    #:
    #: Not indexed: this table holds hundreds of rows, the home page reads fifty
    #: of them, and an index that the planner would skip is a cost on every write.
    promulgated_on: Mapped[date | None] = mapped_column(Date, default=None)

    # 4-block neutral content.
    summary_text: Mapped[str] = mapped_column(Text, default="")
    practical_changes: Mapped[str] = mapped_column(Text, default="")
    # Each entry: {"text": str, "source": str, "author": str}
    arguments_for: Mapped[list] = mapped_column(JSON, default=list)
    arguments_against: Mapped[list] = mapped_column(JSON, default=list)

    # Scheduling & publication.
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Set when a law is withdrawn from the site. Archiving replaces deletion:
    # removing a law used to cascade away every vote cast on it, destroying
    # published open data with one click. An archived law disappears from every
    # public page but keeps its votes, and can be restored. Purging one for good
    # is a separate, explicit act — see `app.crud.laws.purge`.
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    #: The first few hundred characters of `summary_text`, cut by the database.
    #:
    #: Not a column and not stored: it is filled in only by the listing that
    #: renders an excerpt, through ``with_expression`` — see
    #: :func:`app.crud.laws.list_published`. Everywhere else it is None, which is
    #: the point. The home page shows 200 characters of a summary whose column has
    #: no width limit and whose form accepts 20 000, so reading the whole thing
    #: for every card meant the cost of that page was set by the longest text
    #: anybody had ever pasted.
    excerpt: Mapped[str | None] = query_expression()

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_public(self) -> bool:
        """Whether this law may be shown, voted on, or exported at all."""
        return self.published and not self.is_archived

    def status(self, now: datetime | None = None) -> str:
        """Derive status from publication flag and the voting window."""
        if not self.published:
            return STATUS_DRAFT
        return self.scheduled_status(now)

    def scheduled_status(self, now: datetime | None = None) -> str:
        """The status the voting window alone implies, ignoring publication.

        What an administrator needs in order to preview a draft: "how would this
        look once published" is a question about the window, not about the flag.
        """
        now = now or utcnow()
        if self.opens_at and now < as_utc(self.opens_at):
            return STATUS_SCHEDULED
        if self.closes_at and now >= as_utc(self.closes_at):
            return STATUS_CLOSED
        return STATUS_OPEN

    def is_open(self, now: datetime | None = None) -> bool:
        return self.status(now) == STATUS_OPEN
