"""Pytest fixtures: an isolated PostgreSQL test database and a TestClient.

Two deliberate choices:

* **The schema is built by the migrations**, once per session, not by
  ``create_all``. Building it from the models would have meant the suite never
  touched Alembic, so drift between the two could only be discovered by running
  an upgrade in production.
* **Each test runs inside a transaction that is rolled back**, rather than
  dropping and recreating every table. The application commits freely; binding
  the session to an outer transaction in savepoint mode lets it, and still
  leaves the database untouched afterwards.

Set ``TEST_DATABASE_URL`` to override the target — e.g. inside Docker it points
at the ``db`` service.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db import get_db
from app.main import app
from app.models import Law

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://votations:votations@localhost:5432/votations_test",
)


def _ensure_database(url: str) -> None:
    """Create the target database if absent (CREATE DATABASE needs autocommit)."""
    url_obj = make_url(url)
    db_name = url_obj.database
    admin = create_engine(url_obj.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin.dispose()


def alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="session")
def engine():
    """One engine per session, with the schema migrated to head."""
    _ensure_database(TEST_DATABASE_URL)
    eng = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    # Start from nothing so a schema left by an older revision cannot mask a
    # broken migration chain.
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    """A session whose every write is rolled back when the test ends.

    The outer transaction never commits; ``join_transaction_mode`` turns the
    application's own ``commit()`` calls into savepoint releases inside it.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def sessionmaker_for(engine):
    """A plain session factory on the test engine, for code that opens its own."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def client(db_session):
    return TestClient(app)


def _utc(offset_hours: int) -> datetime:
    return datetime.now(UTC) + timedelta(hours=offset_hours)


@pytest.fixture
def open_law(db_session):
    law = Law(
        slug="loi-test",
        title="Loi de test",
        summary_text="Résumé.",
        published=True,
        opens_at=_utc(-1),
        closes_at=_utc(+24),
    )
    db_session.add(law)
    db_session.commit()
    db_session.refresh(law)
    return law


@pytest.fixture
def closed_law(db_session):
    law = Law(
        slug="loi-close",
        title="Loi close",
        published=True,
        opens_at=_utc(-48),
        closes_at=_utc(-1),
    )
    db_session.add(law)
    db_session.commit()
    db_session.refresh(law)
    return law


class _NoCloseSession:
    """Hands the shared test session to code that opens (and closes) its own.

    The background drafting job runs after its request and normally opens a fresh
    session; in a test it has to join the transaction the test will roll back, and
    must not close the session the test is still asserting on.
    """

    def __init__(self, inner: Session) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


@pytest.fixture
def background_db(db_session, monkeypatch):
    """Run background jobs against the test transaction."""
    monkeypatch.setattr("app.drafting.SessionLocal", lambda: _NoCloseSession(db_session))
    return db_session


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    """Rate-limit counters are process-global; no test may inherit another's."""
    from app import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()
