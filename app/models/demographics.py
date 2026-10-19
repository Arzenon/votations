"""Aggregate demographic counters.

This table never stores a token, an identity or a per-voter row, only counts
like "for this law, N people aged 35-49 voted for". So nothing links a person
to both their profile and their vote. Each dimension is counted separately
(no age x sex cells), which keeps the stored counts as large as possible.
"""

import enum
import unicodedata
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
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
    # Listed by population; displayed alphabetically (DIMENSION_ORDER). The values
    # are stored in the counters and the export, so never change them.
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


# Display order and validation are derived from this mapping.
DIMENSION_ENUMS: dict[str, type[enum.StrEnum]] = {
    DIMENSION_AGE: AgeBracket,
    DIMENSION_SEXE: Sexe,
    DIMENSION_REGION: Region,
}

# Ordered dimension keys, as displayed.
DIMENSIONS = tuple(DIMENSION_ENUMS)

# Human labels, shown on the results page.
DIMENSION_LABELS = {
    DIMENSION_AGE: "Tranche d'âge",
    DIMENSION_SEXE: "Sexe",
    DIMENSION_REGION: "Région",
}


def _alphabetical(values) -> list[str]:
    """Sort ignoring accents, so "Île-de-France" comes under I."""

    def key(value: str) -> str:
        stripped = unicodedata.normalize("NFKD", value)
        return "".join(c for c in stripped if not unicodedata.combining(c)).lower()

    return sorted(values, key=key)


# Display order for the ballot dropdowns and the results tables. Regions are
# alphabetical, the others keep their declared order.
DIMENSION_ORDER = {
    DIMENSION_AGE: [m.value for m in AgeBracket],
    DIMENSION_SEXE: [m.value for m in Sexe],
    DIMENSION_REGION: _alphabetical(m.value for m in Region),
}

# Allowed categories per dimension, for form validation.
VALID_CATEGORIES = {dim: set(order) for dim, order in DIMENSION_ORDER.items()}

# Approximate national shares (%), only used to compare the sample with the
# population, never to weight results. Rough INSEE figures (adult population
# for age, total population for sex and region).
INSEE_REFERENCE = {
    # Fallback, close to the INSEE adult shares (18+).
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
        # INSEE doesn't measure "autre", so no reference.
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
        UniqueConstraint("law_id", "dimension", "category", "choice", name="uq_demo_cell"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # No separate index: uq_demo_cell starts with this column.
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"))
    dimension: Mapped[str] = mapped_column(String(16))  # "age", "sexe", "region"
    category: Mapped[str] = mapped_column(String(32))  # e.g. "35-49", "femme"
    choice: Mapped[str] = mapped_column(String(16))  # for, against, abstain

    # Live counter, updated by every ballot.
    count: Mapped[int] = mapped_column(Integer, default=0)

    # What the results page shows while the vote is open: `count` at the last
    # snapshot. Showing `count` live would let anyone diff two page loads and
    # recover a single voter's choice with their age, sex and region. See
    # `app.crud.demographics.refresh_snapshot`.
    published_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # When `published_count` was last copied. A held-back category keeps an
    # older date than the law's last snapshot.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
