"""laws.promulgated_on — the date the home page is ordered by

The home page listed votations by `created_at`: the instant an administrator
pressed save. Three laws entered in one evening therefore appeared in the order
they were typed, which is a fact about somebody's working schedule rather than
about the legislation or about the vote.

The promulgation date was already on the page, but only inside `official_ref`
(« LOI n° 2026-532 du 24 juin 2026 ») — a date you can print and never sort on.
The structured value existed one table away, on `imported_laws.promulgated_on`,
reachable only through `law_id` and therefore absent for any law written by hand.
So it becomes a column here, backfilled from the staged row where there is one.

Revision ID: 3f9a17c2e5d8
Revises: 7c31e0a4bd52
Create Date: 2026-08-29 15:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '3f9a17c2e5d8'
down_revision: str | None = '7c31e0a4bd52'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('laws', sa.Column('promulgated_on', sa.Date(), nullable=True))
    # Every law that came from the daily import already knows its date; carry it
    # over rather than asking an administrator to retype what we already hold.
    op.execute(
        "UPDATE laws SET promulgated_on = i.promulgated_on "
        "FROM imported_laws i WHERE i.law_id = laws.id"
    )


def downgrade() -> None:
    op.drop_column('laws', 'promulgated_on')
