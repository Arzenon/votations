"""SQLAlchemy models.

What is re-exported here is what the rest of the application uses, and nothing
else. Four things are deliberately absent:

* `DIMENSION_ENUMS`, which is how `demographics.py` derives the display order and
  the validation sets rather than something a caller needs;
* `SEARCH_VECTOR_SQL`, the unqualified form of the search expression that only
  the CREATE INDEX in `law.py` uses — callers want the qualified
  `SEARCH_VECTOR_QUERY_SQL`, and offering both invites picking the wrong one;
* the three category enums — `AgeBracket`, `Sexe` and `Region`. Nothing outside
  `models/` names them: the categories reach the rest of the application through
  `DIMENSION_ORDER` and `VALID_CATEGORIES`, both derived from the enums, which is
  what lets a dimension be added in one line. `Region` was re-exported here until
  its last caller went away; a test still names it, and reaches for it where it
  lives, which is the honest import for something the application itself does not
  use.

Re-exporting a name nobody imports is not free: it reads as a supported way in,
and the next person adds a fourth enum here rather than to `DIMENSION_ENUMS`,
where it would actually do something.
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
    DemographicTally,
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
    "DemographicTally",
    "ImportedLaw",
    "InseeReference",
    "Law",
    "LawRevision",
    "Vote",
    "VoteChoice",
]
