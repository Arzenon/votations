"""The Vote model.

Privacy by design: a vote stores only the law, the choice, an *anonymous*
hashed token (to enforce one vote per voter per law), and a timestamp.
No IP address, no user-agent, no personal data whatsoever.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class VoteChoice(enum.StrEnum):
    FOR = "for"
    AGAINST = "against"
    ABSTAIN = "abstain"


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        # One vote per anonymous voter per law.
        UniqueConstraint("law_id", "voter_token", name="uq_vote_law_voter"),
        # Every read of this table asks the same shape of question: "for this one
        # law, group its votes". A plain index on `law_id` answered the *where*
        # and then sent Postgres to the heap for the columns — one visit per vote
        # cast, three times over on a results page.
        #
        # The two key columns are what the tally groups by; the payload columns
        # are the rest of what those queries project, carried in the index's own
        # leaves (`INCLUDE`) rather than made part of the key. That is enough for
        # the tally, the evolution curve and the open-data export to be answered
        # from the index alone, and it keeps the key narrow — an INCLUDE column
        # cannot be searched or sorted on, which is exactly what none of them
        # need to do with these two.
        Index(
            "ix_votes_law_choice",
            "law_id",
            "choice",
            postgresql_include=["created_at", "demographics_recorded"],
        ),
        # The one question asked on the *write* path, and the only one that is
        # about a small subset of a law's votes: how many of them carried a
        # profile. `app.crud.demographics.refresh_snapshot` asks it on every
        # ballot until the law takes its first snapshot — which needs
        # MIN_BUCKET_VOLUME profiled votes and, the profile being optional, may
        # never happen. The index above answers it by walking *every* vote on the
        # law and testing the included column, so a votation with a hundred
        # thousand ballots and forty profiles paid a hundred-thousand-entry scan
        # per ballot, on the busiest write in the application.
        #
        # This one holds only the profiled votes, so the count is proportional to
        # what it counts. It stays small by construction: the rows it indexes are
        # the minority who filled the optional fields in.
        #
        # **The predicate is the bare column**, and `count_profiled` must spell
        # its WHERE the same way — see the note there, and `laws._published` for
        # the same lesson learned the expensive way: Postgres uses a partial index
        # only where it can prove the query implies the predicate, and it cannot
        # prove that of `IS true`.
        #
        # Alembic compares an index on its columns and its uniqueness, never on
        # its `where` (checked against 1.19) — which is what lets `ix_laws_home`
        # be declared here too, and also means nothing would catch this predicate
        # drifting from the migration's. Keep the two spellings identical.
        Index(
            "ix_votes_law_profiled",
            "law_id",
            postgresql_where=text("demographics_recorded"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Not indexed on its own: `ix_votes_law_choice` above leads with this column,
    # so it serves both the per-law lookups and the ON DELETE CASCADE.
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"))
    choice: Mapped[str] = mapped_column(String(16))

    # Anonymous, hashed token. Cannot be linked back to an identity. Only ever
    # looked up together with `law_id`, which uq_vote_law_voter already covers —
    # so this column carries no index of its own.
    voter_token: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # True once this voter's optional demographics have been folded into the
    # aggregate counters. Stores no demographic values and no second choice, only
    # the fact that the one-time contribution happened.
    #
    # A `demographics_choice` column used to sit beside it, holding the choice the
    # contribution had been filed under so that changing a vote could move the
    # counters. It was redundant with `choice` except in one case — a browser that
    # had forgotten its profile cookie — and in exactly that case it turned the
    # row into a record of somebody having changed their mind, and of what they
    # had chosen before. `cast_vote` now reads the previous choice under a lock
    # and hands it straight to the counters, so nothing has to be kept.
    demographics_recorded: Mapped[bool] = mapped_column(Boolean, default=False)
