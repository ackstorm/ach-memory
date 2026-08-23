# Plan 1 — User Memory Vertical Slice: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A holder of a user API key can `retain` and `recall` against their own Hindsight memory bank, through our wrapper, with the bank materialized lazily and its ID never leaving the service.

**Architecture:** A synchronous FastAPI service in front of Hindsight. Postgres holds tenants, users and API keys. Every request resolves a `Principal` from the bearer token, maps `scope=user` to a persisted opaque `bank_id`, and proxies to Hindsight over HTTP. The bank is allocated in our DB at user creation and materialized in Hindsight on first use. This slice deliberately implements only `scope=user` — projects, groups and ownership are Plan 2.

**Tech Stack:** Python 3.12 · uv · FastAPI · Pydantic v2 + pydantic-settings · SQLAlchemy 2.0 (sync) + psycopg 3 · Alembic · httpx (sync) · pytest + respx · Docker (multi-stage) + docker compose for Postgres.

## Global Constraints

Copied from `SPEC-v1.md` rev. 5. Every task's requirements implicitly include this section.

- **Synchronous stack throughout.** No `async def` endpoints, no asyncpg, no async SQLAlchemy. A v1 wrapper at this QPS gains nothing from async and pays for it in test fixtures and debugging. Revisit only with a measured bottleneck.
- **Never install outside a virtualenv.** All dependency work goes through `uv`. A bare `pip install` is an error.
- **`bank_id` never crosses the API boundary** in either direction (§23 invariant 29). It appears in no response body, no error message and no log line at INFO or above.
- **The client never sends its own identity.** `user_id` is derived from the API key, never read from the request body, except on master-key requests which must name their target explicitly (§5.2).
- **Master key is a configured hash.** `MEMORY_MASTER_KEY_HASH`. The plaintext master secret is never written to the database (§5.2).
- **User key plaintext is returned exactly once**, at creation (§5.3).
- **Mono-tenant.** `MEMORY_TENANT_ID` defaults to `default`, which is also Hindsight's `{tenant}` path segment (§4.1, §19.1).
- **Bank IDs are `user_<uuid4>` / `project_<uuid4>`** and encode nothing else (§4.7).
- **No bank tuning.** Banks are created on Hindsight's stock configuration. `memory_defense` is the only field v1 sets (§19.5).
- **`retain` is async by default**; `sync_retain` is the blocking variant (§15).
- **Content size is capped** and over-size input is rejected with `CONTENT_TOO_LARGE` (§20 MUST).
- **Docker:** multi-stage builds, explicit `COPY` paths only. `COPY . .` is forbidden.
- All code, comments, commit messages and docs in English.

## File Structure

```
ach-memory/
  pyproject.toml               deps, pytest + ruff config
  .dockerignore
  docker-compose.yml           our Postgres only; Hindsight runs via uvx
  Dockerfile                   multi-stage, explicit COPY
  alembic.ini
  migrations/env.py
  migrations/versions/         generated revisions
  src/memory/
    config.py                  Settings (env, MEMORY_ prefix)
    ids.py                     ID and bank-ID generation
    errors.py                  domain errors + HTTP mapping
    db.py                      engine, session factory, get_session dep
    models.py                  Tenant, User, ApiKey
    auth/keys.py               generate / hash / verify API keys
    auth/principal.py          Principal + resolve_principal dependency
    hindsight/paths.py         Hindsight endpoint paths, pinned to openapi.json
    hindsight/client.py        HindsightClient: retain, recall, ensure_bank
    api/app.py                 FastAPI factory, exception handlers
    api/users.py               POST /v1/users, POST /v1/users/{id}/keys
    api/memory.py              POST /v1/memory/{retain,recall}
  tests/
    conftest.py                DB fixture, app fixture, key fixtures
    test_config.py
    test_ids.py
    test_keys.py
    test_models.py
    test_principal.py
    test_users_api.py
    test_hindsight_client.py
    test_memory_api.py
    test_integration_hindsight.py   marked; needs a live Hindsight
```

`auth/` splits key crypto from principal resolution because the first is pure
and heavily tested and the second needs the database. `hindsight/paths.py` is
separate from `client.py` because the paths are pinned to a discovered
`openapi.json` (Task 6) and will be the first thing to change on a Hindsight
upgrade.

---

### Task 1: Project skeleton and settings

**Files:**
- Create: `pyproject.toml`
- Create: `src/memory/__init__.py`
- Create: `src/memory/config.py`
- Create: `.gitignore`
- Create: `.dockerignore`
- Test: `tests/test_config.py`
- Test: `tests/__init__.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `memory.config.Settings` with fields `database_url: str`,
  `master_key_hash: str`, `hindsight_url: str`, `hindsight_api_key: str`,
  `tenant_id: str`, `max_content_bytes: int`; and
  `memory.config.get_settings() -> Settings` (cached).

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "ach-memory"
version = "0.1.0"
description = "Multi-tenant memory service for coding agents, over Hindsight"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "sqlalchemy>=2.0.36",
    "psycopg[binary]>=3.2",
    "alembic>=1.14",
    "httpx>=0.28",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "respx>=0.22",
    "ruff>=0.8",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/memory"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = [
    "integration: requires a live Hindsight on MEMORY_HINDSIGHT_URL",
]

[tool.ruff]
line-length = 100
```

- [ ] **Step 2: Create `.gitignore` and `.dockerignore`**

`.gitignore`:

```text
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.superpowers/
```

`uv.lock` is deliberately **not** ignored: this is an application, not a
library, and the image build installs from the lock to stay reproducible
(Task 8).

`.dockerignore`:

```text
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
.git/
tests/
docs/
```

- [ ] **Step 3: Create the virtualenv and install**

```bash
cd ach-memory
uv venv
uv sync --dev
```

Expected: `.venv/` created, packages resolved, no error.

- [ ] **Step 4: Write the failing test**

Create `tests/__init__.py` (empty) and `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from memory.config import Settings

REQUIRED = {
    "MEMORY_DATABASE_URL": "postgresql+psycopg://memory:memory@localhost:5432/memory",
    "MEMORY_MASTER_KEY_HASH": "0" * 64,
    "MEMORY_HINDSIGHT_URL": "http://localhost:8888",
}


def _clear(monkeypatch):
    for key in list(REQUIRED) + ["MEMORY_TENANT_ID", "MEMORY_MAX_CONTENT_BYTES"]:
        monkeypatch.delenv(key, raising=False)


def test_settings_read_from_environment(monkeypatch):
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    settings = Settings()

    assert settings.database_url == REQUIRED["MEMORY_DATABASE_URL"]
    assert settings.hindsight_url == "http://localhost:8888"


def test_tenant_id_defaults_to_hindsight_default_segment(monkeypatch):
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    assert Settings().tenant_id == "default"


def test_missing_required_setting_fails_loudly(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMORY_DATABASE_URL", REQUIRED["MEMORY_DATABASE_URL"])

    with pytest.raises(ValidationError):
        Settings()
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.config'`

- [ ] **Step 6: Write the implementation**

