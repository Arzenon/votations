"""drop the unread imported_laws.law_title column

Revision ID: b7419fd0c8a2
Revises: d81f3a5c07e4
Create Date: 2026-08-22 11:20:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'b7419fd0c8a2'
down_revision: str | None = 'd81f3a5c07e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # titreLoi ("visant à renforcer ...") duplicates the dossier title and was
    # never read.
    op.drop_column('imported_laws', 'law_title')


def downgrade() -> None:
    # Restored empty; the next import would fill it.
    op.add_column(
        'imported_laws',
        sa.Column('law_title', sa.Text(), nullable=False, server_default=''),
    )
    op.alter_column('imported_laws', 'law_title', server_default=None)
