"""Pytest fixtures: isolated PostgreSQL test database and TestClient.

Tests run against a dedicated database (``votations_test`` by default), created
automatically on first use. Set ``TEST_DATABASE_URL`` to override — e.g. inside
Docker it points at the ``db`` service.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import Law

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://votations:votations@localhost:5432/votations_test",
)


def _ensure_database(url: str) -> None:
    """Create the target database if it does not exist (CREATE DATABASE needs autocommit)."""
    url_obj = make_url(url)
    db_name = url_obj.database
    admin = create_engine(url_obj.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()


_ensure_database(TEST_DATABASE_URL)
_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)


@pytest.fixture
def db_session():
    # Fresh schema per test for full isolation.
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    TestingSession = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()

    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    yield session
    app.dependency_overrides.clear()
    session.close()


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