Create `src/memory/__init__.py` (empty) and `src/memory/config.py`:

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration. All variables use the MEMORY_ prefix."""

    model_config = SettingsConfigDict(env_prefix="MEMORY_", extra="ignore")

    database_url: str
    master_key_hash: str
    hindsight_url: str
    hindsight_api_key: str = ""

    # Mono-tenant in v1. This value is also Hindsight's {tenant} path segment.
    tenant_id: str = "default"

    max_content_bytes: int = 256_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS — 3 passed

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore .dockerignore src/memory/__init__.py src/memory/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: project skeleton and settings"
```

---

### Task 2: Identifiers and API key crypto

**Files:**
- Create: `src/memory/ids.py`
- Create: `src/memory/auth/__init__.py`
- Create: `src/memory/auth/keys.py`
- Test: `tests/test_ids.py`
- Test: `tests/test_keys.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `memory.ids.new_user_id() -> str` (`usr_<32 hex>`)
  - `memory.ids.new_key_id() -> str` (`key_<32 hex>`)
  - `memory.ids.new_user_bank_id() -> str` (`user_<uuid4>`)
  - `memory.ids.new_project_bank_id() -> str` (`project_<uuid4>`)
  - `memory.auth.keys.KEY_PREFIX` (`"mem_"`)
  - `memory.auth.keys.generate_key() -> str`
  - `memory.auth.keys.hash_key(plaintext: str) -> str` (64-char hex)
  - `memory.auth.keys.verify_key(plaintext: str, stored_hash: str) -> bool`

- [ ] **Step 1: Write the failing tests**

`tests/test_ids.py`:

```python
import re

from memory import ids


def test_user_id_shape():
    assert re.fullmatch(r"usr_[0-9a-f]{32}", ids.new_user_id())


def test_key_id_shape():
    assert re.fullmatch(r"key_[0-9a-f]{32}", ids.new_key_id())


def test_bank_ids_carry_only_a_type_prefix():
    assert re.fullmatch(
        r"user_[0-9a-f-]{36}", ids.new_user_bank_id()
    )
    assert re.fullmatch(
        r"project_[0-9a-f-]{36}", ids.new_project_bank_id()
    )


def test_ids_are_unique():
    generated = {ids.new_user_bank_id() for _ in range(1000)}
    assert len(generated) == 1000
```

`tests/test_keys.py`:

```python
from memory.auth import keys


def test_generated_key_has_prefix_and_entropy():
    key = keys.generate_key()
    assert key.startswith("mem_")
    assert len(key) > 40


def test_generated_keys_are_unique():
    assert len({keys.generate_key() for _ in range(1000)}) == 1000


def test_hash_is_hex_sha256():
    digest = keys.hash_key("mem_abc")
    assert len(digest) == 64
    assert int(digest, 16) >= 0


def test_verify_accepts_the_right_key():
    key = keys.generate_key()
    assert keys.verify_key(key, keys.hash_key(key)) is True


def test_verify_rejects_a_different_key():
    stored = keys.hash_key(keys.generate_key())
    assert keys.verify_key(keys.generate_key(), stored) is False


def test_hash_never_equals_plaintext():
    key = keys.generate_key()
    assert keys.hash_key(key) != key
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ids.py tests/test_keys.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.ids'`

- [ ] **Step 3: Write `src/memory/ids.py`**

```python
import uuid


def new_user_id() -> str:
    return f"usr_{uuid.uuid4().hex}"


def new_group_id() -> str:
    return f"grp_{uuid.uuid4().hex}"


def new_key_id() -> str:
    return f"key_{uuid.uuid4().hex}"


def new_user_bank_id() -> str:
    """Opaque bank ID. The prefix is a diagnostic hint and nothing more:
    it must never encode tenant, user, project or repository names (SPEC §4.7).
    """
    return f"user_{uuid.uuid4()}"


def new_project_bank_id() -> str:
    return f"project_{uuid.uuid4()}"
```

- [ ] **Step 4: Write `src/memory/auth/keys.py`**

Create `src/memory/auth/__init__.py` (empty), then:

```python
import hashlib
import hmac
import secrets

KEY_PREFIX = "mem_"


def generate_key() -> str:
    """A 256-bit random API key. Returned to the caller exactly once."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(plaintext: str) -> str:
    """SHA-256, deliberately not a slow KDF.

    A slow KDF (bcrypt, argon2) exists to make brute force expensive against
    *low-entropy* secrets. These keys carry 256 bits of entropy from
    secrets.token_urlsafe, so brute force is already infeasible, and a slow
    hash would add its cost to every single authenticated request.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def verify_key(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(plaintext), stored_hash)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ids.py tests/test_keys.py -v`
Expected: PASS — 10 passed

- [ ] **Step 6: Commit**

```bash
git add src/memory/ids.py src/memory/auth/__init__.py src/memory/auth/keys.py tests/test_ids.py tests/test_keys.py
git commit -m "feat: identifier generation and API key crypto"
```

---

### Task 3: Database models and migration

**Files:**
- Create: `src/memory/db.py`
- Create: `src/memory/models.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `docker-compose.yml`
- Test: `tests/conftest.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `memory.config.get_settings`, `memory.ids`.
- Produces:
  - `memory.models.Base`, `Tenant`, `User`, `ApiKey`
  - `memory.db.get_engine()` and `memory.db.get_session()` (FastAPI dependency
    yielding `Session`). The session factory stays private (`_session_factory`):
    callers depend on `get_session`, which owns commit/rollback.

- [ ] **Step 1: Create `docker-compose.yml` for our Postgres**

Hindsight is not in compose — it runs via `uvx` in Task 6. This file owns only
our own database.

```yaml
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: memory
      POSTGRES_PASSWORD: memory
      POSTGRES_DB: memory
    ports:
      - "5433:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U memory"]
      interval: 2s
      timeout: 3s
      retries: 15
    volumes:
      - memory-pgdata:/var/lib/postgresql/data

volumes:
  memory-pgdata:
```

Port 5433 on the host avoids colliding with any local Postgres.

- [ ] **Step 2: Start Postgres and wait for it**

```bash
docker compose up -d postgres
for i in $(seq 1 15); do
  docker compose exec -T postgres pg_isready -U memory && break
  sleep 2
done
docker compose exec -T postgres pg_isready -U memory || { echo "FAIL: postgres never ready" >&2; exit 1; }
```

Expected: `accepting connections`

- [ ] **Step 3: Write the failing test**

`tests/conftest.py`:

```python
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

TEST_DATABASE_URL = os.environ.get(
    "MEMORY_TEST_DATABASE_URL",
    "postgresql+psycopg://memory:memory@localhost:5433/memory",
)


@pytest.fixture(scope="session")
def engine():
    from memory.models import Base

    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Session:
    """Each test runs in a transaction that is rolled back afterwards.

    join_transaction_mode="create_savepoint" is required, not decoration: with
    the default mode, a Session that rolls back internally — which any
    IntegrityError test does — deassociates the outer Transaction, so the
    rollback below silently becomes a no-op and rows survive the test.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def tenant(session) -> str:
    from memory.models import Tenant

    session.add(Tenant(id="default"))
    session.flush()
    return "default"
