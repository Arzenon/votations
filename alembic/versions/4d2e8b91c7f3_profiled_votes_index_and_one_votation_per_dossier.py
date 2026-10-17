"""profiled-votes index, and one votation per dossier

`ix_votes_law_profiled`: `refresh_snapshot` counts profiled votes on every
ballot until the first snapshot. Without this index that meant scanning every
vote on the law. The predicate must stay the bare column (Postgres won't match
`IS true`) and identical to the model, since Alembic doesn't compare it.

`uq_imported_law_law_id`: one dossier per law, enforced in the database since
`scripts.content` can also set law_id. NULLs don't collide. Existing duplicates
are cleared first, keeping the first staged row.

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

    # Keep the first staged dossier (most likely the one the law came from).
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
    # Cleared links can't be restored.
    op.drop_constraint('uq_imported_law_law_id', 'imported_laws', type_='unique')
    op.drop_index('ix_votes_law_profiled', table_name='votes', postgresql_where=_PROFILED)
