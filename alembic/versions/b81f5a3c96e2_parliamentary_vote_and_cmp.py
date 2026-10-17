"""Assemblée vote counts and whether a CMP was convened

The counts come from the scrutins export (94 of 147 laws in the 17th
legislature have one). The CMP flag comes from the dossiers export. Null when
there was no public scrutin (about a third of laws).

Added to both tables: refreshed on the staged row and copied onto the law. Not
editable in the admin and not tracked in revisions.

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