```

`tests/test_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from memory import ids
from memory.auth import keys
from memory.models import ApiKey, User


def test_user_persists_with_its_bank_id(session, tenant):
    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()

    stored = session.get(User, user.id)
    assert stored.bank_id.startswith("user_")


def test_bank_id_is_unique(session, tenant):
    bank_id = ids.new_user_bank_id()
    session.add(User(id=ids.new_user_id(), tenant_id=tenant, bank_id=bank_id))
    session.flush()
    session.add(User(id=ids.new_user_id(), tenant_id=tenant, bank_id=bank_id))

    with pytest.raises(IntegrityError):
        session.flush()


def test_api_key_stores_only_a_hash(session, tenant):
    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()

    plaintext = keys.generate_key()
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=tenant,
            user_id=user.id,
            secret_hash=keys.hash_key(plaintext),
        )
    )
    session.flush()

    stored = session.query(ApiKey).one()
    assert stored.secret_hash != plaintext
    assert stored.status == "active"


def test_api_key_row_without_a_user_is_rejected(session, tenant):
    """The master key is configuration, never a row (SPEC §5.2). A user-less
    row must be impossible at the schema level, because principal resolution
    would otherwise have to decide what it means."""
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=tenant,
            user_id=None,
            secret_hash=keys.hash_key(keys.generate_key()),
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.models'`

- [ ] **Step 5: Write `src/memory/models.py`**

```python
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)


class User(Base):
    __tablename__ = "users"

    # Externally supplied (ACH) or service-generated. SPEC §4.2.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # Allocated here; materialized in Hindsight on first use. SPEC §19.2.
    bank_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NOT NULL on purpose: every row here is a user key. The bootstrap master
    # key is configuration, never a row (SPEC §5.2), so a user-less row is not
    # a legitimate state — and if one existed, principal resolution must never
    # be able to read it as "this key is the master key".
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
```

- [ ] **Step 6: Write `src/memory/db.py`**

```python
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from memory.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Rolls back on exception; never commits.

    Committing in the teardown would run AFTER the response has been sent —
    FastAPI awaits the response inside the dependency exit stack — so a failed
    commit could not be reported to a caller who already holds, say, a
    plaintext API key. Write handlers commit explicitly, before they return.
    """
    db = _session_factory()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS — 4 passed

- [ ] **Step 8: Initialise Alembic and generate the first migration**

```bash
uv run alembic init -t generic migrations
```

Then replace the bottom of `migrations/env.py` target metadata wiring — set:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory.models import Base  # noqa: E402

target_metadata = Base.metadata
```

and in `alembic.ini` set:

```ini
sqlalchemy.url = postgresql+psycopg://memory:memory@localhost:5433/memory
```

Generate and apply:

```bash
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head
```

Expected: a file under `migrations/versions/` creating `tenants`, `users`,
`api_keys`; `upgrade head` completes without error.

- [ ] **Step 9: Verify the migration matches the models**

```bash
uv run alembic revision --autogenerate -m "drift check"
```

Expected: the generated file's `upgrade()` body is `pass` — meaning no drift.
Delete it:

```bash
rm migrations/versions/*drift_check.py
```

- [ ] **Step 10: Commit**

```bash
git add docker-compose.yml alembic.ini migrations src/memory/db.py src/memory/models.py tests/conftest.py tests/test_models.py
git commit -m "feat: database models, session handling and initial migration"
```

---

### Task 4: Principal resolution

**Files:**
- Create: `src/memory/errors.py`
- Create: `src/memory/auth/principal.py`
- Test: `tests/test_principal.py`

**Interfaces:**
- Consumes: `memory.auth.keys`, `memory.models`, `memory.config`.
- Produces:
  - `memory.errors.DomainError(code: str, message: str, status: int, details: dict)`
    and subclasses `Unauthorized`, `Forbidden`, `InvalidScope`,
    `ContentTooLarge`, `HindsightError`
  - `memory.auth.principal.Principal` — frozen dataclass with
    `tenant_id: str`, `user_id: str | None`, `is_master: bool`, `key_id: str | None`
  - `memory.auth.principal.resolve_principal(authorization: str | None, db: Session) -> Principal`

- [ ] **Step 1: Write the failing test**

`tests/test_principal.py`:

```python
import pytest

from memory import ids
from memory.auth import keys
from memory.auth.principal import resolve_principal
from memory.errors import Unauthorized
from memory.models import ApiKey, User

MASTER_PLAINTEXT = "mem_master_secret_for_tests"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", keys.hash_key(MASTER_PLAINTEXT))
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://localhost:8888")
    from memory.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_user_key(session, tenant) -> tuple[User, str]:
    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    plaintext = keys.generate_key()
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=tenant,
            user_id=user.id,
            secret_hash=keys.hash_key(plaintext),
        )
    )
    session.flush()
    return user, plaintext


def test_master_key_resolves_to_a_tenant_only_principal(session, tenant):
    principal = resolve_principal(f"Bearer {MASTER_PLAINTEXT}", session)

    assert principal.is_master is True
    assert principal.user_id is None
    assert principal.tenant_id == tenant


def test_user_key_resolves_to_its_user(session, tenant):
    user, plaintext = _make_user_key(session, tenant)

    principal = resolve_principal(f"Bearer {plaintext}", session)

    assert principal.is_master is False
    assert principal.user_id == user.id


def test_unknown_key_is_unauthorized(session, tenant):
    with pytest.raises(Unauthorized):
        resolve_principal(f"Bearer {keys.generate_key()}", session)


def test_revoked_key_is_unauthorized(session, tenant):
    _, plaintext = _make_user_key(session, tenant)
    session.query(ApiKey).update({"status": "revoked"})
    session.flush()

    with pytest.raises(Unauthorized):
        resolve_principal(f"Bearer {plaintext}", session)


def test_missing_header_is_unauthorized(session, tenant):
    with pytest.raises(Unauthorized):
        resolve_principal(None, session)


def test_non_bearer_header_is_unauthorized(session, tenant):
    with pytest.raises(Unauthorized):
        resolve_principal(f"Basic {MASTER_PLAINTEXT}", session)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_principal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.errors'`

- [ ] **Step 3: Write `src/memory/errors.py`**

```python
class DomainError(Exception):
    """Base for every error the API reports with a stable code (SPEC §18)."""

    code = "INTERNAL_ERROR"
    status = 500

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class Unauthorized(DomainError):
    code = "UNAUTHORIZED"
    status = 401


class Forbidden(DomainError):
    code = "FORBIDDEN"
    status = 403


class InvalidScope(DomainError):
    code = "INVALID_SCOPE"
    status = 400


class ContentTooLarge(DomainError):
    code = "CONTENT_TOO_LARGE"
    status = 413


class HindsightError(DomainError):
    code = "HINDSIGHT_ERROR"
    status = 502
```

- [ ] **Step 4: Write `src/memory/auth/principal.py`**

