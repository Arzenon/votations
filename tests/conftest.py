"""Pytest fixtures: a PostgreSQL test database and a TestClient.

The schema is built by the migrations (once per session), not create_all, so
migrations are tested too. Each test runs in a transaction that is rolled back.

Set ``TEST_DATABASE_URL`` to use another database (in Docker, the ``db``
service).
"""

import os
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
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


UNREACHABLE = """\
PostgreSQL est injoignable sur {url}

La suite s'exécute contre une vraie base : le schéma est construit par les
migrations, et plusieurs comportements testés (upsert ON CONFLICT, date_trunc,
suppression en cascade) n'existent que dans PostgreSQL.

  docker compose -f docker-compose.dev.yml up -d db
  # ou, si la base tourne ailleurs :
  TEST_DATABASE_URL=postgresql+psycopg://user:pass@hôte:5432/votations_test pytest

Erreur : {error}"""


def _ensure_database(url: str) -> None:
    """Create the test database if needed. Exits with a clear message if unreachable."""
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
    except OperationalError as exc:
        masked = url_obj.set(database=db_name).render_as_string(hide_password=True)
        pytest.exit(UNREACHABLE.format(url=masked, error=exc.orig or exc), returncode=1)
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
    # Start from scratch so a leftover schema can't hide a broken migration.
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    """A session rolled back at the end of the test.

    The app's commits become savepoints inside the outer transaction.
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
    """Session factory on the test engine, for code that opens its own sessions."""
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
    """Gives the test session to code that opens and closes its own.

    Lets the drafting job use the test transaction without closing it.
    """

    def __init__(self, inner: Session) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


_QUEUED: list[tuple] = []


class _DeferredExecutor:
    """Queues jobs instead of running them on another thread.

    Keeps the real order (schedule, respond, then run) so tests can see the
    "en cours" banner. `drain_drafts()` runs the queue.
    """

    def submit(self, fn, *args, **kwargs):
        _QUEUED.append((fn, args, kwargs))
        return Future()


def drain_drafts() -> None:
    """Run the queued drafts."""
    while _QUEUED:
        fn, args, kwargs = _QUEUED.pop(0)
        fn(*args, **kwargs)


@pytest.fixture
def background_db(db_session, monkeypatch):
    """Run background jobs against the test transaction, on this thread."""
    monkeypatch.setattr("app.drafting.SessionLocal", lambda: _NoCloseSession(db_session))
    monkeypatch.setattr("app.drafting._executor", _DeferredExecutor)
    _QUEUED.clear()
    yield db_session
    _QUEUED.clear()


@pytest.fixture(autouse=True)
def _clear_process_state():
    """Reset in-memory state (rate limits, results memo) between tests."""
    from app import ratelimit
    from app.crud import results

    ratelimit.reset()
    results.reset_cache()
    yield
    ratelimit.reset()
    results.reset_cache()
