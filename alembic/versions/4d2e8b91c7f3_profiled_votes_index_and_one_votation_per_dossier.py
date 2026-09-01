"""the profiled-votes index, and one votation per dossier as a constraint

Two things, both of them a question the schema was not answering.

**`ix_votes_law_profiled`.** `refresh_snapshot` asks « how many of this law's
votes carried a profile » on *every ballot*, until the law takes its first
snapshot — which needs MIN_BUCKET_VOLUME profiled votes and, the profile being
optional, may never happen. `ix_votes_law_choice` answered it from its included
column by walking every vote on the law, so a votation with a hundred thousand
ballots and forty profiles paid a hundred-thousand-entry scan per ballot, on the
busiest write in the application. This index holds the profiled votes and nothing
else.

The predicate is the bare column, and `app.crud.results.count_profiled` spells
its WHERE the same way, because Postgres uses a partial index only where it can
prove the query implies the predicate — and it cannot prove that of `IS true`.
The same lesson `ix_laws_home` already carries. Note that Alembic compares an
index on its columns and its uniqueness and never on its `where`, so nothing
would catch these two spellings drifting apart: keep them identical.

**`uq_imported_law_law_id`.** One dossier, one votation. `create_law` refuses to
build a second law from a staged row that already has one, and that guard is the
right place to explain the rule to an administrator — but it is not the only way
in. `scripts.content` re-applies the review decisions carried in a snapshot, and
nothing there stopped two dossiers claiming the same law; `get_by_law_id` then
returned whichever row came back first, so the AI draft shown on a law's form
could have belonged to a different dossier.

A plain unique constraint, not a partial index: SQL treats NULLs as distinct, so
every staged row still awaiting a decision keeps its NULL and none of them
collide. It also gives `law_id` the btree index it never had, read on every visit
to a law's form and walked by the `ON DELETE SET NULL` of a purge.

Existing duplicates are cleared first, keeping the row that was staged first —
otherwise the constraint would refuse to be created on the one kind of database
that needs it. The link is only ever a convenience (it is what offers the draft
again); the law and the dossier both survive intact.

Revision ID: 4d2e8b91c7f3
Revises: 8f717193ba71
Create Date: 2026-09-01 10:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '4d2e8b91c7f3'
down_revision: str | None = '8f717193ba71'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROFILED = sa.text('demographics_recorded')


def upgrade() -> None:
    op.create_index(
        'ix_votes_law_profiled',
        'votes',
        ['law_id'],
        unique=False,
        postgresql_where=_PROFILED,
    )

    # Keep the dossier that was staged first — the one a law was almost certainly
    # created from — and release the others' claim on it.
    op.execute(
        """
        UPDATE imported_laws SET law_id = NULL
         WHERE law_id IS NOT NULL
           AND id <> (
                SELECT min(other.id) FROM imported_laws AS other
                 WHERE other.law_id = imported_laws.law_id
           )
        """
    )
    op.create_unique_constraint('uq_imported_law_law_id', 'imported_laws', ['law_id'])


def downgrade() -> None:
    # The links cleared above are not restored: nothing recorded which they were,
    # and inventing one would point a dossier at a votation it did not produce.
    op.drop_constraint('uq_imported_law_law_id', 'imported_laws', type_='unique')
    op.drop_index('ix_votes_law_profiled', table_name='votes', postgresql_where=_PROFILED)
