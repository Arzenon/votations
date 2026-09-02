"""an_vote_checked - telling "no scrutin" apart from "nobody looked"

The previous revision added the Assemblée's vote counts, and left two very
different situations producing the same empty columns:

* the text was adopted without a public scrutin - true of a third of promulgated
  laws, and worth saying;
* nobody has consulted the scrutins export for this text yet.

The page read the emptiness as the first and printed « adoptée à l'Assemblée
nationale sans scrutin public ». On the four laws already published when this
shipped, that was a statement about how Parliament voted made from never having
looked - and it would not have corrected itself, because the record was copied
from the staged row only when the votation was created and never again.

So: three states rather than two. Counts present, say them; checked and none,
say there was no scrutin; unchecked, say nothing at all. The daily import sets
the flag whether or not it finds a vote, and leaves it alone when the download
fails, which is exactly when the page must stay quiet.

`false` on every existing row, which is the honest starting point: nothing has
been looked up yet. One run of `scripts/refresh_assemblee` fills it in - and
`propagate_parliament_record`, added in the same change, is what finally carries
it onto the votations that were created before any of this existed.

Revision ID: c47d0e91b8a5
Revises: b81f5a3c96e2
Create Date: 2026-09-02 10:10:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c47d0e91b8a5'
down_revision: str | None = 'b81f5a3c96e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("laws", "imported_laws")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                'an_vote_checked', sa.Boolean(), server_default=sa.text('false'), nullable=False
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, 'an_vote_checked')
