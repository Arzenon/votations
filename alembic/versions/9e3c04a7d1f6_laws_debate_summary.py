"""laws.debate_summary: what the debate was about

One or two sentences shown at the top of the law page. Names the subject of
the disagreement, never a position. Written from the selected arguments and
tracked in the revision history. Empty for existing laws.

Revision ID: 9e3c04a7d1f6
Revises: 4d2e8b91c7f3
Create Date: 2026-09-01 22:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '9e3c04a7d1f6'
down_revision: str | None = '4d2e8b91c7f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'laws',
        sa.Column('debate_summary', sa.String(length=600), server_default='', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('laws', 'debate_summary')
