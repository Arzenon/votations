"""Alembic migration environment.

Reads the database URL from the application settings (DATABASE_URL) — unless the
caller has already set one — and uses the SQLAlchemy metadata from app.db.Base so
that autogenerate sees all models.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app import models  # noqa: F401  (import registers all models on Base.metadata)
from app.config import get_settings
from app.db import Base, include_object

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers` defaults to True, which switches off every logger
    # that already exists — including the application's own. That is harmless when
    # `alembic upgrade` is its own process, which is how the container runs it,
    # and silently wrong the moment migrations run inside a process that also logs:
    # the test suite migrates once per session, and every `log.warning` in the app
    # went nowhere for the rest of it. A migration environment has no business
    # reconfiguring somebody else's loggers.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# A caller may point the migrations at another database (the test suite does).
# Only fall back to the application settings when nothing has been set, so
# `alembic upgrade` never silently migrates the wrong one.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
