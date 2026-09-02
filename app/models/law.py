"""The Law model — a piece of legislation submitted to a citizens' vote.

Presentation follows the strict neutral 4-block structure mandated by the
project charter: what the law says, what it changes in practice, arguments for,
arguments against. Arguments are stored as lists of {text, source, author} so
that every claim is sourced and attributed, and symmetry can be enforced.
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
    __table_args__ = (
        Index("ix_laws_fts", text(SEARCH_VECTOR_SQL), postgresql_using="gin"),
        # Every public listing asks the same question — "the newest published,
        # non-withdrawn laws" — and answered it by reading the whole table and
        # sorting it. This index *is* that question: the partial clause is the
        # visibility rule, and the key is `_NEWEST_LAW_FIRST` spelled the same way
        # round, so the planner walks it in order and stops at the LIMIT instead
        # of sorting everything first.
        #
        # Measured at 50 000 laws: the home page's « ouvertes » band went from
        # 26.8 ms to 0.3 ms, and the band that matches nothing correctly falls
        # back to a sequential scan rather than walking the whole index.
        #
        # It costs writes only on rows that are published and not archived, which
        # is a table of hundreds written by hand a few times a day.
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
    #: Indexed, together with `created_at` and the visibility rule, by
    #: `ix_laws_home` above — which is the only shape the planner will use here.
    #: A plain index on this column alone was skipped, because the listings sort
    #: on two columns and filter on two more.
    promulgated_on: Mapped[date | None] = mapped_column(Date, default=None)

    # 4-block neutral content.
    summary_text: Mapped[str] = mapped_column(Text, default="")
    practical_changes: Mapped[str] = mapped_column(Text, default="")
    # Each entry: {"text": str, "source": str, "author": str}
    arguments_for: Mapped[list] = mapped_column(JSON, default=list)
    arguments_against: Mapped[list] = mapped_column(JSON, default=list)

    #: What the debate turned on, in one or two sentences. Shown in the encart at
    #: the top of the law page, above everything else.
    #:
    #: **The one place this platform writes about the debate rather than quoting
    #: it**, and the charter had to be widened on purpose to allow it. What made
    #: that acceptable is a rule of formulation, not a rule of process: this names
    #: the *subject* of the disagreement and never a position on it. « Le débat a
    #: porté sur le contrôle judiciaire des techniques de renseignement » is a
    #: fact about what was argued; « cette loi pose un problème de contrôle
    #: judiciaire » is one side's framing, and belongs to nobody here.
    #:
    #: Drafted from the arguments already selected — which are verbatim, sourced
    #: and symmetric — rather than freely from the sitting records, so a reader
    #: can check the sentence against the quotations further down the same page.
    #: Tracked like every other published field: an edit after publication shows
    #: up in the public record of changes.
    #:
    #: Short by construction. `String` rather than `Text` so the width is declared
    #: where the admin form reads it (see `app.routes.admin.forms._column_limits`),
    #: and 600 characters is about three sentences — past which it has stopped
    #: being a summary.
    debate_summary: Mapped[str] = mapped_column(String(600), default="", server_default="")


    #: How the Assemblée nationale settled the text, and whether the two chambers
    #: needed help agreeing. Facts published by Parliament, not presentation: they
    #: are copied from the staged row when the votation is created and are not
    #: editable in the form, because an administrator correcting a parliamentary
    #: vote count is not a thing this platform should make easy.
    #:
    #: Null throughout when no public scrutin was held - a third of promulgated
    #: laws, adopted without a recorded count. The page says so rather than
    #: leaving a gap, because « adoptée sans scrutin public » is information too.
    #:
    #: Only the Assemblée: the Sénat publishes its scrutins elsewhere.
    an_vote_for: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_against: Mapped[int | None] = mapped_column(Integer, default=None)
    an_vote_abstain: Mapped[int | None] = mapped_column(Integer, default=None)
    cmp_convened: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    #: Whether the scrutins export has actually been consulted for this text.
    #:
    #: The column exists because two very different situations produced the same
    #: empty counts, and the page stated the wrong one. « Adoptée sans scrutin
    #: public » is true of a third of promulgated laws and is worth saying; it is
    #: also what the page said about every law that predated this feature, which
    #: nobody had looked up. A claim about how Parliament voted is not a claim to
    #: make from an absence of data.
    #:
    #: So there are three states, not two: counts (say them), checked and none
    #: (say there was no scrutin), unchecked (say nothing at all).
    an_vote_checked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

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

    #: When this law's published demographic breakdown was last moved forward,
    #: and how many profiled votes it had been given at that moment.
    #:
    #: The breakdown shown while a vote is open is a *snapshot*, not the live
    #: counters — see `app.models.demographics.DemographicTally.published_count`
    #: for what that defends against. These two columns are the whole of the
    #: bookkeeping: the first bounds how often the snapshot may move, the second
    #: bounds how little it may move by, and together they are what lets the page
    #: say « arrêtées au … » with a number behind it.
    #:
    #: Null and zero on a law nobody has voted on yet, which reads as "nothing
    #: published so far" and is the state every law starts in.
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
