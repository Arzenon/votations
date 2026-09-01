"""how the Assemblée settled the text, and whether a CMP was needed

« Adoptée par 357 voix contre 177 » and « adoptée par 269 voix contre 0 » do not
say the same thing to somebody about to give their own opinion, and the site said
neither. Both are published as open data and neither was being read.

Two sources, one already in hand:

* **the counts** come from the scrutins export, joined on the vote reference a
  dossier's decision act carries. Measured over the 17th legislature: 94 of 147
  promulgated laws have a usable count, 29 of them adopted without a single vote
  against;
* **the commission mixte paritaire** is in the dossiers file the import already
  downloads every day, so it costs nothing. 56 of 147, and it means the two
  chambers did not agree on their own - a fact about how contested a text was,
  and one nobody has to characterise.

Null throughout is a real state, not a gap: a third of these laws were adopted
without a public scrutin, and the page says so.

The columns sit on both tables for the same reason `promulgated_on` does - the
daily job refreshes the staged row, and the values are copied onto the votation
when an administrator creates one. They are deliberately absent from the
administration form and from `TRACKED_FIELDS`: they are Parliament's record, not
presentation somebody writes, and an administrator editing a vote count is not an
operation worth making easy.

Revision ID: b81f5a3c96e2
Revises: 9e3c04a7d1f6
Create Date: 2026-09-01 23:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b81f5a3c96e2'
down_revision: str | None = '9e3c04a7d1f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("laws", "imported_laws")
_COUNTS = ("an_vote_for", "an_vote_against", "an_vote_abstain")


def upgrade() -> None:
    for table in _TABLES:
        for column in _COUNTS:
            op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                'cmp_convened', sa.Boolean(), server_default=sa.text('false'), nullable=False
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, 'cmp_convened')
        for column in _COUNTS:
            op.drop_column(table, column)
