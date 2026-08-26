import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# A DIFFERENT database than docker-compose gives the `api` container
# (postgresql://.../memory) -- on purpose. This suite's Base.metadata.drop_all
# below previously ran against that same `memory` database: `docker compose up
# -d postgres` plus a bare `uv run pytest` deleted a running dev stack's data
# mid-session, then silently reverted its schema to create_all's shape while
# leaving alembic_version at head, so a later `alembic upgrade head` looked
# like a no-op and fixed nothing. Isolating the test database means the two
# can never collide, whether or not the compose stack happens to be up.
TEST_DATABASE_URL = os.environ.get(
    "MEMORY_TEST_DATABASE_URL",
    "postgresql+psycopg://memory:memory@localhost:5433/memory_test",
)

MASTER_PLAINTEXT = "mem_master_secret_for_tests"


@pytest.fixture(autouse=True, scope="session")
def _default_settings_env():
    """Baseline so `Settings()` can construct for any test.

    `provenance.build` reads `get_settings().max_content_bytes` (task 16's
    metadata size cap) -- some tests (test_provenance.py, test_content_caps.py)
    call `build()` directly with no `app`/`client` fixture, so without this
    the required `MEMORY_DATABASE_URL`/`MEMORY_MASTER_KEY_HASH`/
    `MEMORY_HINDSIGHT_URL` fields are simply missing and construction 422s.
    `os.environ.setdefault` so the `app` fixture's function-scoped
    `monkeypatch.setenv` (the real values) still takes precedence per test.
    """
    os.environ.setdefault("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    os.environ.setdefault("MEMORY_MASTER_KEY_HASH", "unused")
    os.environ.setdefault("MEMORY_HINDSIGHT_URL", "http://hindsight.test")


def _ensure_database_exists(url: str) -> None:
    """Create the test database if it isn't there yet.

    docker-compose's postgres service only provisions `memory` (via
    POSTGRES_DB) -- nothing creates `memory_test` on its own. Connects to the
    server's default `postgres` maintenance database (same server, same
    credentials) to check and, if needed, create it.
    """
    target = make_url(url)
    admin_engine = create_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
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
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine):
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


@pytest.fixture
def session(connection) -> Session:
    """A Session for tests that talk to the database directly.

    join_transaction_mode="create_savepoint" is required, not decoration: with
    the default mode, a Session that rolls back internally — which any
    IntegrityError test does — deassociates the outer Transaction, so the
    rollback would silently become a no-op and rows would survive the test.
    """
    factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    db = factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def tenant(session) -> str:
    from memory.models import Tenant

    session.add(Tenant(id="default"))
    session.flush()
    return "default"


@pytest.fixture
def seeded_activity(session, tenant) -> None:
    """Two rows for `tenant`'s project `alpha`, plus one for a second
    tenant so the tenant filter in activity.py is genuinely exercised.

    Explicit `created_at` (not the server default) so the retain is
    reliably ordered before the recall, and so both land in the summary's
    current hour bucket.
    """
    from datetime import UTC, datetime, timedelta

    from memory import ids
    from memory.models import ActivityEvent, Tenant

    session.add(Tenant(id="other-tenant"))
    session.flush()

    now = datetime.now(UTC)
    session.add_all(
        [
            ActivityEvent(
                id=ids.new_activity_id(),
                tenant_id=tenant,
                action="memory.retain",
                surface="rest",
                scope="project",
                project_slug="alpha",
                bank_fingerprint="fp-alpha",
                content_bytes=10,
                outcome="ok",
                duration_ms=5,
                created_at=now,
            ),
            ActivityEvent(
                id=ids.new_activity_id(),
                tenant_id=tenant,
                action="memory.recall",
                surface="rest",
                scope="project",
                project_slug="alpha",
                bank_fingerprint="fp-alpha",
                outcome="ok",
                duration_ms=5,
                created_at=now + timedelta(minutes=1),
            ),
            ActivityEvent(
                id=ids.new_activity_id(),
                tenant_id="other-tenant",
                action="memory.retain",
                surface="rest",
                scope="project",
                project_slug="beta",
                bank_fingerprint="fp-beta",
                content_bytes=5,
                outcome="ok",
                duration_ms=5,
                created_at=now,
            ),
        ]
    )
    session.flush()


@pytest.fixture
def app(connection, session, monkeypatch):
    from memory import db, ratelimit
    from memory.api.app import create_app
    from memory.auth import keys
    from memory.config import get_settings
    from memory.hindsight.client import get_client

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", keys.hash_key(MASTER_PLAINTEXT))
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    get_settings.cache_clear()
    get_client.cache_clear()
    # `ratelimit.get_limiter` is process-lifetime `lru_cache`d, same as
    # get_settings/get_client above -- without clearing it here, every test
    # in the session would share one Limiter (and, worse, one master-key
    # bucket), so an earlier test's writes could trip a later test's limit.
    ratelimit.get_limiter.cache_clear()

    factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")

    # `memory.db.session_scope` (used by the MCP pipeline, which has no
    # FastAPI dependency to override) calls `_session_factory()` directly. A
    # real `_session_factory()` would open a brand-new connection -- a
    # different transaction than the one `_request_session` above joins via
    # savepoint -- so anything an MCP test creates through `client` would be
    # invisible to it (uncommitted on a connection nothing else can see).
    # Patching the module-level factory itself, not just the FastAPI
    # dependency, keeps both paths on the one connection/transaction.
    monkeypatch.setattr(db, "_session_factory", lambda: factory)

    def _request_session():
        """One Session per request, exactly like production.

        All of them join the test's outer transaction through a savepoint, so a
        handler's commit() releases its savepoint and is visible to later
        requests, while the fixture's final rollback still discards everything.
        """
        db_session = factory()
        try:
            yield db_session
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.close()

    application = create_app()
    application.dependency_overrides[db.get_session] = _request_session
    yield application
    get_settings.cache_clear()
    get_client.cache_clear()
    ratelimit.get_limiter.cache_clear()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def master_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MASTER_PLAINTEXT}"}


@pytest.fixture
def configured_env(monkeypatch):
    """Minimum configuration for `create_app()` to build.

    It reads settings now (the MCP mount needs its allowed-host list), so a
    test that constructs the app without going through the `app` fixture has
    to supply them. Nothing here talks to a database or to Hindsight.
    """
    from memory.auth import keys
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", keys.hash_key(MASTER_PLAINTEXT))
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