```python
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth import keys
from memory.config import get_settings
from memory.errors import Unauthorized
from memory.models import ApiKey

BEARER = "Bearer "


@dataclass(frozen=True)
class Principal:
    """Who is calling, derived only from the credential (SPEC §2.3)."""

    tenant_id: str
    user_id: str | None
    is_master: bool
    key_id: str | None


def resolve_principal(authorization: str | None, db: Session) -> Principal:
    if not authorization or not authorization.startswith(BEARER):
        raise Unauthorized("missing or malformed Authorization header")

    plaintext = authorization[len(BEARER) :].strip()
    settings = get_settings()

    # The bootstrap master key is configuration, never a database row (§5.2).
    if keys.verify_key(plaintext, settings.master_key_hash):
        return Principal(
            tenant_id=settings.tenant_id, user_id=None, is_master=True, key_id=None
        )

    row = db.execute(
        select(ApiKey).where(ApiKey.secret_hash == keys.hash_key(plaintext))
    ).scalar_one_or_none()

    if row is None or row.status != "active":
        raise Unauthorized("unknown or revoked API key")

    # A stored key is always a user key: is_master is a constant here, never
    # derived from a column. Deriving it would mean a single bad row could
    # mint tenant-wide authority.
    return Principal(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        is_master=False,
        key_id=row.id,
    )
```

The database lookup is by hash equality on an indexed column, so an unknown key
costs one index probe and never compares against every stored row.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_principal.py -v`
Expected: PASS — 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/memory/errors.py src/memory/auth/principal.py tests/test_principal.py
git commit -m "feat: principal resolution from bearer credentials"
```

---

### Task 5: Users and keys provisioning API

**Files:**
- Create: `src/memory/api/__init__.py`
- Create: `src/memory/api/app.py`
- Create: `src/memory/api/users.py`
- Modify: `tests/conftest.py` (add `client` and credential fixtures)
- Test: `tests/test_users_api.py`

**Interfaces:**
- Consumes: `memory.auth.principal.resolve_principal`, `memory.db.get_session`,
  `memory.errors`, `memory.ids`, `memory.models`.
- Produces:
  - `memory.api.app.create_app() -> FastAPI`
  - `memory.api.app.current_principal` — FastAPI dependency returning `Principal`
  - `memory.api.app.require_master(principal) -> Principal`
  - Routes: `POST /v1/users`, `GET /v1/users/{user_id}`,
    `POST /v1/users/{user_id}/keys`

- [ ] **Step 1: Extend `tests/conftest.py`**

Append to the existing file:

```python
@pytest.fixture
def app(session, monkeypatch):
    from memory import db
    from memory.api.app import create_app
    from memory.auth import keys
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", keys.hash_key(MASTER_PLAINTEXT))
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    get_settings.cache_clear()

    application = create_app()
    application.dependency_overrides[db.get_session] = lambda: session
    yield application
    get_settings.cache_clear()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def master_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MASTER_PLAINTEXT}"}
```

and at the top of the file add:

```python
MASTER_PLAINTEXT = "mem_master_secret_for_tests"
```

`fastapi.testclient` needs `httpx`, already a dependency.

- [ ] **Step 2: Write the failing test**

`tests/test_users_api.py`:

```python
def test_master_creates_a_user_with_a_generated_id(client, master_headers, tenant):
    response = client.post("/v1/users", json={}, headers=master_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"].startswith("usr_")


def test_master_creates_a_user_with_an_explicit_id(client, master_headers, tenant):
    response = client.post(
        "/v1/users", json={"id": "ach-user-82f"}, headers=master_headers
    )

    assert response.status_code == 201
    assert response.json()["user_id"] == "ach-user-82f"


def test_user_response_never_exposes_the_bank_id(client, master_headers, tenant):
    body = client.post("/v1/users", json={}, headers=master_headers).json()

    assert "bank_id" not in body
    assert not any("user_" in str(v) for k, v in body.items() if k != "user_id")


def test_creating_a_user_requires_the_master_key(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.post(
        "/v1/users", json={}, headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_key_creation_returns_the_plaintext_once(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]

    created = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()
    listed = client.get(f"/v1/users/{user_id}", headers=master_headers).json()

    assert created["key"].startswith("mem_")
    assert "key" not in str(listed)


def test_unauthenticated_request_is_401(client, tenant):
    assert client.post("/v1/users", json={}).status_code == 401
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_users_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.api'`

- [ ] **Step 4: Write `src/memory/api/app.py`**

Create `src/memory/api/__init__.py` (empty), then:

```python
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, resolve_principal
from memory.db import get_session
from memory.errors import DomainError, Forbidden


def current_principal(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_session),
) -> Principal:
    return resolve_principal(authorization, db)


def require_master(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_master:
        raise Forbidden("this operation requires the master key")
    return principal


def create_app() -> FastAPI:
    from memory.api import users as user_routes

    app = FastAPI(title="ach-memory", version="0.1.0")

    @app.exception_handler(DomainError)
    def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    **({"details": exc.details} if exc.details else {}),
                }
            },
        )

    @app.exception_handler(Exception)
    def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        """Last resort, so the error envelope is a contract and not a hope.

        Without this, anything that is not a DomainError — a driver
        IntegrityError, a bug — escapes to Starlette's default handler and the
        client gets plain text instead of {"error": {...}}. The message is
        deliberately fixed: never echo the exception, which can carry SQL,
        a connection string, or a bank ID.
        """
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "internal error"}},
        )

    app.include_router(user_routes.router)
    return app
```

Add at the top of the module:

```python
import logging

logger = logging.getLogger("memory.api")
```

Only the users router exists at this point. Task 7 creates
`src/memory/api/memory.py` and adds its `include_router` line here. Do **not**
create an empty placeholder module now — scaffolding for a later task is dead
code until that task lands.

- [ ] **Step 5: Write `src/memory/api/users.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from memory import ids
from memory.api.app import require_master
from memory.auth import keys
from memory.auth.principal import Principal
from memory.db import get_session
from memory.errors import DomainError
from memory.models import ApiKey, Tenant, User

router = APIRouter(prefix="/v1/users", tags=["users"])


class UserNotFound(DomainError):
    code = "USER_NOT_FOUND"
    status = 404


class UserAlreadyExists(DomainError):
    code = "USER_ALREADY_EXISTS"
    status = 409


class CreateUserRequest(BaseModel):
    id: str | None = None


class CreateUserResponse(BaseModel):
    user_id: str
    created_at: str


class CreateKeyResponse(BaseModel):
    key_id: str
    key: str


def _ensure_tenant(db: Session, tenant_id: str) -> None:
    if db.get(Tenant, tenant_id) is None:
        db.add(Tenant(id=tenant_id))
        db.flush()


@router.post("", status_code=201, response_model=CreateUserResponse)
def create_user(
    body: CreateUserRequest,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> CreateUserResponse:
    """Provisioning. An explicit id is the ACH path; omitting it is standalone."""
    _ensure_tenant(db, principal.tenant_id)

    user = User(
        id=body.id or ids.new_user_id(),
        tenant_id=principal.tenant_id,
        bank_id=ids.new_user_bank_id(),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        # A caller-supplied id that already exists is an ordinary client
        # mistake, not a server fault. Caught rather than pre-checked: a
        # SELECT-then-INSERT would still race.
        db.rollback()
        raise UserAlreadyExists("a user with that id already exists") from exc
    return CreateUserResponse(user_id=user.id, created_at=user.created_at.isoformat())


@router.get("/{user_id}", response_model=CreateUserResponse)
def get_user(
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> CreateUserResponse:
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise UserNotFound(user_id=user_id)
    return CreateUserResponse(user_id=user.id, created_at=user.created_at.isoformat())


@router.post("/{user_id}/keys", status_code=201, response_model=CreateKeyResponse)
def create_key(
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> CreateKeyResponse:
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise UserNotFound(user_id=user_id)

    plaintext = keys.generate_key()
    row = ApiKey(
        id=ids.new_key_id(),
        tenant_id=principal.tenant_id,
        user_id=user.id,
        secret_hash=keys.hash_key(plaintext),
    )
    db.add(row)
    db.flush()
    # The only time the plaintext exists outside the caller's hands (§5.3).
    return CreateKeyResponse(key_id=row.id, key=plaintext)
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_users_api.py -v`
Expected: PASS — 6 passed

