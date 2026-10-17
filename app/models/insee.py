"""Cached INSEE population shares, used to show how the sample differs.

Refreshed yearly by ``scripts/refresh_insee.py``. Falls back to the
``INSEE_REFERENCE`` constant when empty. Never used to reweight results.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class InseeReference(Base):
    __tablename__ = "insee_reference"
    __table_args__ = (UniqueConstraint("dimension", "category", name="uq_insee_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dimension: Mapped[str] = mapped_column(String(16))  # "sexe", "region", ...
    category: Mapped[str] = mapped_column(String(64))
    percentage: Mapped[float] = mapped_column(Float)
    source_year: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
