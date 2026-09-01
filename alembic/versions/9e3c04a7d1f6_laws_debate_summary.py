"""laws.debate_summary - what the debate turned on, in one or two sentences

Shown in the encart at the top of a law page, above everything else, because it
is what a hesitating reader needs first and the page had no way of telling them:
they had to scroll past two blocks of prose to discover there had even been a
disagreement.

**This is the one place the platform writes about the debate rather than quoting
it**, and the charter was widened on purpose to allow it. What makes that
acceptable is a rule of formulation: the sentence names the *subject* of the
disagreement and never a position on it. « Le débat a porté sur le contrôle
judiciaire des techniques de renseignement » is a fact about what was argued;
« cette loi pose un problème de contrôle judiciaire » is one side's framing.

It is drafted from the arguments already selected — verbatim, sourced, symmetric
— rather than freely from the sitting records, so a reader can check it against
the quotations on the same page. And it joins `TRACKED_FIELDS`, so an edit after
publication appears in the public record of changes like any other block.

Empty on every existing law, which reads correctly: the encart simply carries the
opening paragraph alone until somebody drafts or writes the sentence.

Revision ID: 9e3c04a7d1f6
Revises: 4d2e8b91c7f3
Create Date: 2026-09-01 22:40:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '9e3c04a7d1f6'
down_revision: str | None = '4d2e8b91c7f3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'laws',
        sa.Column('debate_summary', sa.String(length=600), server_default='', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('laws', 'debate_summary')
