"""The Vote model.

A vote stores the law, the choice, an anonymous hashed token (one vote per
voter per law) and a timestamp. No IP, no user agent, no personal data.
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
        UniqueConstraint("law_id", "voter_token", name="uq_vote_law_voter"),
        # Covering index: the tally, the evolution curve and the export can be
        # answered from the index alone, without visiting the table.
        Index(
            "ix_votes_law_choice",
            "law_id",
            "choice",
            postgresql_include=["created_at", "demographics_recorded"],
        ),
        # Partial index for `count_profiled`, called on every ballot until the
        # first snapshot. Keep the predicate as the bare column (Postgres won't
        # match `IS true`), identical to the migration: Alembic doesn't compare
        # index predicates.
        Index(
            "ix_votes_law_profiled",
            "law_id",
            postgresql_where=text("demographics_recorded"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # No separate index: ix_votes_law_choice starts with this column.
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"))
    choice: Mapped[str] = mapped_column(String(16))

    # Anonymous hashed token. Covered by uq_vote_law_voter.
    voter_token: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # True once the voter's optional profile has been added to the counters.
    # The profile itself isn't stored, nor is any previous choice.
    demographics_recorded: Mapped[bool] = mapped_column(Boolean, default=False)
