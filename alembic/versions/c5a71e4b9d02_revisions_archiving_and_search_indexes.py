"""law revisions, archiving instead of deletion, and the missing indexes

Revision ID: c5a71e4b9d02
Revises: a1c4e9f27b30
Create Date: 2026-08-20 09:12:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c5a71e4b9d02'
down_revision: str | None = 'a1c4e9f27b30'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Columns the public search scans with an unanchored ILIKE. A btree index cannot
# serve `%term%`, so these need trigram indexes or every search is a sequential
# scan of the whole corpus — twice, once for the count and once for the page.
_SEARCH_COLUMNS = ("title", "official_ref", "summary_text")


def _has_trigram(conn) -> bool:
    """Install pg_trgm if this role is allowed to, and report whether it is there.

    Managed Postgres often forbids CREATE EXTENSION. That must not fail the whole
    migration: search still works without the index, just unindexed. The attempt
    runs inside a savepoint because a refused statement would otherwise poison the
    surrounding transaction and take the rest of this migration with it.
    """
    already = conn.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
    ).scalar()
    if already:
        return True
    try:
        with conn.begin_nested():
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    except sa.exc.DatabaseError:
        return False
    return bool(
        conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).scalar()
    )


def upgrade() -> None:
    # --- Archiving replaces deletion -----------------------------------------
    # Deleting a law cascaded away every vote cast on it. An archived law leaves
    # the public site but keeps its votes and can be restored; purging is a
    # separate, explicit act.
    op.add_column('laws', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_laws_archived_at'), 'laws', ['archived_at'], unique=False)

    # --- The public record of edits made after publication -------------------
    op.create_table(
        'law_revisions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('law_id', sa.Integer(), nullable=False),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('field', sa.String(length=32), nullable=False),
        sa.Column('old_value', sa.Text(), nullable=False),
        sa.Column('new_value', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['law_id'], ['laws.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_law_revisions_law_id'), 'law_revisions', ['law_id'], unique=False)
    op.create_index(
        op.f('ix_law_revisions_changed_at'), 'law_revisions', ['changed_at'], unique=False
    )

    # --- Indexes for columns filtered or ordered on every page load ----------
    op.create_index(op.f('ix_laws_created_at'), 'laws', ['created_at'], unique=False)
    op.create_index(op.f('ix_imported_laws_status'), 'imported_laws', ['status'], unique=False)
    op.create_index(
        op.f('ix_imported_laws_last_seen_at'), 'imported_laws', ['last_seen_at'], unique=False
    )

    # --- Trigram indexes for the public search -------------------------------
    conn = op.get_bind()
    if _has_trigram(conn):
        for column in _SEARCH_COLUMNS:
            op.create_index(
                f'ix_laws_{column}_trgm',
                'laws',
                [sa.text(f'{column} gin_trgm_ops')],
                postgresql_using='gin',
            )


def downgrade() -> None:
    conn = op.get_bind()
    for column in _SEARCH_COLUMNS:
        conn.execute(sa.text(f'DROP INDEX IF EXISTS ix_laws_{column}_trgm'))

    op.drop_index(op.f('ix_imported_laws_last_seen_at'), table_name='imported_laws')
    op.drop_index(op.f('ix_imported_laws_status'), table_name='imported_laws')
    op.drop_index(op.f('ix_laws_created_at'), table_name='laws')

    op.drop_index(op.f('ix_law_revisions_changed_at'), table_name='law_revisions')
    op.drop_index(op.f('ix_law_revisions_law_id'), table_name='law_revisions')
    op.drop_table('law_revisions')

    op.drop_index(op.f('ix_laws_archived_at'), table_name='laws')
    op.drop_column('laws', 'archived_at')
