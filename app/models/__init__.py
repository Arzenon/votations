"""SQLAlchemy models.

What is re-exported here is what the rest of the application uses. Two things
are deliberately not: `DIMENSION_ENUMS`, which is how `demographics.py` derives
the display order and the validation sets rather than something a caller needs,
and `SEARCH_VECTOR_SQL`, which is the unqualified form of the search expression
that only the CREATE INDEX in `law.py` uses — callers want the qualified
`SEARCH_VECTOR_QUERY_SQL`, and offering both invites picking the wrong one.
"""

from app.models.demographics import (
    DIMENSION_AGE,
    DIMENSION_LABELS,
    DIMENSION_ORDER,
    DIMENSION_REGION,
    DIMENSION_SEXE,
    DIMENSIONS,
    INSEE_REFERENCE,
    VALID_CATEGORIES,
    AgeBracket,
    DemographicTally,
    Region,
    Sexe,
)
from app.models.imported_law import (
    DRAFT_FAILED,
    DRAFT_IDLE,
    DRAFT_PENDING,
    DRAFT_READY,
    SOURCE_ASSEMBLEE,
    STATUS_IGNORED,
    STATUS_IMPORTED,
    STATUS_NEW,
    ImportedLaw,
)
from app.models.insee import InseeReference
from app.models.law import (
    SEARCH_VECTOR_QUERY_SQL,
    STATUS_CLOSED,
    STATUS_DRAFT,
    STATUS_OPEN,
    STATUS_SCHEDULED,
    Law,
)
from app.models.revision import LawRevision
from app.models.vote import Vote, VoteChoice

__all__ = [
    "DIMENSIONS",
    "DIMENSION_AGE",
    "DIMENSION_LABELS",
    "DIMENSION_ORDER",
    "DIMENSION_REGION",
    "DIMENSION_SEXE",
    "DRAFT_FAILED",
    "DRAFT_IDLE",
    "DRAFT_PENDING",
    "DRAFT_READY",
    "INSEE_REFERENCE",
    "SEARCH_VECTOR_QUERY_SQL",
    "SOURCE_ASSEMBLEE",
    "STATUS_CLOSED",
    "STATUS_DRAFT",
    "STATUS_IGNORED",
    "STATUS_IMPORTED",
    "STATUS_NEW",
    "STATUS_OPEN",
    "STATUS_SCHEDULED",
    "VALID_CATEGORIES",
    "AgeBracket",
    "DemographicTally",
    "ImportedLaw",
    "InseeReference",
    "Law",
    "LawRevision",
    "Region",
    "Sexe",
    "Vote",
    "VoteChoice",
]
