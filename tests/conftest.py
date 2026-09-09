import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# A different SERVER than docker-compose's, on its own port -- not merely a
# different database name on the same one. Both defaulted to 5433, so whichever
# container held the port served the suite, and restarting the compose stack
# mid-run swapped the database out from under it: every table vanished and the
# suite reported `relation "tenants" does not exist` against a `memory_test`
# that had been created inside the other server. `make testdb` starts this one.
#
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
    "postgresql+psycopg://memory:memory@localhost:5434/memory_test",
)

# The suite authenticates the way production does: through an external
# identity provider. There is no local credential left to mint, so the
# platform provider is wired to a resolver that never leaves the process.
#
# A token IS the identity: `subject#group,group`. Stateless and deterministic
# on purpose -- a registry dict would leak identities between tests and would
# have to be reset in a fixture nobody remembers to request.
IDENTITY_HEADER = "x-ach-identity"
RESOLVER_URL = "http://identity.test/whoami"
OPERATOR_SUBJECT = "operator@test"


def identity_token(subject: str, groups: tuple[str, ...] = ()) -> str:
    return f"{subject}#{','.join(groups)}" if groups else subject


def _fake_resolve(token: str) -> tuple[str, frozenset[str]]:
    """Stand in for `platform._resolve`, the one HTTP call on the auth path.

    Patched at the resolver rather than mocked over HTTP so that
    `platform.authenticate` still runs for real -- `link_identity`, the
    `User` row and the credential id are all genuine, and only the network
    hop is replaced.
    """
    subject, _, raw_groups = token.partition("#")
    return subject, frozenset(g for g in raw_groups.split(",") if g)


@pytest.fixture(autouse=True, scope="session")
def _default_settings_env():
    """Baseline so `Settings()` can construct for any test.

    Some tests call service helpers directly with no `app`/`client` fixture,
    so without this the required `MEMORY_DATABASE_URL`/`MEMORY_MASTER_KEY_HASH`/
    `MEMORY_HINDSIGHT_URL` fields are simply missing and construction 422s.
    `os.environ.setdefault` so the `app` fixture's function-scoped
    `monkeypatch.setenv` (the real values) still takes precedence per test.
    """
    os.environ.setdefault("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
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
    from memory import bootstrap as bootstrap_service
    from memory import db, mental_model_service, ratelimit, retention
    from memory.api.app import create_app
    from memory.auth.providers import platform
    from memory.config import get_settings
    from memory.hindsight.client import get_client

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    # The suite's identity provider. Every authenticated test caller arrives
    # through the same platform path production uses; only `_resolve`'s HTTP
    # hop is replaced, below.
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "1")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", IDENTITY_HEADER)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "x-resolver-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_URL", RESOLVER_URL)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "groups")
    # Operator authority is configuration now, so the suite's operator is
    # named here rather than carried by a credential.
    monkeypatch.setenv("MEMORY_MASTER_USERS", OPERATOR_SUBJECT)
    monkeypatch.setattr(platform, "_resolve", _fake_resolve)
    platform.reset_cache()
    get_settings.cache_clear()
    get_client.cache_clear()
    # `ratelimit.get_limiter` is process-lifetime `lru_cache`d, same as
    # get_settings/get_client above -- without clearing it here, every test
    # in the session would share one Limiter (and, worse, one master-key
    # bucket), so an earlier test's writes could trip a later test's limit.
    ratelimit.get_limiter.cache_clear()

    # API/MCP contract tests mock only the upstream operation they exercise.
    # Strategy provisioning has its own unit and live coverage; keep it out
    # of every unrelated route test so their respx expectations remain
    # focused on that route's contract.
    monkeypatch.setattr(retention, "ensure_exact_retain_strategy", lambda *_args: None)
    monkeypatch.setattr(
        bootstrap_service, "ensure_exact_retain_strategy", lambda *_args: None
    )
    # Same reason, extended to builtin-model registration: since Task 1 made
    # provisioning failure fatal instead of best-effort, every route test that
    # creates a user or project now provisions for real, and reconcile_builtin
    # is the one step here that still makes a live-shaped Hindsight call. Tests
    # about provisioning itself (test_bootstrap.py, test_bootstrap_api.py)
    # restore the real function via this same `monkeypatch`.
    monkeypatch.setattr(
        mental_model_service, "reconcile_builtin", lambda *_args, **_kwargs: None
    )

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
    """An operator: an ordinary external identity that configuration grants
    authority to. `MEMORY_MASTER_USERS` names it in the `app` fixture.

    The name is kept from when this was a minted master key, so the ~27 test
    files that request it need no edit -- what changed is only what makes the
    caller an operator, not that they are one.
    """
    return {IDENTITY_HEADER: OPERATOR_SUBJECT}


def create_user(client, session, *, groups: tuple[str, ...] = ()) -> dict:
    """A distinct external user, provisioned the way a real first-time caller
    is: the first authenticated request links the identity and creates the
    `User` row, and `POST /v1/bootstrap` provisions its bank -- exactly what
    the proxy's pre-warm does before an agent's first prompt.

    Replaces the old `POST /v1/users` + `POST /v1/users/{id}/keys` pair. Under
    external identity nobody mints a user: a user exists because an IdP
    asserted them, so there is no route to call and no key to hand back.

    Returns `subject` (what the IdP calls them, and what `MEMORY_MASTER_USERS`
    would name), `user_id` (minted locally by `link_identity`, knowable only
    after first sight), `groups` and ready-to-send `headers`.
    """
    from memory.models import ExternalIdentity

    subject = f"user-{uuid.uuid4().hex[:12]}@test"
    headers = {IDENTITY_HEADER: identity_token(subject, groups)}
    client.post("/v1/bootstrap", json={}, headers=headers)
    row = session.get(ExternalIdentity, (RESOLVER_URL, subject))
    return {
        "subject": subject,
        "user_id": row.user_id if row is not None else None,
        "groups": groups,
        "headers": headers,
    }


@pytest.fixture
def new_user(client, session, tenant):
    """Factory for tests that need more than the two `two_users` gives, or
    that need a user holding a particular group."""

    def _make(*, groups: tuple[str, ...] = ()) -> dict:
        return create_user(client, session, groups=groups)

    return _make


@pytest.fixture
def two_users(client, session, tenant):
    return [create_user(client, session) for _ in range(2)]


@pytest.fixture
def configured_env(monkeypatch):
    """Minimum configuration for `create_app()` to build.

    It reads settings now (the MCP mount needs its allowed-host list), so a
    test that constructs the app without going through the `app` fixture has
    to supply them. Nothing here talks to a database or to Hindsight.
    """
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
