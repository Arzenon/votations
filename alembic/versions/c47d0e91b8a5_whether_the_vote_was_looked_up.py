"""an_vote_checked: "no scrutin" vs "not looked up"

Empty vote counts could mean there was no public scrutin, or that nobody had
checked yet, and the page assumed the former. Now: counts present, show them;
checked and none, say there was no scrutin; unchecked, say nothing.

False for existing rows. The next `scripts/refresh_assemblee` run fills it in,
and `propagate_parliament_record` copies it onto existing laws.

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
