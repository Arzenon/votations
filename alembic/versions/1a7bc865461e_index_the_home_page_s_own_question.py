"""ix_laws_home — the question every public listing asks

The home page, the search results and the sitemap all want the same thing: the
newest published, non-withdrawn laws. Every one of them got it by reading the
whole `laws` table and sorting it, because no index covered both the visibility
filter and the two-column order.

This one does. The partial clause *is* the visibility rule, and the key is
`app.crud.laws._NEWEST_LAW_FIRST` spelled the same way round, so the planner
walks the index in order and stops at the LIMIT. Measured at 50 000 laws: the
« Votations ouvertes » band went from 26.8 ms to 0.3 ms, and a band matching
nothing still falls back to a sequential scan rather than walking the whole
index — the planner decides that from its statistics, which is the right place
for it to be decided.

One thing has to stay true for any of that to happen: the query must filter on
`published`, not on `published IS true`. Postgres applies a partial index only
where it can prove the WHERE implies the index predicate, and it cannot prove
that of `IS true`. See the comment on `_published`, which is where the proof
either holds or quietly stops holding.

Revision ID: 1a7bc865461e
Revises: 3f9a17c2e5d8
Create Date: 2026-08-31 17:30:56.794042
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '1a7bc865461e'
down_revision: str | None = '3f9a17c2e5d8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WHERE = sa.text('published AND archived_at IS NULL')


def upgrade() -> None:
    op.create_index(
        'ix_laws_home',
        'laws',
        [
            sa.literal_column('promulgated_on DESC NULLS LAST'),
            sa.literal_column('created_at DESC'),
        ],
        unique=False,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index('ix_laws_home', table_name='laws', postgresql_where=_WHERE)
