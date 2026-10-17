"""Public log of changes made to a law after publication.

Shown on the results page, so readers can tell if the text changed while the
vote was open. Draft edits aren't recorded. The admin isn't identified: there
is a single shared admin account.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, desc
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class LawRevision(Base):
    __tablename__ = "law_revisions"
    __table_args__ = (
        # Matches the history page query: one law, newest first.
        Index(
            "ix_law_revisions_law_changed",
            "law_id",
            desc("changed_at"),
            desc("id"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Column name, e.g. "summary_text". Displayed via `app.labels.field_label`.
    field: Mapped[str] = mapped_column(String(32))

    # Old and new values as text (JSON for the argument lists).
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
