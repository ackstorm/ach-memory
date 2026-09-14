import os

import pytest
from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = os.environ.get(
    "MEMORY_TEST_DATABASE_URL",
    "postgresql+psycopg://memory:memory@localhost:5434/memory_test",
)

# A bare `Settings()` 422s without these two required vars; every test session gets them
# unconditionally so it never has to know that. Carries no live Hindsight -- test_contract.py's
# integration param treats this exact placeholder the same as "unset" and skips.
PLACEHOLDER_HINDSIGHT_URL = "http://hindsight.test"
os.environ.setdefault("MEMORY_BACKEND", "fake")
os.environ.setdefault("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("MEMORY_HINDSIGHT_URL", PLACEHOLDER_HINDSIGHT_URL)


def _ensure_database_exists(url: str) -> None:
    """Create the test database if it isn't there yet."""
    target = make_url(url)
    admin_engine = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).first()
            if exists is None:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def engine():
    from memory.models import Base

    _ensure_database_exists(TEST_DATABASE_URL)
    eng = create_engine(TEST_DATABASE_URL)
    # Drop what the database actually holds, not what the current models know.
    existing = MetaData()
    existing.reflect(bind=eng)
    existing.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    transaction = conn.begin()
    factory = sessionmaker(bind=conn, join_transaction_mode="create_savepoint")
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        conn.close()


@pytest.fixture(autouse=True)
def _isolated_fake_backend():
    from memory.backend import get_backend
    from memory.backend.fake import fake_backend

    get_backend.cache_clear()
    fake_backend.reset()
    yield
    get_backend.cache_clear()
