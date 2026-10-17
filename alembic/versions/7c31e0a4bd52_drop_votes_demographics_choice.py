"""drop votes.demographics_choice

The column stored the choice a voter's profile had been counted under. When
it differed from `votes.choice`, it recorded that the voter had changed their
mind. `cast_vote` now reads the previous choice under a lock and passes it to
the counters, so the column isn't needed.

Revision ID: 7c31e0a4bd52
Revises: e2b6d4a09f13
Create Date: 2026-08-28 17:10:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '7c31e0a4bd52'
down_revision: str | None = 'e2b6d4a09f13'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column('votes', 'demographics_choice')


def downgrade() -> None:
    op.add_column('votes', sa.Column('demographics_choice', sa.String(length=16), nullable=True))
    # Approximate: the older choice is gone, so this may cause a small counter
    # drift for voters whose contribution couldn't be moved.
    op.execute(
        "UPDATE votes SET demographics_choice = choice WHERE demographics_recorded"
    )
