"""set aside dossiers whose law was deleted

Deleting a law set its dossier's law_id to NULL but left it "importe", a
state the admin offers no action for. `app.crud.laws.purge` now moves it to
"ignore"; this does the same for the dossiers already in that state.

Revision ID: 0ae66fc1f5fb
Revises: a3d9f6c1e872
Create Date: 2026-10-20 19:00:00.000000
"""
from collections.abc import Sequence

from alembic import op

revision: str = '0ae66fc1f5fb'
down_revision: str | None = 'a3d9f6c1e872'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE imported_laws SET status = 'ignore' WHERE status = 'importe' AND law_id IS NULL"
    )


def downgrade() -> None:
    # Nothing to undo: "importe" without a law was never a valid state.
    pass
