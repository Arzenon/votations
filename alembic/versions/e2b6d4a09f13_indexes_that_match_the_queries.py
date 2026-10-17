"""index the actual queries: covering votes index, one revisions index, drop two unused

- votes: covering index on (law_id, choice) including the other columns read,
  so the tally, curve and export don't touch the table.
- law_revisions: one composite index for filter and order, replacing two.
- demographic_tallies.law_id and imported_laws.dossier_uid duplicated the
  leading column of a unique constraint and were never used.

Revision ID: e2b6d4a09f13
Revises: b7419fd0c8a2
Create Date: 2026-08-25 23:10:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = 'e2b6d4a09f13'
down_revision: str | None = 'b7419fd0c8a2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- votes: cover the three per-law aggregations -------------------------
    # Starts with law_id, so it also serves the ON DELETE CASCADE.
    op.execute(
        "CREATE INDEX ix_votes_law_choice ON votes (law_id, choice) "
        "INCLUDE (created_at, demographics_recorded)"
    )
    op.drop_index('ix_votes_law_id', table_name='votes')

    # --- law_revisions: filter and order in one index ------------------------
    op.execute(
        "CREATE INDEX ix_law_revisions_law_changed "
        "ON law_revisions (law_id, changed_at DESC, id DESC)"
    )
    op.drop_index('ix_law_revisions_changed_at', table_name='law_revisions')
    op.drop_index('ix_law_revisions_law_id', table_name='law_revisions')

    # --- redundant prefixes of an existing unique constraint -----------------
    op.drop_index('ix_demographic_tallies_law_id', table_name='demographic_tallies')
    op.drop_index('ix_imported_laws_dossier_uid', table_name='imported_laws')


def downgrade() -> None:
    op.create_index('ix_imported_laws_dossier_uid', 'imported_laws', ['dossier_uid'], unique=False)
    op.create_index(
        'ix_demographic_tallies_law_id', 'demographic_tallies', ['law_id'], unique=False
    )

    op.create_index('ix_law_revisions_law_id', 'law_revisions', ['law_id'], unique=False)
    op.create_index('ix_law_revisions_changed_at', 'law_revisions', ['changed_at'], unique=False)
    op.drop_index('ix_law_revisions_law_changed', table_name='law_revisions')

    op.create_index('ix_votes_law_id', 'votes', ['law_id'], unique=False)
    op.drop_index('ix_votes_law_choice', table_name='votes')
