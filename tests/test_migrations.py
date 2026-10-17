"""The migrations build the same schema as the models, and run both ways."""

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
    """The migrated schema matches the models."""
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_object": include_object},
        )
        differences = compare_metadata(context, Base.metadata)
    assert differences == [], (
        "les modèles et les migrations divergent - générez une révision :\n  "
        "alembic revision --autogenerate -m '…'\n"
        f"{differences}"
    )


def test_there_is_exactly_one_head(engine):
    """There's a single head."""
    script = ScriptDirectory.from_config(alembic_config(TEST_DATABASE_URL))
    assert len(script.get_heads()) == 1, script.get_heads()


def test_every_revision_is_reachable_from_base(engine):
    script = ScriptDirectory.from_config(alembic_config(TEST_DATABASE_URL))
    walked = {rev.revision for rev in script.walk_revisions()}
    all_revisions = {rev.revision for rev in script.get_revisions("heads")}
    assert all_revisions <= walked


def test_the_unused_voter_token_index_is_gone(engine):
    """There's no separate index on voter_token."""
    indexes = {ix["name"] for ix in inspect(engine).get_indexes("votes")}
    assert "ix_votes_voter_token" not in indexes


def test_the_profiled_votes_index_is_partial(engine):
    """ix_votes_law_profiled is partial (read from pg_indexes)."""
    from sqlalchemy import text

    with engine.connect() as connection:
        definition = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_votes_law_profiled'")
        ).scalar()

    assert definition, "l'index partiel des votes profilés est absent"
    assert "WHERE demographics_recorded" in definition, definition


def test_counting_profiled_votes_can_use_that_index(engine):
    """count_profiled uses the partial index (same predicate as the query)."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects import postgresql

    from app.models import Vote

    compiled = str(
        select(func.count())
        .select_from(Vote)
        .where(Vote.law_id == 1, Vote.demographics_recorded)
        .compile(dialect=postgresql.dialect())
    )
    assert "votes.demographics_recorded" in compiled
    assert "is true" not in compiled.lower(), compiled


def test_a_dossier_cannot_claim_two_votations(engine):
    """A dossier can't be linked to two laws."""
    names = {c["name"] for c in inspect(engine).get_unique_constraints("imported_laws")}
    assert "uq_imported_law_law_id" in names


def test_the_chain_downgrades_and_upgrades_again(engine):
    """All migrations downgrade and upgrade again."""
    config = alembic_config(TEST_DATABASE_URL)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"la chaîne de migrations ne fait pas l'aller-retour : {exc}")

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_object": include_object},
        )
        assert compare_metadata(context, Base.metadata) == []


# --- Migration drift ---------------------------------------------------------


def test_conditionally_created_indexes_are_not_compared():
    """The two hand-written indexes are excluded from the drift check."""
    for name in ("ix_laws_fts", "ix_laws_official_ref_trgm"):
        assert not include_object(None, name, "index", True, None)
    # Other indexes are still compared.
    assert include_object(None, "ix_laws_created_at", "index", True, None)
    assert include_object(None, "laws", "table", True, None)
