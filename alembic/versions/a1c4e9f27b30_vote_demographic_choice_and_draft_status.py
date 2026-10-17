"""track the choice demographics were counted under; draft lifecycle; drop unused index

Revision ID: a1c4e9f27b30
Revises: 6af9828ed5cf
Create Date: 2026-08-19 18:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a1c4e9f27b30'
down_revision: str | None = '6af9828ed5cf'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The choice a voter's profile was counted under, so a vote change can move
    # the counters.
    op.add_column('votes', sa.Column('demographics_choice', sa.String(length=16), nullable=True))
    # Backfill with the current choice (best approximation).
    op.execute(
        "UPDATE votes SET demographics_choice = choice WHERE demographics_recorded = true"
    )

    # voter_token is only queried with law_id, covered by uq_vote_law_voter.
    op.drop_index('ix_votes_voter_token', table_name='votes')

    # AI drafts now run in the background; store their status.
    op.add_column(
        'imported_laws',
        sa.Column('draft_status', sa.String(length=16), nullable=False, server_default=''),
    )
    op.alter_column('imported_laws', 'draft_status', server_default=None)
    op.add_column('imported_laws', sa.Column('draft_error', sa.Text(), nullable=True))
    # Rows with a draft are ready.
    op.execute("UPDATE imported_laws SET draft_status = 'pret' WHERE draft IS NOT NULL")


def downgrade() -> None:
    op.drop_column('imported_laws', 'draft_error')
    op.drop_column('imported_laws', 'draft_status')
    op.create_index('ix_votes_voter_token', 'votes', ['voter_token'], unique=False)
    op.drop_column('votes', 'demographics_choice')
