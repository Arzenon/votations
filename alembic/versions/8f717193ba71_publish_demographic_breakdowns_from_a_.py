"""publish demographic breakdowns from a snapshot, not live

Comparing two loads of the public results page could reveal a single voter's
choice along with their age, sex and region. While a vote is open, the page
now shows a snapshot that moves at most once an hour and by at least
MIN_BUCKET_VOLUME profiled votes. The headline tally stays live.

Adds the published counter and two columns recording when the snapshot last
moved and how many profiled votes it included. Backfilled with the current
values, which are already public.

Revision ID: 8f717193ba71
Revises: 1a7bc865461e
Create Date: 2026-08-31 17:34:24.470674
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '8f717193ba71'
down_revision: str | None = '1a7bc865461e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'demographic_tallies',
        sa.Column('published_count', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column(
        'laws',
        sa.Column('demographics_snapshot_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'laws',
        sa.Column(
            'demographics_snapshot_profiles',
            sa.Integer(),
            server_default='0',
            nullable=False,
        ),
    )

    # Start from the current (already public) values.
    op.execute('UPDATE demographic_tallies SET published_count = count')
    # Start each law's snapshot clock now.
    op.execute(
        """
        UPDATE laws SET
            demographics_snapshot_at = now(),
            demographics_snapshot_profiles = (
                SELECT count(*) FROM votes
                 WHERE votes.law_id = laws.id
                   AND votes.demographics_recorded
            )
        """
    )


def downgrade() -> None:
    op.drop_column('laws', 'demographics_snapshot_profiles')
    op.drop_column('laws', 'demographics_snapshot_at')
    op.drop_column('demographic_tallies', 'published_count')
