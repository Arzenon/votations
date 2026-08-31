"""publish demographic breakdowns from a snapshot, not live

`demographic_tallies` holds one *marginal* counter per dimension and never a
joint age × sexe × région cell, so that the smallest number stored at rest stays
as large as possible; `MIN_CELL_COUNT` then withholds a whole dimension unless
every populated cell clears it. Both of those protect a single reading of the
results page. Neither protects a *sequence* of readings.

The page is public, unthrottled, and recomputed on every request. Two reads
thirty seconds apart could therefore be subtracted from one another, and the
difference was one ballot: it named that voter's choice together with their age
bracket, their sex and their région — the joint cell the schema deliberately
refuses to store, reassembled out of pages anyone may fetch, without needing to
know when anybody voted.

So what the page publishes while a vote is open is now a snapshot. It moves in
steps of at least MIN_BUCKET_VOLUME *profiled* votes and at most once an hour, so
no difference between two readings can be attributed to a person; once the vote
closes the figures stop moving on their own and the live counters are published
as before. The headline tally is untouched and stays live — it is one bit about
one voter, and being checkable is the point of it.

Three columns carry that: the published value beside the live one, and the two
that record when the snapshot was last moved and how many profiled votes it had
been given at that moment.

Existing rows are backfilled rather than started from zero. Everything in
`published_count` here is already public — it is on the results page right now —
so publishing it again discloses nothing new, while zeroing it would blank every
open votation's breakdown until fifty more people filled the optional fields in.
The ratchet starts from today instead.

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

    # Publish what is already published, so the upgrade changes nothing a reader
    # can see. See the note above on why that discloses nothing.
    op.execute('UPDATE demographic_tallies SET published_count = count')
    # And start every law's clock now, with the profiled votes it has today, so
    # the first step after this deployment is a full one rather than a catch-up
    # that would move the figures by everything cast since the beginning.
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