- [ ] **Step 7: Commit**

```bash
git add src/memory/api tests/conftest.py tests/test_users_api.py
git commit -m "feat: user and API key provisioning endpoints"
```

---

### Task 6: Hindsight client

**Files:**
- Create: `src/memory/hindsight/__init__.py`
- Create: `src/memory/hindsight/paths.py`
- Create: `src/memory/hindsight/client.py`
- Test: `tests/test_hindsight_client.py`
- Test: `tests/test_integration_hindsight.py`

**Interfaces:**
- Consumes: `memory.config.get_settings`, `memory.errors.HindsightError`.
- Produces:
  - `memory.hindsight.client.HindsightClient(base_url, api_key, tenant_id)`
  - `.retain(bank_id, content, *, document_id=None, metadata=None, context=None, update_mode="replace", is_async=True) -> dict`
  - `.recall(bank_id, query) -> dict`
  - `.ensure_bank(bank_id) -> None` — idempotent; enables `memory_defense`
  - `memory.hindsight.client.get_client() -> HindsightClient` (cached)

- [ ] **Step 1: Discover the real endpoint paths**

The published documentation disagrees with itself on the retain path
(`/memories` in one page, `/memories` in the API reference), so the paths
are pinned against the running server rather than a doc page.

Start Hindsight locally and read its schema:

```bash
HINDSIGHT_API_LLM_API_KEY="${LITELLM_API_KEY}" \
  uvx --from hindsight-api hindsight-local-mcp &
for i in $(seq 1 30); do
  curl -sf http://localhost:8888/openapi.json >/dev/null && break
  sleep 2
done
curl -sf http://localhost:8888/openapi.json \
  | python -c "import json,sys; [print(p) for p in sorted(json.load(sys.stdin)['paths'])]"
```

Expected: a list of paths under `/v1/{tenant}/banks/{bank_id}/...`. Record the
four we need — retain, recall, bank upsert, bank config — and use those literal
values in Step 2. If a path differs from the one written below, the discovered
value wins; correct Step 2 before writing it.

- [ ] **Step 2: Write `src/memory/hindsight/paths.py`**

Create `src/memory/hindsight/__init__.py` (empty), then write the module using
the paths discovered in Step 1. The values below are the expected ones:

```python
"""Hindsight endpoint paths.

Pinned against the openapi.json of the deployed Hindsight version. These are
the first thing to re-check on a Hindsight upgrade.
"""


def bank(tenant: str, bank_id: str) -> str:
    return f"/v1/{tenant}/banks/{bank_id}"


def bank_config(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/config"


def retain(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/memories"


def recall(tenant: str, bank_id: str) -> str:
    return f"{bank(tenant, bank_id)}/memories/recall"
```

- [ ] **Step 3: Write the failing test**

`tests/test_hindsight_client.py`:

```python
import httpx
import pytest
import respx

from memory.errors import HindsightError
from memory.hindsight.client import HindsightClient

BASE = "http://hindsight.test"
BANK = "user_11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")


@respx.mock
def test_retain_posts_the_item_envelope(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"success": True, "operation_id": "op-1"})
    )

    result = client.retain(BANK, "we use uv", metadata={"agent": "codex"})

    assert result["operation_id"] == "op-1"
    body = route.calls.last.request.read()
    import json

    payload = json.loads(body)
    assert payload["items"][0]["content"] == "we use uv"
    assert payload["items"][0]["metadata"] == {"agent": "codex"}
    assert payload["async"] is True


@respx.mock
def test_retain_sends_the_api_key(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.retain(BANK, "x")

    assert route.calls.last.request.headers["authorization"] == "Bearer secret"


@respx.mock
def test_sync_retain_sets_async_false(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.retain(BANK, "x", is_async=False)

    import json

    assert json.loads(route.calls.last.request.read())["async"] is False


@respx.mock
def test_recall_posts_the_query(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"memories": []})
    )

    client.recall(BANK, "how do we do migrations")

    import json

    assert json.loads(route.calls.last.request.read())["query"] == (
        "how do we do migrations"
    )


@respx.mock
def test_ensure_bank_enables_memory_defense(client):
    respx.put(f"{BASE}/v1/default/banks/{BANK}").mock(
        return_value=httpx.Response(200, json={"bank_id": BANK})
    )
    config = respx.patch(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json={})
    )

    client.ensure_bank(BANK)

    import json

    assert json.loads(config.calls.last.request.read())["memory_defense"]["enabled"]


@respx.mock
def test_upstream_failure_becomes_a_hindsight_error(client):
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with pytest.raises(HindsightError):
        client.retain(BANK, "x")


@respx.mock
def test_hindsight_error_does_not_carry_the_bank_id(client):
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(500, text=f"bank {BANK} exploded")
    )

    with pytest.raises(HindsightError) as caught:
        client.retain(BANK, "x")

    assert BANK not in str(caught.value)
    assert BANK not in str(caught.value.details)
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest tests/test_hindsight_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.hindsight.client'`

- [ ] **Step 5: Write `src/memory/hindsight/client.py`**

