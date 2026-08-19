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
DIMENSION_REGION = "region"


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


class Region(enum.StrEnum):
    # Ordered by population (largest first) — used as display order too.
    IDF = "Île-de-France"
    ARA = "Auvergne-Rhône-Alpes"
    HDF = "Hauts-de-France"
    NAQ = "Nouvelle-Aquitaine"
    OCC = "Occitanie"
    GES = "Grand Est"
    PACA = "Provence-Alpes-Côte d'Azur"
    PDL = "Pays de la Loire"
    BRE = "Bretagne"
    NOR = "Normandie"
    BFC = "Bourgogne-Franche-Comté"
    CVL = "Centre-Val de Loire"
    COR = "Corse"
    OM = "Outre-mer"


# Canonical display order per dimension.
DIMENSION_ORDER = {
    DIMENSION_AGE: [b.value for b in AgeBracket],
    DIMENSION_SEXE: [s.value for s in Sexe],
    DIMENSION_REGION: [r.value for r in Region],
}

# Allowed (dimension, category) pairs, used to validate incoming form values.
VALID_CATEGORIES = {
    DIMENSION_AGE: {b.value for b in AgeBracket},
    DIMENSION_SEXE: {s.value for s in Sexe},
    DIMENSION_REGION: {r.value for r in Region},
}

# Approximate national reference shares (%), used only to show how far the
# self-selected sample diverges from the population — never to weight results.
# Sources: ordres de grandeur INSEE (population adulte pour l'âge, population
# totale pour le sexe et la région). Volontairement approximatifs.
INSEE_REFERENCE = {
    # Fallback only — kept close to the INSEE-derived adult shares (18+).
    DIMENSION_AGE: {
        "18-24": 10.2,
        "25-34": 14.6,
        "35-49": 23.5,
        "50-64": 24.0,
        "65+": 27.8,
    },
    DIMENSION_SEXE: {
        "femme": 51.5,
        "homme": 48.5,
        # "autre" n'est pas mesuré par l'INSEE → pas de référence.
    },
    DIMENSION_REGION: {
        "Île-de-France": 18.3,
        "Auvergne-Rhône-Alpes": 12.1,
        "Hauts-de-France": 9.0,
        "Nouvelle-Aquitaine": 9.0,
        "Occitanie": 9.0,
        "Grand Est": 8.2,
        "Provence-Alpes-Côte d'Azur": 7.6,
        "Pays de la Loire": 5.8,
        "Bretagne": 5.1,
        "Normandie": 4.9,
        "Bourgogne-Franche-Comté": 4.2,
        "Centre-Val de Loire": 3.8,
        "Corse": 0.5,
        "Outre-mer": 3.3,
    },
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
