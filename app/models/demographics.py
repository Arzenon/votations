"""Aggregate demographic tallies.

The privacy invariant of this table: **it never stores a token, an identity, or a
per-voter row.** It holds only counters — "for a given law, N people in this age
bracket voted FOR". A voter's demographics are added to these counters once, at
their first vote, and the originating event is never kept. There is therefore no
record anywhere that links a person to both their demographics and their vote, so
no Article 9 (sensitive) personal data is created server-side.

Each voter contributes to one *marginal* counter per dimension (age, sexe) — never
to a joint (age × sexe) cell — so the smallest value stored at rest stays as large
as possible, which strengthens the anonymity guarantee.
"""

import enum

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Dimension keys.
DIMENSION_AGE = "age"
DIMENSION_SEXE = "sexe"


class AgeBracket(enum.StrEnum):
    A18_24 = "18-24"
    A25_34 = "25-34"
    A35_49 = "35-49"
    A50_64 = "50-64"
    A65_PLUS = "65+"


class Sexe(enum.StrEnum):
    FEMME = "femme"
    HOMME = "homme"
    AUTRE = "autre"


# Canonical display order per dimension.
DIMENSION_ORDER = {
    DIMENSION_AGE: [b.value for b in AgeBracket],
    DIMENSION_SEXE: [s.value for s in Sexe],
}

# Allowed (dimension, category) pairs, used to validate incoming form values.
VALID_CATEGORIES = {
    DIMENSION_AGE: {b.value for b in AgeBracket},
    DIMENSION_SEXE: {s.value for s in Sexe},
}


class DemographicTally(Base):
    __tablename__ = "demographic_tallies"
    __table_args__ = (
        UniqueConstraint(
            "law_id", "dimension", "category", "choice", name="uq_demo_cell"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    law_id: Mapped[int] = mapped_column(
        ForeignKey("laws.id", ondelete="CASCADE"), index=True
    )
    dimension: Mapped[str] = mapped_column(String(16))  # "age" | "sexe"
    category: Mapped[str] = mapped_column(String(32))   # e.g. "35-49" | "femme"
    choice: Mapped[str] = mapped_column(String(16))     # for | against | abstain
    count: Mapped[int] = mapped_column(Integer, default=0)