```python
from functools import lru_cache
from typing import Any

import httpx

from memory.config import get_settings
from memory.errors import HindsightError
from memory.hindsight import paths


class HindsightClient:
    def __init__(self, base_url: str, api_key: str, tenant_id: str) -> None:
        self._tenant = tenant_id
        self._materialized: set[str] = set()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=30.0)

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict:
        response = None
        try:
            response = self._http.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            # Logged here and never attached to the raised error: the httpx
            # exception holds .request.url, which contains the bank ID.
            logger.warning("hindsight transport failure: %s", type(exc).__name__)

        if response is None:
            # Raised OUTSIDE the except block on purpose. Inside it, Python sets
            # __context__ to the httpx error even with `from None`, and anything
            # walking __context__ would reach the bank ID (SPEC §23 inv. 29).
            raise HindsightError("memory backend unreachable")

        if response.status_code >= 400:
            raise HindsightError(
                "memory backend rejected the request",
                upstream_status=response.status_code,
            )

        return response.json()

    def retain(
        self,
        bank_id: str,
        content: str,
        *,
        document_id: str | None = None,
        metadata: dict[str, str] | None = None,
        context: str | None = None,
        update_mode: str = "replace",
        is_async: bool = True,
    ) -> dict:
        item: dict[str, Any] = {"content": content, "update_mode": update_mode}
        if document_id is not None:
            item["document_id"] = document_id
        if metadata:
            item["metadata"] = metadata
        if context is not None:
            item["context"] = context

        # No "tags" key is ever sent: v1 writes no retrieval tags (SPEC §13.6).
        return self._request(
            "POST",
            paths.retain(self._tenant, bank_id),
            {"items": [item], "async": is_async},
        )

    def recall(self, bank_id: str, query: str) -> dict:
        return self._request(
            "POST", paths.recall(self._tenant, bank_id), {"query": query}
        )

    def ensure_bank(self, bank_id: str) -> None:
        """Idempotent materialization, done once per bank per process.

        Without the guard this costs two upstream round trips on every single
        write. The set is per-process and unbounded-in-theory; in practice it
        holds one short string per bank this replica has touched.

        Banks auto-create on first use, so the upsert exists only to attach
        memory_defense — the single field v1 sets (SPEC §19.5). Everything else
        stays on Hindsight's stock configuration.
        """
        if bank_id in self._materialized:
            return
        self._request("PUT", paths.bank(self._tenant, bank_id), {})
        # The config body is wrapped in "updates": BankConfigUpdate requires it
        # and a flat body 422s. Verified against a live server, not the docs.
        self._request(
            "PATCH",
            paths.bank_config(self._tenant, bank_id),
            {"updates": {"memory_defense": {"enabled": True}}},
        )


@lru_cache
def get_client() -> HindsightClient:
    settings = get_settings()
    return HindsightClient(
        base_url=settings.hindsight_url,
        api_key=settings.hindsight_api_key,
        tenant_id=settings.tenant_id,
    )
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_hindsight_client.py -v`
Expected: PASS — 7 passed

- [ ] **Step 7: Write the integration test**

`tests/test_integration_hindsight.py`:

```python
import os
import uuid

import pytest

from memory.hindsight.client import HindsightClient

pytestmark = pytest.mark.integration

HINDSIGHT_URL = os.environ.get("MEMORY_HINDSIGHT_URL", "http://localhost:8888")


@pytest.fixture
def live_client() -> HindsightClient:
    return HindsightClient(
        base_url=HINDSIGHT_URL,
        api_key=os.environ.get("MEMORY_HINDSIGHT_API_KEY", ""),
        tenant_id="default",
    )


def test_retain_then_recall_round_trip(live_client):
    bank_id = f"user_{uuid.uuid4()}"
    live_client.ensure_bank(bank_id)

    live_client.retain(
        bank_id,
        "This project pins its Python dependencies with uv, never with pip.",
        is_async=False,
    )
    result = live_client.recall(bank_id, "how are Python dependencies managed here")

    assert "uv" in str(result).lower()
```

- [ ] **Step 8: Run the integration test against live Hindsight**

With the Hindsight started in Step 1 still running:

```bash
uv run pytest tests/test_integration_hindsight.py -v -m integration
```

Expected: PASS. If it fails on a 404, a path in `paths.py` does not match the
running version — correct `paths.py` from the `openapi.json` output of Step 1
and re-run.

- [ ] **Step 9: Commit**

```bash
git add src/memory/hindsight tests/test_hindsight_client.py tests/test_integration_hindsight.py
git commit -m "feat: Hindsight client with retain, recall and bank materialization"
```

---

### Task 7: Memory data plane for `scope=user`

**Files:**
- Create: `src/memory/api/memory.py`
- Modify: `src/memory/api/app.py` (add the memory router include)
- Create: `src/memory/banks.py`
- Test: `tests/test_memory_api.py`

**Interfaces:**
- Consumes: `memory.api.app.current_principal`, `memory.hindsight.client.get_client`,
  `memory.models.User`, `memory.errors`.
- Produces:
  - `memory.banks.resolve_user_bank(db, principal, requested_user_id) -> str`
  - Routes: `POST /v1/memory/retain`, `POST /v1/memory/sync_retain`,
    `POST /v1/memory/recall`

- [ ] **Step 1: Write the failing test**

`tests/test_memory_api.py`:

```python
import httpx
import pytest
import respx

BASE = "http://hindsight.test"


@pytest.fixture
def user_key(client, master_headers, tenant) -> tuple[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return user_id, key


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _mock_hindsight() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/config$").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_retain_reaches_the_callers_own_bank(client, user_key, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True, "operation_id": "op-1"})
    )
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "we use uv"},
        headers=_headers(key),
    )

    assert response.status_code == 200
    assert route.called


@respx.mock
def test_retain_response_never_contains_the_bank_id(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(
            200, json={"success": True, "bank_id": "user_leaked", "operation_id": "op-1"}
        )
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x"},
        headers=_headers(key),
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)


@respx.mock
def test_two_users_reach_two_different_banks(client, master_headers, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    banks = []
    for _ in range(2):
        user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
            "user_id"
        ]
        key = client.post(
            f"/v1/users/{user_id}/keys", json={}, headers=master_headers
        ).json()["key"]
        client.post(
            "/v1/memory/retain",
            json={"scope": "user", "content": "x"},
            headers=_headers(key),
        )
        banks.append(str(route.calls.last.request.url))

    assert banks[0] != banks[1]


@respx.mock
def test_user_key_cannot_target_another_user(client, master_headers, user_key, tenant):
    _mock_hindsight()
    other = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "user_id": other, "content": "x"},
        headers=_headers(key),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@respx.mock
def test_master_key_must_name_the_target_user(client, master_headers, tenant):
    _mock_hindsight()

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x"},
        headers=master_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SCOPE"


@respx.mock
def test_master_key_reaches_a_named_user_bank(client, master_headers, user_key, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    user_id, _ = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "user_id": user_id, "content": "x"},
        headers=master_headers,
    )

    assert response.status_code == 200
    assert route.called


def test_project_scope_is_not_implemented_yet(client, user_key, tenant):
    _, key = user_key

    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "query": "x"},
        headers=_headers(key),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SCOPE"


def test_oversize_content_is_rejected(client, user_key, tenant, monkeypatch):
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x" * 100},
        headers=_headers(key),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


@respx.mock
def test_recall_returns_the_upstream_payload(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"memories": [{"content": "we use uv"}]})
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "deps"},
        headers=_headers(key),
    ).json()

    assert body["result"]["memories"][0]["content"] == "we use uv"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_memory_api.py -v`
Expected: FAIL — 404 on `/v1/memory/retain`, since the Task 5 router is empty.

- [ ] **Step 3: Write `src/memory/banks.py`**

