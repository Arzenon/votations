"""Database engine, session factory and FastAPI dependency."""

from collections.abc import Iterator
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


# Indexes that autogenerate shouldn't compare with the models:
# - ix_laws_fts is an expression index, which Alembic can't compare and would
#   report as changed on every run.
# - ix_laws_official_ref_trgm needs pg_trgm, so it only exists where the
#   extension could be created.
UNCOMPARED_INDEXES = frozenset({"ix_laws_fts", "ix_laws_official_ref_trgm"})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Autogenerate filter, shared by `alembic/env.py` and the migration tests."""
    return not (type_ == "index" and name in UNCOMPARED_INDEXES)


def utcnow() -> datetime:
    """The single source of "now" for every timestamp column."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC. DB values are already aware (timestamptz)."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# Built lazily so `create_app` can check the production config first.
@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        # Keep parameters out of error messages: on the vote route they include
        # the voter token, and errors get logged.
        hide_parameters=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def SessionLocal() -> Session:
    """Open a new session."""
    return _session_factory()()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
