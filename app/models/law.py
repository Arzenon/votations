"""The Law model — a piece of legislation submitted to a citizens' vote.

Presentation follows the strict neutral 4-block structure mandated by the
project charter: what the law says, what it changes in practice, arguments for,
arguments against. Arguments are stored as lists of {text, source, author} so
that every claim is sourced and attributed, and symmetry can be enforced.
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow

# Derived publication states. Stored nowhere: always computed from `published`
# and the voting window, so a law opens and closes on the clock alone.
STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


class Law(Base):
    __tablename__ = "laws"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))

    # Official source (Journal Officiel / Légifrance).
    official_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    legifrance_url: Mapped[str | None] = mapped_column(String(1000), default=None)

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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def status(self, now: datetime | None = None) -> str:
        """Derive status from publication flag and the voting window."""
        now = now or utcnow()
        if not self.published:
            return STATUS_DRAFT
        if self.opens_at and now < _aware(self.opens_at):
            return STATUS_SCHEDULED
        if self.closes_at and now >= _aware(self.closes_at):
            return STATUS_CLOSED
        return STATUS_OPEN

    def is_open(self, now: datetime | None = None) -> bool:
        return self.status(now) == STATUS_OPEN


def _aware(dt: datetime) -> datetime:
    """Treat any naive datetime as UTC for safe comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
