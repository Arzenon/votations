"""index the questions actually asked: covering votes, one revisions index, drop two dead ones

Three separate things, all of them the same thing: the indexes had drifted away
from the queries.

* ``votes`` is only ever read one law at a time and grouped — the tally, the
  evolution curve, the open-data export. A bare ``law_id`` index answered the
  WHERE and sent Postgres to the heap once per vote for the columns. The
  replacement carries them in its own leaves, so all three are answered from the
  index alone.
* ``law_revisions`` had one index for the filter and another for the order, so
  the public history sorted a law's whole history to show twenty rows. One
  composite in the order the page reads them replaces both.
* ``demographic_tallies.law_id`` and ``imported_laws.dossier_uid`` were each a
  second index on the leading column of an existing unique constraint. The
  planner never chose either; both cost writes.

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
    # `law_id` leads, so this also serves the ON DELETE CASCADE that `purge`
    # relies on — which is what makes dropping the old index safe rather than
    # merely tidy.
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
