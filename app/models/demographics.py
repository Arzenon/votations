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
import unicodedata

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
    # Declared largest-population-first, which is only how this list is *written*.
    # What a visitor sees is alphabetical — see DIMENSION_ORDER below. The member
    # *values* are what lands in the counters and in the open-data export, so they
    # are the part that must never move.
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


# The single source of truth for the dimensions: everything else — display
# order, validation, labels — is derived from this mapping, so adding a
# dimension is one line and the three views can never disagree.
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
    """Sort French labels the way a French reader expects to find them.

    Sorting the raw strings would file « Île-de-France » after « Provence » —
    ``Î`` sits well past ``Z`` in code-point order — so the key strips diacritics
    first and the accented name lands under I, where someone looking for it will
    actually look.
    """

    def key(value: str) -> str:
        stripped = unicodedata.normalize("NFKD", value)
        return "".join(c for c in stripped if not unicodedata.combining(c)).lower()

    return sorted(values, key=key)


# Canonical display order per dimension — used for the ballot's dropdowns and for
# the rows of the results table. Age runs youngest to oldest and sexe keeps its
# declared order, both of which are meaningful; régions are alphabetical, because
# a list of fourteen is something people scan for their own rather than read.
DIMENSION_ORDER = {
    DIMENSION_AGE: [m.value for m in AgeBracket],
    DIMENSION_SEXE: [m.value for m in Sexe],
    DIMENSION_REGION: _alphabetical(m.value for m in Region),
}

# Allowed (dimension, category) pairs, used to validate incoming form values.
VALID_CATEGORIES = {dim: set(order) for dim, order in DIMENSION_ORDER.items()}

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
        UniqueConstraint("law_id", "dimension", "category", "choice", name="uq_demo_cell"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Not indexed on its own: `uq_demo_cell` above leads with this column, so it
    # already answers both the per-law read and the ON DELETE CASCADE. A second
    # index on the same prefix only cost writes.
    law_id: Mapped[int] = mapped_column(ForeignKey("laws.id", ondelete="CASCADE"))
    dimension: Mapped[str] = mapped_column(String(16))  # "age" | "sexe"
    category: Mapped[str] = mapped_column(String(32))  # e.g. "35-49" | "femme"
    choice: Mapped[str] = mapped_column(String(16))  # for | against | abstain
    count: Mapped[int] = mapped_column(Integer, default=0)
