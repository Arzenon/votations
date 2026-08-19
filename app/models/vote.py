"""The Vote model.

Privacy by design: a vote stores only the law, the choice, an *anonymous*
hashed token (to enforce one vote per voter per law), and a timestamp.
No IP address, no user-agent, no personal data whatsoever.
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"), index=True)
    choice: Mapped[str] = mapped_column(String(16))

    # Anonymous, hashed token. Cannot be linked back to an identity. Only ever
    # looked up together with `law_id`, which uq_vote_law_voter already covers —
    # so this column carries no index of its own.
    voter_token: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # True once this voter's optional demographics have been folded into the
    # aggregate counters. Stores no demographic values, only the fact that the
    # one-time contribution happened.
    demographics_recorded: Mapped[bool] = mapped_column(Boolean, default=False)

    # Which choice this voter's demographics were counted under, so that changing
    # a vote can move the aggregate counters instead of leaving them stale. Holds
    # a choice, never a category: the profile itself is still stored nowhere.
    demographics_choice: Mapped[str | None] = mapped_column(String(16), default=None)
