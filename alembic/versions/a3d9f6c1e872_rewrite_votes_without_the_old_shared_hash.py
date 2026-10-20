"""rewrite votes without the old shared hash

7b1e4c9d2a60 replaced each stored token with a per-law one, but an UPDATE
leaves the previous row version in the table and index files until the space
is reused: anyone with a copy of the data volume could still read the shared
hash and link a browser's votes. VACUUM FULL rewrites the table and its
indexes, dropping those versions.

Separate from 7b1e4c9d2a60 because VACUUM can't run in a transaction: the
autocommit block commits what came before it, and a failure after committing
that UPDATE but before recording its revision would hash the tokens twice on
the next run. This one is safe to run again.

Revision ID: a3d9f6c1e872
Revises: 7b1e4c9d2a60
Create Date: 2026-10-20 15:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'a3d9f6c1e872'
down_revision: str | None = '7b1e4c9d2a60'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("VACUUM FULL votes")


def downgrade() -> None:
    # Nothing to undo: the table holds the same rows.
    pass
