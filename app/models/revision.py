"""The public record of every change made to a law after it was published.

The charter's first principle is radical transparency, and the sharpest test of
it is this: an administrator can rewrite « Ce que dit la loi », swap an argument,
or move the closing date while people are voting. Without a record, the published
result would describe a text nobody voted on, and no reader could tell.

So every field change on a *published* law is written here and shown on the
results page. Edits to an unpublished draft are not recorded — nobody has voted
on a draft, and logging the ordinary act of writing a presentation would bury the
changes that actually matter.

Contains no personal data: the values are the public presentation of a law. The
administrator is not identified either — the provisional guard is a single shared
credential pair, so naming it would imply an accountability the MVP cannot back.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class LawRevision(Base):
    __tablename__ = "law_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"), index=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    # The column that changed, e.g. "summary_text". Rendered through
    # `app.labels.field_label` so no English column name reaches a French page.
    field: Mapped[str] = mapped_column(String(32))

    # Both sides, rendered as text. Lists (the argument blocks) are stored in
    # their JSON form: the point is that a reader can see *what* changed, and the
    # stored text is enough for that.
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
