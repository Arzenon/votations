"""per-law voter token

Each vote stored the SHA-256 of the voter's token, the same for every law, so
the table linked all of one browser's votes. It now stores
sha256("<law_id>:<that hash>") (see `app.auth.anonymous.law_token`), which
this migration computes from the stored hash. Voters keep their ballots.

Not reversible: the downgrade leaves the per-law values in place, since
restoring the shared hash would mean linking the votes again. Code from before
this migration no longer finds the stored ballots, and neither does a second
run of it: in both cases voters can vote again on open laws.

Revision ID: 7b1e4c9d2a60
Revises: 5e8b2d47a1c9
Create Date: 2026-10-19 16:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = '7b1e4c9d2a60'
down_revision: str | None = '5e8b2d47a1c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE votes
           SET voter_token = encode(
                 sha256(convert_to(law_id::text || ':' || voter_token, 'UTF8')), 'hex')
        """
    )


def downgrade() -> None:
    pass
