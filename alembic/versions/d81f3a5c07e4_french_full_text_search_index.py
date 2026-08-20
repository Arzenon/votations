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

# Must stay equivalent to `app.crud.laws._SEARCH_VECTOR_SQL`, or the planner stops
# using this index and every search falls back to a sequential scan.
#
# `coalesce` and `||`, not `concat_ws`: an index expression must be IMMUTABLE and
# `concat_ws` is not. Column names are unqualified here because that is the form
# CREATE INDEX reads most naturally; the query qualifies them, which matches all
# the same since Postgres compares parsed expressions rather than strings.
_VECTOR = (
    "to_tsvector('french'::regconfig, "
    "coalesce(title, '') || ' ' || coalesce(summary_text, ''))"
)


def upgrade() -> None:
    # A GIN index on the expression rather than a stored tsvector column: the
    # column would need a trigger or a generated column to stay in step, and this
    # keeps the definition in exactly one place per side (here, and in the query).
    op.execute(f"CREATE INDEX ix_laws_fts ON laws USING gin ({_VECTOR})")

    # The trigram indexes from the previous revision covered `title` and
    # `summary_text` for the old substring search. Full-text replaces that for the
    # prose; only `official_ref` is still matched with ILIKE, so the other two are
    # now dead weight on every insert and update.
    conn = op.get_bind()
    for column in ("title", "summary_text"):
        conn.execute(sa.text(f"DROP INDEX IF EXISTS ix_laws_{column}_trgm"))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_laws_fts")

    # Restore the trigram indexes, if the extension is available — it may not be,
    # in which case search still worked before and will work again, unindexed.
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
