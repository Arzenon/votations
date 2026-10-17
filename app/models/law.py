"""The Law model: a law submitted to the vote.

Presented in four blocks: what the law says, what it changes, arguments for,
arguments against. Arguments are lists of {text, source, author}.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    desc,
    nullslast,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, query_expression

from app.db import Base, as_utc, utcnow

# Full-text search expression, in two forms: unqualified for CREATE INDEX and
# qualified for queries. Postgres matches both against the same index.
# coalesce and || rather than concat_ws, which isn't IMMUTABLE. Same for the
# one-argument to_tsvector.
_SEARCH_BODY = "coalesce({p}title, '') || ' ' || coalesce({p}summary_text, '')"
SEARCH_VECTOR_SQL = f"to_tsvector('french'::regconfig, {_SEARCH_BODY.format(p='')})"
SEARCH_VECTOR_QUERY_SQL = f"to_tsvector('french'::regconfig, {_SEARCH_BODY.format(p='laws.')})"

# Derived states, never stored: computed from `published` and the voting window.
STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


class Law(Base):
    __tablename__ = "laws"
    __table_args__ = (
        Index("ix_laws_fts", text(SEARCH_VECTOR_SQL), postgresql_using="gin"),
        # Public listings: visibility rule as the predicate, `_NEWEST_LAW_FIRST`
        # as the key, so the planner can stop at the LIMIT without sorting.
        # 26.8 ms down to 0.3 ms at 50,000 laws.
        Index(
            "ix_laws_home",
            nullslast(desc("promulgated_on")),
            desc("created_at"),
            postgresql_where=text("published AND archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))

    # Official source (Journal Officiel / Légifrance).
    official_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    legifrance_url: Mapped[str | None] = mapped_column(String(1000), default=None)

    # Promulgation date, used to sort public listings. Null for laws entered by
    # hand, hence NULLS LAST everywhere. Indexed through ix_laws_home.
    promulgated_on: Mapped[date | None] = mapped_column(Date, default=None)

    # The four presentation blocks.
    summary_text: Mapped[str] = mapped_column(Text, default="")
    practical_changes: Mapped[str] = mapped_column(Text, default="")
    # Each entry: {"text": str, "source": str, "author": str}
    arguments_for: Mapped[list] = mapped_column(JSON, default=list)
    arguments_against: Mapped[list] = mapped_column(JSON, default=list)

    # One or two sentences on what the debate was about, shown at the top of the
    # law page. Names the subject of the disagreement, never a position. Written
    # from the selected arguments so readers can check it against them. Tracked
    # in revisions. 600 chars is about three sentences.
    debate_summary: Mapped[str] = mapped_column(String(600), default="", server_default="")

    # Assemblée nationale vote and whether a CMP was needed. Copied from the
    # staged row, not editable in the admin. Null when there was no public
    # scrutin (about a third of laws). The Sénat publishes its votes elsewhere.
    an_vote_for: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_against: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_abstain: Mapped[int | None] = mapped_column(Integer, default=None)
    cmp_convened: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Whether the scrutins export was checked for this law. Lets the page tell
    # "no public scrutin" apart from "not looked up", which it says nothing about.
    an_vote_checked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Scheduling and publication.
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Archiving hides a law from public pages but keeps its votes, and can be
    # undone. Permanent deletion is `app.crud.laws.purge`.
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    # When the published demographic snapshot last moved, and how many profiled
    # votes it included. See `app.crud.demographics.refresh_snapshot`.
    demographics_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    demographics_snapshot_profiles: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Start of `summary_text`, only loaded by `app.crud.laws.list_published`
    # (via with_expression). None elsewhere.
    excerpt: Mapped[str | None] = query_expression()

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def is_public(self) -> bool:
        """Whether this law can be shown, voted on or exported."""
        return self.published and not self.is_archived

    def status(self, now: datetime | None = None) -> str:
        """Status from the published flag and the voting window."""
        if not self.published:
            return STATUS_DRAFT
        return self.scheduled_status(now)

    def scheduled_status(self, now: datetime | None = None) -> str:
        """Status from the voting window only, ignoring `published` (for previews)."""
        now = now or utcnow()
        if self.opens_at and now < as_utc(self.opens_at):
            return STATUS_SCHEDULED
        if self.closes_at and now >= as_utc(self.closes_at):
            return STATUS_CLOSED
        return STATUS_OPEN

    def is_open(self, now: datetime | None = None) -> bool:
        return self.status(now) == STATUS_OPEN