```python
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.errors import Forbidden, InvalidScope
from memory.models import User


def resolve_user_bank(
    db: Session, principal: Principal, requested_user_id: str | None
) -> str:
    """Map scope=user to a bank ID.

    A user key always addresses itself; naming somebody else is a 403, not a
    silent redirect. A master key has no identity of its own, so it must name
    its target (SPEC §5.2).
    """
    if principal.is_master:
        if not requested_user_id:
            raise InvalidScope("master-key requests with scope=user must set user_id")
        target_id = requested_user_id
    else:
        if requested_user_id and requested_user_id != principal.user_id:
            raise Forbidden("a user key cannot address another user's memory")
        target_id = principal.user_id

    user = db.get(User, target_id)
    if user is None or user.tenant_id != principal.tenant_id:
        # Same shape as a cross-tenant miss: no existence signal either way.
        raise Forbidden("no accessible memory for the requested user")

    return user.bank_id
```

- [ ] **Step 4: Write `src/memory/api/memory.py`**

```python
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from memory.api.app import current_principal
from memory.auth.principal import Principal
from memory.banks import resolve_user_bank
from memory.config import get_settings
from memory.db import get_session
from memory.errors import ContentTooLarge, InvalidScope
from memory.hindsight.client import get_client

router = APIRouter(prefix="/v1/memory", tags=["memory"])

Scope = Literal["user", "project"]


class RetainRequest(BaseModel):
    scope: Scope
    content: str
    user_id: str | None = None
    document_id: str | None = None
    update_mode: str = "replace"
    metadata: dict[str, str] | None = None


class RecallRequest(BaseModel):
    scope: Scope
    query: str
    user_id: str | None = None


class MemoryResponse(BaseModel):
    result: dict[str, Any]


def _resolve_bank(
    scope: Scope, db: Session, principal: Principal, user_id: str | None
) -> str:
    if scope == "user":
        return resolve_user_bank(db, principal, user_id)
    # Project resolution is Plan 2. Refusing loudly beats a partial answer.
    raise InvalidScope("scope=project is not available in this build")


def _strip_bank_id(value: Any) -> Any:
    """Hindsight echoes bank_id; it must not reach the caller (SPEC inv. 29).

    Recursive on purpose. A top-level-only filter holds for today's flat retain
    response, but recall returns nested items and the invariant is absolute —
    it should not depend on an upstream response shape we do not control.
    """
    if isinstance(value, dict):
        return {k: _strip_bank_id(v) for k, v in value.items() if k != "bank_id"}
    if isinstance(value, list):
        return [_strip_bank_id(item) for item in value]
    return value


@router.post("/retain", response_model=MemoryResponse)
def retain(
    body: RetainRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    return _retain(body, principal, db, is_async=True)


@router.post("/sync_retain", response_model=MemoryResponse)
def sync_retain(
    body: RetainRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    return _retain(body, principal, db, is_async=False)


def _retain(
    body: RetainRequest, principal: Principal, db: Session, *, is_async: bool
) -> MemoryResponse:
    limit = get_settings().max_content_bytes
    if len(body.content.encode("utf-8")) > limit:
        raise ContentTooLarge(f"content exceeds {limit} bytes")

    bank_id = _resolve_bank(body.scope, db, principal, body.user_id)
    client = get_client()
    client.ensure_bank(bank_id)

    result = client.retain(
        bank_id,
        body.content,
        document_id=body.document_id,
        metadata=body.metadata,
        update_mode=body.update_mode,
        is_async=is_async,
    )
    return MemoryResponse(result=_strip_bank_id(result))


@router.post("/recall", response_model=MemoryResponse)
def recall(
    body: RecallRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id = _resolve_bank(body.scope, db, principal, body.user_id)
    result = get_client().recall(bank_id, body.query)
    return MemoryResponse(result=_strip_bank_id(result))
```

- [ ] **Step 5: Clear the cached client between tests**

Append to `tests/conftest.py`, inside the `app` fixture before `create_app()`:

```python
    from memory.hindsight.client import get_client

    get_client.cache_clear()
```

and after the `yield application` line:

```python
    get_client.cache_clear()
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_memory_api.py -v`
Expected: PASS — 9 passed

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -v -m "not integration"`
Expected: PASS — 38 passed, 1 deselected

- [ ] **Step 8: Commit**

```bash
git add src/memory/banks.py src/memory/api/memory.py tests/conftest.py tests/test_memory_api.py
git commit -m "feat: retain and recall for scope=user"
```

---

### Task 8: Container packaging and end-to-end smoke

**Files:**
- Create: `Dockerfile`
- Modify: `docker-compose.yml` (add the `api` service)
- Create: `scripts/smoke.sh`
- Create: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a runnable image and a smoke script that exercises provision →
  key → retain → recall against live Postgres and live Hindsight.

- [ ] **Step 1: Write the `Dockerfile`**

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
# Install from the lock file, never from a hand-copied dependency list:
# a second list in the Dockerfile drifts from pyproject.toml silently.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --target=/app/deps -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app/deps:/app/src
COPY --from=builder /app/deps /app/deps
COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "memory.api.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

Explicit `COPY` paths only — no `COPY . .`, so `.git/`, `tests/` and the
virtualenv never enter the image.

- [ ] **Step 2: Add Hindsight and the API to `docker-compose.yml`**

The goal is that `docker compose up` alone produces the complete scenario an
end-to-end test can run against — no host-installed Hindsight, no `uvx`, no
ambient environment.

First create `Dockerfile.hindsight`. Hindsight publishes no container image, but
it ships as the `hindsight-api` pip package, so we build a thin one and pin the
version:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir "hindsight-api==${HINDSIGHT_VERSION:-*}"
EXPOSE 8888
CMD ["hindsight-api", "--host", "0.0.0.0", "--port", "8888"]
```

Pin the real version: run `uv run pip index versions hindsight-api` (or check
the version resolved during Task 6) and replace the specifier with an exact
`==X.Y.Z`. An unpinned memory engine is a silently moving e2e baseline. If
`hindsight-api` exposes no `hindsight-api` console script, use the entry point
discovered in Task 6 and record which one in the file.

Then replace `docker-compose.yml` entirely:

