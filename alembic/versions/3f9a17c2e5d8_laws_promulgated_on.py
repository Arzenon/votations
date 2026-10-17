"""laws.promulgated_on, used to sort the home page

The home page was sorted by `created_at`, i.e. when the law was entered. The
promulgation date was only in `official_ref` as text. Backfilled from the
staged row when there is one.

Revision ID: 3f9a17c2e5d8
Revises: 7c31e0a4bd52
Create Date: 2026-08-29 15:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '3f9a17c2e5d8'
down_revision: str | None = '7c31e0a4bd52'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('laws', sa.Column('promulgated_on', sa.Date(), nullable=True))
    # Copy the date from the staged row.
    op.execute(
        "UPDATE laws SET promulgated_on = i.promulgated_on "
        "FROM imported_laws i WHERE i.law_id = laws.id"
    )


def downgrade() -> None:
    op.drop_column('laws', 'promulgated_on')
