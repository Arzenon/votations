"""Database engine, session factory and FastAPI dependency."""

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


# Indexes autogenerate must not try to reconcile with the models. Both entries
# are written by hand in their migration, for two different reasons:
#
# * `ix_laws_fts` is a functional index. Alembic renders the expression as opaque
#   text and cannot compare two of them, so it reports a spurious
#   drop-and-recreate on every run — which would make the drift check cry wolf
#   until nobody read it.
# * `ix_laws_official_ref_trgm` needs the `pg_trgm` extension, and managed
#   Postgres often refuses CREATE EXTENSION. Its migration therefore creates it
#   only where the extension is available, so it is present on some deployments
#   and absent on others. Declaring it on the model would make every install
#   without pg_trgm fail to migrate; not declaring it made autogenerate want to
#   drop it everywhere it does exist.
#
# Anything *not* named here is still compared, so real drift is still caught.
UNCOMPARED_INDEXES = frozenset({"ix_laws_fts", "ix_laws_official_ref_trgm"})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Autogenerate filter, shared by `alembic/env.py` and the migration tests."""
    return not (type_ == "index" and name in UNCOMPARED_INDEXES)


def utcnow() -> datetime:
    """The single source of "now" for every timestamp column."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC, so a comparison never uses local time.

    Every timestamp column is ``timestamptz`` and comes back aware, so this is
    only reached by values built in Python. It exists because the alternative —
    ``datetime.now(tz=value.tzinfo)`` — silently compares local wall-clock time
    against a UTC instant on the day something hands over a naive value.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# The engine is built on first use, not at import time, so the production
# configuration check in `create_app` runs before anything opens a socket.
@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        # Keep bound parameters out of the text of a database error.
        #
        # SQLAlchemy renders `[parameters: {...}]` into `str(StatementError)`, and
        # `app.main`'s last-resort handler logs the exception. On the vote route
        # the parameters of a failing statement are the law id, the choice and
        # **the voter token** — so an IntegrityError there would have written a
        # voter's identifier next to the instant of their ballot, in the logs, on
        # a site whose privacy page states that pairing is never recorded and
        # whose `app.logging_config` says in as many words never to log a token.
        #
        # The failure is still fully diagnosable: the statement, the table and
        # the underlying database message are all still there. Only the values go.
        hide_parameters=True,
        # Sized against the request thread pool: a pool smaller than the worker
        # count turns every burst into a queue on connection checkout.
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def SessionLocal() -> Session:
    """Open a new session. Call sites are unchanged: ``SessionLocal()``."""
    return _session_factory()()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
