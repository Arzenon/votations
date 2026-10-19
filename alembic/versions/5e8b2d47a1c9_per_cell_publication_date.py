"""per-cell publication date of the demographic snapshot

A snapshot step now only copies the categories whose every cell moved by 0
or by at least MIN_CELL_COUNT, so a held-back category can date from an
earlier step than the others. Records when each cell was last copied, so the
results page can say so.

Backfilled with each law's last snapshot date: until now every step copied
every cell, so that date is exact.

Revision ID: 5e8b2d47a1c9
Revises: c47d0e91b8a5
Create Date: 2026-10-18 10:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '5e8b2d47a1c9'
down_revision: str | None = 'c47d0e91b8a5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'demographic_tallies',
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE demographic_tallies SET published_at = laws.demographics_snapshot_at
          FROM laws
         WHERE laws.id = demographic_tallies.law_id
        """
    )


def downgrade() -> None:
    op.drop_column('demographic_tallies', 'published_at')
