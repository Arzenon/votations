"""French full-text index for the public search

Revision ID: d81f3a5c07e4
Revises: c5a71e4b9d02
Create Date: 2026-08-21 09:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd81f3a5c07e4'
down_revision: str | None = 'c5a71e4b9d02'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match `app.models.law.SEARCH_VECTOR_SQL`, otherwise the index isn't used.
# coalesce and || because concat_ws isn't IMMUTABLE.
_VECTOR = (
    "to_tsvector('french'::regconfig, "
    "coalesce(title, '') || ' ' || coalesce(summary_text, ''))"
)


def upgrade() -> None:
    # Expression index rather than a stored tsvector column, which would need a
    # trigger or a generated column.
    op.execute(f"CREATE INDEX ix_laws_fts ON laws USING gin ({_VECTOR})")

    # Full-text search replaces ILIKE on title and summary_text, so their
    # trigram indexes are no longer needed.
    conn = op.get_bind()
    for column in ("title", "summary_text"):
        conn.execute(sa.text(f"DROP INDEX IF EXISTS ix_laws_{column}_trgm"))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_laws_fts")

    # Restore the trigram indexes if pg_trgm is available.
    conn = op.get_bind()
    has_trgm = conn.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
    ).scalar()
    if has_trgm:
        for column in ("title", "summary_text"):
            conn.execute(
                sa.text(
                    f"CREATE INDEX ix_laws_{column}_trgm ON laws "
                    f"USING gin ({column} gin_trgm_ops)"
                )
            )
