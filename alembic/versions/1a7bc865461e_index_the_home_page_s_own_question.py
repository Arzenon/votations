"""ix_laws_home: index for the public listings

Partial index matching the visibility filter and `_NEWEST_LAW_FIRST`, so the
planner can stop at the LIMIT instead of sorting the whole table (26.8 ms down
to 0.3 ms at 50,000 laws).

Queries must filter on `published`, not `published IS true`, or Postgres won't
use the partial index.

Revision ID: 1a7bc865461e
Revises: 3f9a17c2e5d8
Create Date: 2026-08-31 17:30:56.794042
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '1a7bc865461e'
down_revision: str | None = '3f9a17c2e5d8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WHERE = sa.text('published AND archived_at IS NULL')


def upgrade() -> None:
    op.create_index(
        'ix_laws_home',
        'laws',
        [
            sa.literal_column('promulgated_on DESC NULLS LAST'),
            sa.literal_column('created_at DESC'),
        ],
        unique=False,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index('ix_laws_home', table_name='laws', postgresql_where=_WHERE)
