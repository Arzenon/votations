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
    # Which choice a voter's demographics were folded in under, so a vote change
    # can move the aggregate counters instead of leaving them counted under the
    # old choice. Still no profile stored per voter — only a choice.
    op.add_column('votes', sa.Column('demographics_choice', sa.String(length=16), nullable=True))
    # Backfill: for votes that already contributed, the current choice is the
    # best available approximation of the one they contributed under.
    op.execute(
        "UPDATE votes SET demographics_choice = choice WHERE demographics_recorded = true"
    )

    # `voter_token` is only ever looked up together with `law_id`, which
    # uq_vote_law_voter already covers. The standalone index cost writes and
    # indexed a privacy-sensitive column for nothing.
    op.drop_index('ix_votes_voter_token', table_name='votes')

    # The AI draft now runs in the background: the staged row carries its state.
    op.add_column(
        'imported_laws',
        sa.Column('draft_status', sa.String(length=16), nullable=False, server_default=''),
    )
    op.alter_column('imported_laws', 'draft_status', server_default=None)
    op.add_column('imported_laws', sa.Column('draft_error', sa.Text(), nullable=True))
    # Rows that already hold a draft are, by definition, ready.
    op.execute("UPDATE imported_laws SET draft_status = 'pret' WHERE draft IS NOT NULL")


def downgrade() -> None:
    op.drop_column('imported_laws', 'draft_error')
    op.drop_column('imported_laws', 'draft_status')
    op.create_index('ix_votes_voter_token', 'votes', ['voter_token'], unique=False)
    op.drop_column('votes', 'demographics_choice')
