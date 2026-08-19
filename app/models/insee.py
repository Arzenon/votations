"""Cached INSEE reference shares (national population distribution).

Populated by an annual refresh job (``scripts/refresh_insee.py``) from the open
INSEE Melodi API. The app reads these values to show how far the self-selected
sample diverges from the population; if the table is empty (or a dimension is
missing) it falls back to the curated ``INSEE_REFERENCE`` constant. Reference
data only — never used to reweight results, never personal data.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class InseeReference(Base):
    __tablename__ = "insee_reference"
    __table_args__ = (UniqueConstraint("dimension", "category", name="uq_insee_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dimension: Mapped[str] = mapped_column(String(16))  # "sexe" | "region" | …
    category: Mapped[str] = mapped_column(String(64))
    percentage: Mapped[float] = mapped_column(Float)
    source_year: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
