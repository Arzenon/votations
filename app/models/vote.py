"""The Vote model.

Privacy by design: a vote stores only the law, the choice, an *anonymous*
hashed token (to enforce one vote per voter per law), and a timestamp.
No IP address, no user-agent, no personal data whatsoever.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class VoteChoice(enum.StrEnum):
    FOR = "for"
    AGAINST = "against"
    ABSTAIN = "abstain"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        # One vote per anonymous voter per law.
        UniqueConstraint("law_id", "voter_token", name="uq_vote_law_voter"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"), index=True)
    choice: Mapped[str] = mapped_column(String(16))

    # Anonymous, hashed token. Cannot be linked back to an identity.
    voter_token: Mapped[str] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    law = relationship("Law")
