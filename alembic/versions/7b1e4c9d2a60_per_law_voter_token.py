"""per-law voter token

Each vote stored the SHA-256 of the voter's token, the same for every law, so
the table linked all of one browser's votes. It now stores
sha256("<law_id>:<that hash>") (see `app.auth.anonymous.law_token`), which
this migration computes from the stored hash. Voters keep their ballots.

Not reversible: restoring the shared hash would mean linking the votes again.
Code from before this migration no longer finds the stored ballots, and a
second run of it would hash them twice: either way voters could vote again on
open laws. So the downgrade refuses to run while there are votes. See DEPLOY.md,
"Les migrations irréversibles".

Revision ID: 7b1e4c9d2a60
Revises: 5e8b2d47a1c9
Create Date: 2026-10-19 16:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

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
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM votes)")).scalar():
        raise RuntimeError(
            "Migration 7b1e4c9d2a60 irréversible : la rejouer ensuite hacherait les "
            "jetons une seconde fois, et chacun pourrait revoter. Pour revenir en "
            "arrière, restaurez le dump pris avant le déploiement (voir DEPLOY.md)."
        )
