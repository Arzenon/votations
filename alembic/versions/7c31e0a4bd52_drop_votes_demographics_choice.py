"""drop votes.demographics_choice — a vote no longer remembers the choice before it

The column held the choice a voter's optional profile had been counted under, so
that changing a vote could move the aggregate counters instead of leaving them
stale. It equalled `votes.choice` in every ordinary case; it differed in exactly
one — a browser that had forgotten its profile cookie, whose contribution could
therefore not be moved — and in that case the row stated both the choice somebody
had made *and* the one they had made before. A record of a changed mind, stored
per voter, on a platform whose privacy page lists the columns of this table and
calls the list exhaustive.

It is not needed. `app.crud.votes.cast_vote` now locks the row, reads the choice
it is about to overwrite, and hands that to the counters within the same
transaction. Where a contribution cannot be moved it simply stays where it is,
for good: the ballot stops offering the profile fields once a contribution
exists, and the profile cookie is only ever written back from categories it
already held, so a browser that has forgotten a law can never learn it again.

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
    # The best reconstruction there is, and deliberately not a perfect one: a
    # contribution that could not be moved sat under an *older* choice, and that
    # is precisely the fact the upgrade exists to stop storing. Rows whose
    # contribution is stranded come back pointing at the current choice, which
    # would make the old code decrement the wrong cell on a further change. Going
    # back therefore means accepting a small counter drift for those voters — the
    # information to do better is gone, which is the point.
    op.execute(
        "UPDATE votes SET demographics_choice = choice WHERE demographics_recorded"
    )