```yaml
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: memory
      POSTGRES_PASSWORD: memory
      POSTGRES_DB: memory
    ports:
      - "5433:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U memory"]
      interval: 2s
      timeout: 3s
      retries: 15
    volumes:
      - memory-pgdata:/var/lib/postgresql/data

  hindsight-db:
    # pgvector, not plain postgres: Hindsight stores embeddings in a vector
    # column and fails at query time with `could not access file "$$libdir/vector"`
    # on a stock image. Everything starts healthy and the FIRST RETAIN 500s.
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_USER: hindsight
      POSTGRES_PASSWORD: hindsight
      POSTGRES_DB: hindsight
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U hindsight"]
      interval: 2s
      timeout: 3s
      retries: 15
    volumes:
      - hindsight-pgdata:/var/lib/postgresql/data

  hindsight:
    build:
      context: .
      dockerfile: Dockerfile.hindsight
    depends_on:
      hindsight-db:
        condition: service_healthy
    environment:
      HINDSIGHT_API_DATABASE_URL: postgresql://hindsight:hindsight@hindsight-db:5432/hindsight
      # Explicit LLM wiring. Without these, Hindsight falls back to
      # provider=openai/gpt-4o-mini with no base URL, and the OpenAI SDK then
      # silently picks up whatever OPENAI_BASE_URL happens to be in the ambient
      # environment — which is how a green test on a laptop becomes a red one
      # everywhere else.
      HINDSIGHT_API_LLM_PROVIDER: openai
      HINDSIGHT_API_LLM_BASE_URL: ${LITELLM_BASE_URL:?set LITELLM_BASE_URL}/v1
      HINDSIGHT_API_LLM_API_KEY: ${LITELLM_API_KEY:?set LITELLM_API_KEY}
      HINDSIGHT_API_LLM_MODEL: ${HINDSIGHT_LLM_MODEL:-bedrock.openai.gpt-oss-20b-1-0}
    ports:
      - "8888:8888"
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8888/openapi.json').status==200 else 1)\""]
      interval: 5s
      timeout: 5s
      retries: 60

  api:
    build: .
    depends_on:
      postgres:
        condition: service_healthy
      hindsight:
        condition: service_healthy
    environment:
      MEMORY_DATABASE_URL: postgresql+psycopg://memory:memory@postgres:5432/memory
      MEMORY_MASTER_KEY_HASH: ${MEMORY_MASTER_KEY_HASH:?set MEMORY_MASTER_KEY_HASH}
      MEMORY_HINDSIGHT_URL: http://hindsight:8888
      MEMORY_HINDSIGHT_API_KEY: ${MEMORY_HINDSIGHT_API_KEY:-}
      MEMORY_TENANT_ID: default
    ports:
      - "8000:8000"

volumes:
  memory-pgdata:
  hindsight-pgdata:
```

Two separate Postgres instances on purpose: Hindsight owns its schema and we own
ours, and sharing one database would couple our migrations to theirs for no
gain. Embeddings stay on Hindsight's `local` provider, so the only external
dependency in the whole scenario is LiteLLM.

The unit-test workflow is unaffected — `docker compose up -d postgres` still
starts just our database.

- [ ] **Step 3: Write `scripts/smoke.sh`**

```bash
#!/usr/bin/env bash
# End-to-end smoke: provision a user, mint a key, retain, recall.
# Requires: docker compose up -d, and Hindsight reachable on MEMORY_HINDSIGHT_URL.
set -euo pipefail

API="${API:-http://localhost:8000}"
MASTER="${MEMORY_MASTER_KEY:?set MEMORY_MASTER_KEY to the plaintext master key}"

for i in $(seq 1 30); do
  curl -sf "${API}/docs" >/dev/null && break
  sleep 2
done
curl -sf "${API}/docs" >/dev/null || { echo "FAIL: API never came up" >&2; exit 1; }

user_id=$(curl -sf -X POST "${API}/v1/users" \
  -H "Authorization: Bearer ${MASTER}" -H 'Content-Type: application/json' \
  -d '{}' | python -c 'import json,sys; print(json.load(sys.stdin)["user_id"])')

user_key=$(curl -sf -X POST "${API}/v1/users/${user_id}/keys" \
  -H "Authorization: Bearer ${MASTER}" -H 'Content-Type: application/json' \
  -d '{}' | python -c 'import json,sys; print(json.load(sys.stdin)["key"])')

curl -sf -X POST "${API}/v1/memory/sync_retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","content":"This project pins dependencies with uv, never pip."}' \
  >/dev/null

recalled=$(curl -sf -X POST "${API}/v1/memory/recall" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","query":"how are dependencies managed"}')

echo "${recalled}" | grep -qi "uv" || { echo "FAIL: recall did not return the fact" >&2; exit 1; }
echo "${recalled}" | grep -q "bank_id" && { echo "FAIL: bank_id leaked to the client" >&2; exit 1; }

echo "PASS: provision -> key -> retain -> recall, no bank_id leak"
```

```bash
chmod +x scripts/smoke.sh
```

- [ ] **Step 4: Build and run the stack**

```bash
export MEMORY_MASTER_KEY="mem_local_master_change_me"
export MEMORY_MASTER_KEY_HASH=$(python -c \
  "import hashlib,os; print(hashlib.sha256(os.environ['MEMORY_MASTER_KEY'].encode()).hexdigest())")
docker compose up -d --build
docker compose ps
docker compose run --rm api python -m alembic upgrade head
```

`LITELLM_BASE_URL` and `LITELLM_API_KEY` must be set — compose fails fast with a
named error if they are not.

Expected: all four services healthy (`postgres`, `hindsight-db`, `hindsight`,
`api`); migration reports `Running upgrade -> ...`. Hindsight's first start
builds its schema and downloads the local embedding model, so allow several
minutes; the healthcheck's 60 retries cover it.

- [ ] **Step 5: Run the smoke test**

Everything the smoke test needs is inside compose — nothing on the host:

```bash
./scripts/smoke.sh
```

Expected: `PASS: provision -> key -> retain -> recall, no bank_id leak`

- [ ] **Step 6: Write `README.md`**

````markdown
# ach-memory

Multi-tenant memory service for coding agents, over [Hindsight](https://github.com/vectorize-io/hindsight).
See `SPEC-v1.md` for the contract and `docs/superpowers/plans/` for the build plans.

This build implements Plan 1: `scope=user` memory only. Projects, groups and the
MCP surface are Plans 2 and 3.

## Run locally

```bash
export LITELLM_BASE_URL=https://api.ackstorm.ai
export LITELLM_API_KEY=...                       # your key
export MEMORY_MASTER_KEY="mem_local_master_change_me"
export MEMORY_MASTER_KEY_HASH=$(python -c \
  "import hashlib,os; print(hashlib.sha256(os.environ['MEMORY_MASTER_KEY'].encode()).hexdigest())")

docker compose up -d --build          # postgres, hindsight-db, hindsight, api
docker compose run --rm api python -m alembic upgrade head
./scripts/smoke.sh
```

The whole scenario lives in compose: our API and database, Hindsight and its own
database. The only external dependency is LiteLLM, which serves the models
Hindsight uses for extraction and reflection.

## Test

```bash
uv sync --dev
docker compose up -d postgres           # our database only
uv run pytest -m "not integration"      # unit + API, no Hindsight needed

docker compose up -d hindsight          # adds Hindsight + its database
uv run pytest -m integration            # live round-trip
```
````

- [ ] **Step 7: Commit**

```bash
git add Dockerfile docker-compose.yml scripts/smoke.sh README.md
git commit -m "feat: container packaging and end-to-end smoke test"
```

---

## Done when

- `uv run pytest -m "not integration"` is green with no live Hindsight.
- `uv run pytest -m integration` is green against a live Hindsight.
- `./scripts/smoke.sh` prints PASS.
- `grep -rn "bank_id" src/memory/api/` shows only the stripping helper.

## Deliberately not in this plan

Projects, slugs, groups, ownership, `PROJECT_ACCESS_DENIED` (Plan 2) · the MCP
server and its 17 tools (Plan 3) · curation, documents and operations (Plan 3) ·
directives, mental models, the admin plane and Helm (Plan 4) · audit events,
which land with the first master-key action that mutates shared state in Plan 2 ·
rate limiting, which needs a deployment shape to be worth tuning.
