"""Database engine, session factory and FastAPI dependency."""

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


# Alembic renders a functional index expression as opaque text and cannot compare
# two of them, so autogenerate reports a spurious drop-and-recreate for these on
# every run — which would make the drift check cry wolf until nobody reads it.
# They are written by hand in their migration instead. Anything *not* named here
# is still compared, so real drift is still caught.
UNCOMPARABLE_INDEXES = frozenset({"ix_laws_fts"})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Autogenerate filter, shared by `alembic/env.py` and the migration tests."""
    return not (type_ == "index" and name in UNCOMPARABLE_INDEXES)


def utcnow() -> datetime:
    """The single source of "now" for every timestamp column."""
    return datetime.now(UTC)


# The engine is built on first use, not at import time, so the production
# configuration check in `create_app` runs before anything opens a socket.
@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
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
