"""The migrations must build exactly the schema the models describe.

The suite migrates the test database rather than calling ``create_all``, so a
missing migration now breaks every test rather than only the production upgrade.
This file closes the other half: that the migrated schema and the model metadata
actually agree, and that the chain runs both ways.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from alembic import command
from app.db import Base, include_object
from tests.conftest import TEST_DATABASE_URL, alembic_config


def test_the_migrated_schema_matches_the_models(engine):
    """Catch drift the moment a model changes without a migration."""
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_object": include_object},
        )
        differences = compare_metadata(context, Base.metadata)
    assert differences == [], (
        "les modèles et les migrations divergent — générez une révision :\n  "
        "alembic revision --autogenerate -m '…'\n"
        f"{differences}"
    )


def test_there_is_exactly_one_head(engine):
    """Two heads mean two branches, and `upgrade head` becomes ambiguous."""
    script = ScriptDirectory.from_config(alembic_config(TEST_DATABASE_URL))
    assert len(script.get_heads()) == 1, script.get_heads()


def test_every_revision_is_reachable_from_base(engine):
    script = ScriptDirectory.from_config(alembic_config(TEST_DATABASE_URL))
    walked = {rev.revision for rev in script.walk_revisions()}
    all_revisions = {rev.revision for rev in script.get_revisions("heads")}
    assert all_revisions <= walked


def test_the_unused_voter_token_index_is_gone(engine):
    """`voter_token` is only ever read with `law_id`, which the unique key covers."""
    indexes = {ix["name"] for ix in inspect(engine).get_indexes("votes")}
    assert "ix_votes_voter_token" not in indexes


def test_the_chain_downgrades_and_upgrades_again(engine):
    """A migration you cannot undo is a migration you cannot deploy carefully."""
    config = alembic_config(TEST_DATABASE_URL)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    except Exception as exc:  # pragma: no cover - the assertion is the message
        pytest.fail(f"la chaîne de migrations ne fait pas l'aller-retour : {exc}")

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_object": include_object},
        )
        assert compare_metadata(context, Base.metadata) == []
