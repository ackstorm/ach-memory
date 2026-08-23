# Plan 2 — Projects, Ownership and Groups: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `scope=project` works end to end — a coding agent resolves its project from Git or from `MEMORY_PROJECT`, the first authenticated user to touch a slug owns it, ownership can move to another user or to a group, renaming leaves a forwarding tombstone instead of a hole, and every authorized member of the owning group reaches the same memory bank.

**Architecture:** Projects get a public `project_slug` derived from the whole Git locator, and an internal DB id nobody outside sees. `banks.resolve_project_bank` becomes the single place that maps a slug to a bank: normalize, look up live projects, follow retired slugs, lazily create, authorize, and only then hand back a bank id. Groups are a flat membership table with no roles. Ownership transfer and rename are ordinary authorized operations that write audit events.

**Tech Stack:** unchanged from Plan 1 — Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (sync) + psycopg 3, Alembic, httpx, pytest + respx.

## Global Constraints

From `SPEC-v1.md` rev. 5. Every task's requirements implicitly include this section.

- **Synchronous stack throughout.** No `async def`, no async SQLAlchemy.
- **All dependency work through `uv`.** A bare `pip install` is an error.
- **`bank_id` never crosses the API boundary** in either direction (inv. 29).
- **`project_slug` is the only public project identifier** (inv. 7). The internal DB id must never be required by, or returned to, an ordinary client.
- **A Git-derived slug flattens the whole locator, never the repository basename** (inv. 10). `github.com/acme/payments-api` → `github.com-acme-payments-api`.
- **`git_locator` is metadata**: it never resolves identity and never grants access (inv. 11) — but a *mismatch* refuses an ambiguous explicit resolution.
- **Resolution order is `MEMORY_PROJECT` → Git-derived slug → error** (inv. 12). There is no `MEMORY_PROJECT_ID` and no `MEMORY_PROJECT_NAME`.
- **A retired slug resolves to exactly one project in one hop** and is never reassigned while its tombstone exists (inv. 13). Slug uniqueness spans live projects *and* retired slugs.
- **Any caller authorized for a project may rename it and transfer its ownership** (inv. 15). v1 has no group-admin role.
- **`PROJECT_ACCESS_DENIED` returns `project_slug` and `owner_type`, never `owner_id`** (§8.5).
- **Master-key actions, ownership changes and renames are audit events** (§20 MUST).
- Write handlers commit explicitly before returning; `get_session` is rollback-only (Plan 1 final review).
- All code, comments, commit messages and docs in English.

## What Plan 1 left for this plan

Recorded by the whole-branch review; Task 1 closes the first two.

- `tests/conftest.py` overrides `get_session` with a lambda returning one shared session, so **no API test ever exercises commit or rollback**. `test_duplicate_explicit_user_id_is_a_conflict` passes only because both requests share a session.
- `users.py`'s `create_user` calls a blanket `db.rollback()` on `IntegrityError`, which discards the **whole** request transaction. The project-creation race (§9) has the same catch-and-recover shape but with prior writes in the request, so it needs `begin_nested()`.
- Reserved-metadata validation (§13.4) is **not** in this plan — it belongs with provenance in Plan 3.

## File Structure

```
src/memory/
  slugs.py                 NEW  canonical_locator, normalize_slug, slug_from_locator
  models.py                MOD  + Group, GroupMember, Project, RetiredSlug, AuditEvent
  errors.py                MOD  + project error classes
  audit.py                 NEW  record()
  projects.py              NEW  lookup, lazy create, rename + tombstone, transfer
  banks.py                 MOD  + resolve_project_bank
  api/groups.py            NEW  group provisioning + membership
  api/projects.py          NEW  project control plane
  api/memory.py            MOD  scope=project stops being refused
  api/app.py               MOD  + two routers
tests/
  conftest.py              MOD  per-request session, project/group helpers
  test_slugs.py            NEW
  test_projects.py         NEW  resolution, authorization, rename, transfer
  test_groups_api.py       NEW
  test_projects_api.py     NEW
  test_memory_api.py       MOD  + scope=project cases
```

`slugs.py` is pure and separate because it is the one piece with no I/O and the
most edge cases. `projects.py` holds the domain operations; `api/projects.py`
holds only HTTP. `banks.py` stays the single chokepoint for scope → bank.

---

### Task 1: Make the test harness exercise real transactions

**Files:**
- Modify: `tests/conftest.py`
- Modify: `src/memory/api/users.py`
- Test: `tests/test_users_api.py`

**Interfaces:**
- Consumes: `memory.db.get_session`, `memory.models`.
- Produces: an `app` fixture whose `get_session` override yields a **fresh session per request**, all bound to one outer test transaction that is rolled back at the end.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_users_api.py`:

```python
def test_each_request_gets_its_own_session(client, master_headers, tenant):
    """Two requests must not share a Session.

    Sharing one hides every commit and rollback bug: an uncommitted write is
    still visible to the next request through the identity map, so a handler
    that forgets to commit looks correct in tests and loses data in production.

    This wraps the fixture's OWN override rather than the real
    memory.db.get_session. Calling the real one would open a second physical
    connection outside the test transaction: it would commit rows the rollback
    never undoes, and it would deadlock against the `tenant` fixture's
    uncommitted row.
    """
    from memory import db

    seen = []
    override = client.app.dependency_overrides[db.get_session]

    def _recording():
        gen = override()
        session = next(gen)
        seen.append(id(session))
        try:
            yield session
        finally:
            gen.close()

    client.app.dependency_overrides[db.get_session] = _recording

    client.post("/v1/users", json={}, headers=master_headers)
    client.post("/v1/users", json={}, headers=master_headers)

    assert len(seen) == 2
    assert seen[0] != seen[1], "both requests shared one Session"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_users_api.py::test_each_request_gets_its_own_session -v`
Expected: FAIL — the override returns the same object twice, or the recording
override is never consulted because the fixture's lambda already replaced it.

- [ ] **Step 3: Rewrite the `app` fixture's session override**

In `tests/conftest.py`, replace the single line
`application.dependency_overrides[db.get_session] = lambda: session` with a
per-request session factory bound to the same connection:

```python
@pytest.fixture
def app(connection, session, monkeypatch):
    from memory import db
    from memory.api.app import create_app
    from memory.auth import keys
    from memory.config import get_settings
    from memory.hindsight.client import get_client

    monkeypatch.setenv("MEMORY_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", keys.hash_key(MASTER_PLAINTEXT))
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    get_settings.cache_clear()
    get_client.cache_clear()

    factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")

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
```

Split the connection out of the `session` fixture so both can share it:

```python
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
```

- [ ] **Step 4: Run the whole suite and fix what the harness now exposes**

Run: `uv run pytest -v -m "not integration"`

Expected: the new test passes. **Some existing tests may now fail** — that is
the point: they were passing on a shared session. In particular
`test_duplicate_explicit_user_id_is_a_conflict` now runs two real requests, and
the first one's write must be committed for the second to conflict.

If a test fails because a handler never committed, the handler is the bug — fix
the handler, not the test. Report every test whose behavior changed.

- [ ] **Step 5: Scope the rollback in `create_user` to a savepoint**

In `src/memory/api/users.py`, `create_user` currently calls a blanket
`db.rollback()` when the insert conflicts, which would discard any earlier write
in the same request. Wrap the insert instead:

```python
    user = User(
        id=body.id or ids.new_user_id(),
        tenant_id=principal.tenant_id,
        bank_id=ids.new_user_bank_id(),
    )
    try:
        with db.begin_nested():
            db.add(user)
    except IntegrityError as exc:
        # A caller-supplied id that already exists is an ordinary client
        # mistake, not a server fault. A savepoint, not a bare rollback: this
        # handler is small today, but the same shape guards the project-creation
        # race (SPEC §9) where earlier writes must survive the conflict.
        raise UserAlreadyExists("a user with that id already exists") from exc
    db.commit()
```

`db.begin_nested()` flushes and releases the savepoint on exit, so the
`IntegrityError` surfaces there rather than at an explicit `flush()`.

- [ ] **Step 6: Run the suite again**

Run: `uv run pytest -v -m "not integration"`
Expected: PASS, no warnings. Report the count.

- [ ] **Step 7: Commit**

```bash
git add tests/conftest.py tests/test_users_api.py src/memory/api/users.py
git commit -m "test: one session per request, and scope the conflict rollback to a savepoint"
```

---

### Task 2: Slug derivation

**Files:**
- Create: `src/memory/slugs.py`
- Modify: `src/memory/errors.py`
- Test: `tests/test_slugs.py`

**Interfaces:**
- Consumes: `memory.errors`.
- Produces:
  - `memory.slugs.canonical_locator(remote_url: str) -> str`
  - `memory.slugs.normalize_slug(raw: str) -> str`
  - `memory.slugs.slug_from_locator(remote_url: str) -> str`
  - `memory.errors.ProjectInvalidSlug` (code `PROJECT_INVALID_SLUG`, 400)

- [ ] **Step 1: Write the failing test**

`tests/test_slugs.py`:

```python
import pytest

from memory import slugs
from memory.errors import ProjectInvalidSlug


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:acme/payments-api.git",
        "https://github.com/acme/payments-api.git",
        "https://github.com/acme/payments-api",
        "ssh://git@github.com/acme/payments-api.git",
        "https://github.com/acme/payments-api/",
    ],
)
def test_every_remote_spelling_yields_one_locator(remote):
    assert slugs.canonical_locator(remote) == "github.com/acme/payments-api"


def test_locator_keeps_the_host_so_forges_do_not_collide():
    assert slugs.canonical_locator(
        "https://gitlab.com/customer/payments-api"
    ) == "gitlab.com/customer/payments-api"


def test_slug_flattens_the_whole_locator():
    assert slugs.slug_from_locator("git@github.com:acme/payments-api.git").startswith(
        "github.com-acme-payments-api-"
    )


def test_the_same_repository_always_yields_the_same_slug():
    spellings = [
        "git@github.com:acme/payments-api.git",
        "https://github.com/acme/payments-api",
        "ssh://git@github.com:22/acme/payments-api.git",
    ]
    assert len({slugs.slug_from_locator(s) for s in spellings}) == 1


def test_differently_segmented_locators_do_not_collide():
    """The collision this module exists to prevent.

    normalize_slug collapses `/` and `-` to one separator, so without a
    disambiguator these two unrelated repositories would share a memory bank.
    """
    a = slugs.slug_from_locator("https://github.com/acme/payments-api")
    b = slugs.slug_from_locator("https://github.com/acme-payments/api")
    assert a != b


@pytest.mark.parametrize(
    "remote", ["https://github.com", "git@github.com:", "github.com"]
)
def test_a_remote_without_a_repository_path_is_rejected(remote):
    with pytest.raises(ProjectInvalidSlug):
        slugs.canonical_locator(remote)


def test_two_forges_with_the_same_repository_name_do_not_collide():
    """The reason the slug is not the basename (SPEC §8.2)."""
    a = slugs.slug_from_locator("https://github.com/acme/payments-api")
    b = slugs.slug_from_locator("https://gitlab.com/customer/payments-api")
    assert a != b


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("payments-api", "payments-api"),
        ("Payments API", "payments-api"),
        ("  padded  ", "padded"),
        ("under_scores", "under-scores"),
        ("dots.and.dashes", "dots.and.dashes"),
        ("multiple///separators", "multiple-separators"),
        ("--leading-and-trailing--", "leading-and-trailing"),
    ],
)
def test_normalize_slug(raw, expected):
    assert slugs.normalize_slug(raw) == expected


def test_normalize_slug_is_idempotent():
    once = slugs.normalize_slug("Payments API")
    assert slugs.normalize_slug(once) == once


@pytest.mark.parametrize("raw", ["", "   ", "---", "***", "x" * 200])
def test_unusable_slug_is_rejected(raw):
    with pytest.raises(ProjectInvalidSlug):
        slugs.normalize_slug(raw)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_slugs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.slugs'`

- [ ] **Step 3: Add the error class**

In `src/memory/errors.py`, alongside the existing classes:

```python
class ProjectInvalidSlug(DomainError):
    code = "PROJECT_INVALID_SLUG"
    status = 400
```

- [ ] **Step 4: Write `src/memory/slugs.py`**

```python
import hashlib
import re

from memory.errors import ProjectInvalidSlug

MAX_SLUG_LENGTH = 128

# scp-style remotes have no scheme and use a colon to separate host from path:
#   git@github.com:acme/payments-api.git
_SCP_STYLE = re.compile(r"^(?:[^/@]+@)?([^/:]+):(.+)$")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_USERINFO = re.compile(r"^[^/@]+@")
_DOT_GIT = re.compile(r"\.git$")
_NON_SLUG = re.compile(r"[^a-z0-9.]+")


def canonical_locator(remote_url: str) -> str:
    """One spelling for a Git remote: host/path, lowercase, no scheme or .git.

    The host is kept on purpose. Dropping it would make
    github.com/acme/payments-api and gitlab.com/customer/payments-api the same
    project, which is the collision SPEC §8.2 exists to prevent. The port is
    dropped for the opposite reason: ssh://git@host:22/acme/repo and
    https://host/acme/repo are the same repository and must not become two.
    """
    url = _DOT_GIT.sub("", remote_url.strip().rstrip("/"))

    if _SCHEME.match(url):
        url = _USERINFO.sub("", _SCHEME.sub("", url))
    else:
        scp = _SCP_STYLE.match(url)
        url = f"{scp.group(1)}/{scp.group(2)}" if scp else _USERINFO.sub("", url)

    host, _, path = url.partition("/")
    host = host.split(":", 1)[0]
    path = path.strip("/")
    if not host or not path:
        raise ProjectInvalidSlug(
            "a Git remote must name a host and a repository path"
        )
    return f"{host}/{path}".lower()


def slug_from_locator(remote_url: str) -> str:
    """The whole locator flattened, never the repository basename (inv. 10).

    The digest suffix is not decoration. normalize_slug collapses `/`, `.` and
    `-` to one separator, so without it acme/payments-api and acme-payments/api
    would both become github.com-acme-payments-api — two unrelated repositories
    sharing one memory bank, which is the exact failure this function exists to
    prevent. The digest is taken over the canonical locator, so the same
    repository always yields the same slug however its remote is spelled.
    """
    locator = canonical_locator(remote_url)
    digest = hashlib.sha256(locator.encode("utf-8")).hexdigest()[:8]
    return normalize_slug(f"{locator}-{digest}")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_slugs.py -v`
Expected: PASS — 18 passed

- [ ] **Step 6: Commit**

```bash
git add src/memory/slugs.py src/memory/errors.py tests/test_slugs.py
git commit -m "feat: canonical Git locators and project slug normalization"
```

---

### Task 3: Groups

**Files:**
- Modify: `src/memory/models.py`
- Create: `src/memory/api/groups.py`
- Modify: `src/memory/api/app.py`
- Modify: `src/memory/ids.py` (nothing to add — `new_group_id` already exists)
- Test: `tests/test_groups_api.py`

**Interfaces:**
- Consumes: `memory.api.app.require_master`, `memory.db.get_session`, `memory.ids.new_group_id`.
- Produces:
  - `memory.models.Group`, `memory.models.GroupMember`
  - Routes: `POST /v1/groups`, `GET /v1/groups`, `GET /v1/groups/{group_id}`, `PUT /v1/groups/{group_id}/members/{user_id}`, `DELETE /v1/groups/{group_id}/members/{user_id}`
  - `memory.errors.GroupNotFound` (code `GROUP_NOT_FOUND`, 404)

- [ ] **Step 1: Write the failing test**

`tests/test_groups_api.py`:

```python
import pytest


@pytest.fixture
def user_id(client, master_headers, tenant) -> str:
    return client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]


def test_master_creates_a_group_with_a_generated_id(client, master_headers, tenant):
    response = client.post("/v1/groups", json={}, headers=master_headers)

    assert response.status_code == 201
    assert response.json()["group_id"].startswith("grp_")


def test_master_creates_a_group_with_an_explicit_id(client, master_headers, tenant):
    response = client.post(
        "/v1/groups", json={"id": "grp_payments", "name": "Payments"},
        headers=master_headers,
    )

    assert response.status_code == 201
    assert response.json()["group_id"] == "grp_payments"


def test_membership_is_added_and_removed(client, master_headers, tenant, user_id):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]

    added = client.put(
        f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers
    )
    listed = client.get(f"/v1/groups/{group_id}", headers=master_headers).json()

    assert added.status_code == 204
    assert listed["members"] == [user_id]

    removed = client.delete(
        f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers
    )
    after = client.get(f"/v1/groups/{group_id}", headers=master_headers).json()

    assert removed.status_code == 204
    assert after["members"] == []


def test_adding_the_same_member_twice_is_idempotent(
    client, master_headers, tenant, user_id
):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]

    first = client.put(f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers)
    second = client.put(f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers)
    listed = client.get(f"/v1/groups/{group_id}", headers=master_headers).json()

    assert (first.status_code, second.status_code) == (204, 204)
    assert listed["members"] == [user_id]


def test_group_operations_require_the_master_key(
    client, master_headers, tenant, user_id
):
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.post(
        "/v1/groups", json={}, headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 403


def test_unknown_group_is_404(client, master_headers, tenant):
    response = client.get("/v1/groups/grp_nope", headers=master_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GROUP_NOT_FOUND"


def test_adding_an_unknown_user_is_404(client, master_headers, tenant):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]

    response = client.put(
        f"/v1/groups/{group_id}/members/usr_nope", headers=master_headers
    )

    assert response.status_code == 404
    # The group exists; only the user is missing. The code must say so.
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_reusing_an_explicit_group_id_conflicts(client, master_headers, tenant):
    client.post("/v1/groups", json={"id": "grp_dup"}, headers=master_headers)

    response = client.post("/v1/groups", json={"id": "grp_dup"}, headers=master_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GROUP_ALREADY_EXISTS"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_groups_api.py -v`
Expected: FAIL — 404 on `/v1/groups`, the router does not exist.

- [ ] **Step 3: Add the models**

In `src/memory/models.py`, after `ApiKey`:

```python
class Group(Base):
    __tablename__ = "groups"

    # Externally supplied (ACH) or service-generated, like User. SPEC §4.3.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    # No roles inside a group in v1 (SPEC §4.3): membership is the whole model.
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
```

- [ ] **Step 4: Add the error class**

In `src/memory/errors.py`:

```python
class GroupNotFound(DomainError):
    code = "GROUP_NOT_FOUND"
    status = 404


class GroupAlreadyExists(DomainError):
    code = "GROUP_ALREADY_EXISTS"
    status = 409
```

`UserNotFound` already exists in `src/memory/api/users.py`. Move it to
`src/memory/errors.py` unchanged and import it from there in both routers: a
group route that cannot find a *user* must not answer `GROUP_NOT_FOUND`, and
the second caller is what makes the shared home worth having.

- [ ] **Step 5: Write `src/memory/api/groups.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import ids
from memory.api.app import require_master
from memory.auth.principal import Principal
from memory.db import ensure_tenant, get_session
from memory.errors import GroupAlreadyExists, GroupNotFound, UserNotFound
from memory.models import Group, GroupMember, User

router = APIRouter(prefix="/v1/groups", tags=["groups"])


class CreateGroupRequest(BaseModel):
    id: str | None = None
    name: str | None = None


class GroupResponse(BaseModel):
    group_id: str
    name: str | None
    members: list[str]


def _load(db: Session, principal: Principal, group_id: str) -> Group:
    group = db.get(Group, group_id)
    if group is None or group.tenant_id != principal.tenant_id:
        raise GroupNotFound(group_id=group_id)
    return group


def _members(db: Session, group_id: str) -> list[str]:
    return sorted(
        db.scalars(
            select(GroupMember.user_id).where(GroupMember.group_id == group_id)
        ).all()
    )


@router.post("", status_code=201, response_model=GroupResponse)
def create_group(
    body: CreateGroupRequest,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> GroupResponse:
    ensure_tenant(db, principal.tenant_id)

    group = Group(
        id=body.id or ids.new_group_id(),
        tenant_id=principal.tenant_id,
        name=body.name,
    )
    try:
        with db.begin_nested():
            db.add(group)
    except IntegrityError as exc:
        # Reusing an explicit id is the documented platform-provisioning path,
        # so a duplicate is an ordinary client mistake — 409, not a 500 from
        # the catch-all handler. Same shape as create_user.
        raise GroupAlreadyExists("a group with that id already exists") from exc
    db.commit()
    return GroupResponse(group_id=group.id, name=group.name, members=[])


@router.get("", response_model=list[GroupResponse])
def list_groups(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> list[GroupResponse]:
    groups = db.scalars(
        select(Group).where(Group.tenant_id == principal.tenant_id).order_by(Group.id)
    ).all()
    return [
        GroupResponse(group_id=g.id, name=g.name, members=_members(db, g.id))
        for g in groups
    ]


@router.get("/{group_id}", response_model=GroupResponse)
def get_group(
    group_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> GroupResponse:
    group = _load(db, principal, group_id)
    return GroupResponse(
        group_id=group.id, name=group.name, members=_members(db, group.id)
    )


@router.put("/{group_id}/members/{user_id}", status_code=204)
def add_member(
    group_id: str,
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> Response:
    _load(db, principal, group_id)
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        # The group exists; the user does not. Saying GROUP_NOT_FOUND here
        # would send the caller looking in the wrong place.
        raise UserNotFound(user_id=user_id)

    # PUT is idempotent: adding an existing member is success, not a conflict.
    if db.get(GroupMember, (group_id, user_id)) is None:
        db.add(GroupMember(group_id=group_id, user_id=user_id))
        db.commit()
    return Response(status_code=204)


@router.delete("/{group_id}/members/{user_id}", status_code=204)
def remove_member(
    group_id: str,
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> Response:
    _load(db, principal, group_id)
    membership = db.get(GroupMember, (group_id, user_id))
    if membership is not None:
        db.delete(membership)
        db.commit()
    return Response(status_code=204)
```

- [ ] **Step 6: Wire the router**

In `src/memory/api/app.py`'s `create_app()`, alongside the existing includes:

```python
    from memory.api import groups as group_routes

    app.include_router(group_routes.router)
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_groups_api.py -v`
Expected: PASS — 7 passed

- [ ] **Step 8: Commit**

```bash
git add src/memory/models.py src/memory/errors.py src/memory/api/groups.py src/memory/api/app.py tests/test_groups_api.py
git commit -m "feat: group provisioning and flat membership"
```

---

### Task 4: Project, retired slug and audit models

**Files:**
- Modify: `src/memory/models.py`
- Modify: `src/memory/ids.py`
- Create: `src/memory/audit.py`
- Migration: a new revision under `migrations/versions/`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `memory.models.Base`.
- Produces:
  - `memory.models.Project`, `memory.models.RetiredSlug`, `memory.models.AuditEvent`
  - `memory.ids.new_project_internal_id() -> str`, `memory.ids.new_audit_id() -> str`
  - `memory.audit.record(db, principal, action: str, resource: str, on_behalf_of: str | None = None) -> None`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
def test_project_slug_is_unique_per_tenant(session, tenant):
    from memory.models import Project

    def _project(slug: str) -> Project:
        return Project(
            internal_id=ids.new_project_internal_id(),
            tenant_id=tenant,
            project_slug=slug,
            owner_type="user",
            owner_id="usr_x",
            bank_id=ids.new_project_bank_id(),
        )

    session.add(_project("payments-api"))
    session.flush()
    session.add(_project("payments-api"))

    with pytest.raises(IntegrityError):
        session.flush()


def test_git_locator_is_not_unique(session, tenant):
    """Two projects may legitimately record the same locator (SPEC §17)."""
    from memory.models import Project

    for slug in ("one", "two"):
        session.add(
            Project(
                internal_id=ids.new_project_internal_id(),
                tenant_id=tenant,
                project_slug=slug,
                git_locator="github.com/acme/payments-api",
                owner_type="user",
                owner_id="usr_x",
                bank_id=ids.new_project_bank_id(),
            )
        )
    session.flush()

    assert session.query(Project).count() == 2


def test_retired_slug_points_at_a_project(session, tenant):
    from memory.models import Project, RetiredSlug

    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        project_slug="payments-service",
        owner_type="user",
        owner_id="usr_x",
        bank_id=ids.new_project_bank_id(),
    )
    session.add(project)
    session.flush()
    session.add(
        RetiredSlug(
            tenant_id=tenant,
            retired_slug="github.com-acme-payments-api",
            project_internal_id=project.internal_id,
        )
    )
    session.flush()

    stored = session.get(RetiredSlug, (tenant, "github.com-acme-payments-api"))
    assert stored.project_internal_id == project.internal_id


def test_audit_event_records_the_actor(session, tenant):
    from memory.models import AuditEvent

    session.add(
        AuditEvent(
            id=ids.new_audit_id(),
            tenant_id=tenant,
            actor_key_id=None,
            on_behalf_of="usr_alice",
            action="project.transfer",
            resource="payments-api",
        )
    )
    session.flush()

    assert session.query(AuditEvent).one().on_behalf_of == "usr_alice"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'Project'`

- [ ] **Step 3: Add the id generators**

In `src/memory/ids.py`:

```python
def new_project_internal_id() -> str:
    """Internal only. SPEC inv. 34: never required by an ordinary client."""
    return f"prj_{uuid.uuid4().hex}"


def new_audit_id() -> str:
    return f"aud_{uuid.uuid4().hex}"
```

- [ ] **Step 4: Add the models**

In `src/memory/models.py`, and add `UniqueConstraint` to the `sqlalchemy` import:

```python
class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "project_slug"),)

    # Internal. The public identity is project_slug (SPEC inv. 7).
    internal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    project_slug: Mapped[str] = mapped_column(String(128), index=True)
    # Metadata, never identity and never authorization evidence (inv. 11).
    # Deliberately NOT unique: SPEC §17.
    git_locator: Mapped[str | None] = mapped_column(String(512), nullable=True)
    owner_type: Mapped[str] = mapped_column(String(8))
    owner_id: Mapped[str] = mapped_column(String(128))
    bank_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RetiredSlug(Base):
    __tablename__ = "retired_slugs"

    # A forwarding tombstone (SPEC §8.6). Resolution follows it in ONE hop:
    # on each rename, existing tombstones are repointed at the new slug's
    # project, so there is never a chain to walk and never a cycle.
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    retired_slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_internal_id: Mapped[str] = mapped_column(
        ForeignKey("projects.internal_id")
    )
    retired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NULL for the bootstrap master key, which is configuration not a row.
    actor_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    on_behalf_of: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
```

- [ ] **Step 5: Write `src/memory/audit.py`**

```python
from sqlalchemy.orm import Session

from memory import ids
from memory.auth.principal import Principal
from memory.models import AuditEvent


def record(
    db: Session,
    principal: Principal,
    action: str,
    resource: str,
    on_behalf_of: str | None = None,
) -> None:
    """Append an audit event. Caller commits.

    SPEC §20 MUST: master-key actions, ownership changes and renames are
    recorded.

    on_behalf_of is passed in, never derived from the principal. A master key
    has no identity of its own — principal.user_id is None for every master
    call — so deriving it would make delegation unrecordable in exactly the
    case §5.2 cares about. For a user key it stays None: the caller acts for
    itself, and actor_key_id already says who that is. It is provenance and
    never authorization evidence.
    """
    db.add(
        AuditEvent(
            id=ids.new_audit_id(),
            tenant_id=principal.tenant_id,
            actor_key_id=principal.key_id,
            on_behalf_of=on_behalf_of,
            action=action,
            resource=resource,
        )
    )
```

- [ ] **Step 6: Generate and apply the migration**

```bash
docker compose up -d postgres
uv run alembic revision --autogenerate -m "projects, groups, retired slugs, audit"
uv run alembic upgrade head
```

Expected: a revision creating `groups`, `group_members`, `projects`,
`retired_slugs`, `audit_events`.

- [ ] **Step 7: Prove there is no drift**

```bash
uv run alembic revision --autogenerate -m "drift check"
```

The generated file's `upgrade()` body must be exactly `pass`. If it is not, the
migration does not match the models — fix the migration, do not delete the
evidence. Then remove the drift-check file:

```bash
rm migrations/versions/*drift_check.py
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest -v -m "not integration"`
Expected: PASS, no warnings.

- [ ] **Step 9: Commit**

```bash
git add src/memory/models.py src/memory/ids.py src/memory/audit.py migrations tests/test_models.py
git commit -m "feat: project, retired slug and audit models"
```

---

### Task 5: Project resolution and authorization

**Files:**
- Create: `src/memory/projects.py`
- Modify: `src/memory/errors.py`
- Modify: `src/memory/banks.py`
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `memory.slugs`, `memory.models`, `memory.errors`, `memory.ids`, `memory.auth.principal.Principal`.
- Produces:
  - `memory.errors.ProjectNotFound` (404), `ProjectAccessDenied` (403), `ProjectSlugConflict` (409), `ProjectLocatorMismatch` (409), `ProjectContextUnavailable` (400)
  - `memory.projects.Resolution` — dataclass with `project: Project` and `resolved_from: str | None`
  - `memory.projects.resolve(db, principal, slug, git_locator=None, create=True) -> Resolution`
  - `memory.projects.authorize(db, principal, project) -> None`
  - `memory.banks.resolve_project_bank(db, principal, slug, git_locator=None) -> tuple[str, str | None]`

- [ ] **Step 1: Write the failing test**

`tests/test_projects.py`:

```python
import pytest

from memory import ids, projects
from memory.auth.principal import Principal
from memory.errors import (
    GroupNotFound,
    InvalidOwnerType,
    ProjectAccessDenied,
    ProjectLocatorMismatch,
    ProjectNotFound,
)
from memory.models import Group, GroupMember, Project, User


def _user(session, tenant, user_id: str) -> User:
    user = User(id=user_id, tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return user


def _principal(tenant: str, user_id: str | None, master: bool = False) -> Principal:
    return Principal(
        tenant_id=tenant, user_id=user_id, is_master=master, key_id="key_x"
    )


def test_first_toucher_creates_and_owns_the_project(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    result = projects.resolve(session, juan, "github.com-acme-payments-api")

    assert result.project.owner_type == "user"
    assert result.project.owner_id == "usr_juan"
    assert result.project.bank_id.startswith("project_")
    assert result.resolved_from is None


def test_second_resolution_reuses_the_same_bank(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    first = projects.resolve(session, juan, "payments-api")
    second = projects.resolve(session, juan, "payments-api")

    assert first.project.bank_id == second.project.bank_id


def test_another_user_is_denied_and_told_only_the_owner_type(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    with pytest.raises(ProjectAccessDenied) as caught:
        projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert caught.value.details["project_slug"] == "payments-api"
    assert caught.value.details["owner_type"] == "user"
    assert "owner_id" not in caught.value.details
    assert "usr_juan" not in str(caught.value.details)


def test_no_second_bank_is_created_for_the_denied_caller(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    with pytest.raises(ProjectAccessDenied):
        projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert session.query(Project).count() == 1


def test_group_member_reaches_a_group_owned_project(session, tenant):
    _user(session, tenant, "usr_juan")
    alice = _user(session, tenant, "usr_alice")
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()
    session.add(GroupMember(group_id="grp_payments", user_id=alice.id))
    session.flush()

    result = projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")
    projects.transfer(
        session, _principal(tenant, "usr_juan"), result.project, "group", "grp_payments"
    )

    for_alice = projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")

    assert for_alice.project.bank_id == result.project.bank_id


def test_non_member_is_denied_a_group_owned_project(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_bob")
    session.add(Group(id="grp_payments", tenant_id=tenant))
    session.flush()

    result = projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")
    projects.transfer(
        session, _principal(tenant, "usr_juan"), result.project, "group", "grp_payments"
    )

    with pytest.raises(ProjectAccessDenied):
        projects.resolve(session, _principal(tenant, "usr_bob"), "payments-api")


def test_transfer_between_users_moves_access(session, tenant):
    _user(session, tenant, "usr_juan")
    _user(session, tenant, "usr_alice")
    juan = _principal(tenant, "usr_juan")

    result = projects.resolve(session, juan, "payments-api")
    bank_before = result.project.bank_id
    projects.transfer(session, juan, result.project, "user", "usr_alice")

    for_alice = projects.resolve(session, _principal(tenant, "usr_alice"), "payments-api")
    assert for_alice.project.bank_id == bank_before

    with pytest.raises(ProjectAccessDenied):
        projects.resolve(session, juan, "payments-api")


def test_rename_leaves_a_forwarding_tombstone(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "github.com-acme-payments-api")
    bank_before = result.project.bank_id

    projects.rename(session, juan, result.project, "payments-api")

    forwarded = projects.resolve(session, juan, "github.com-acme-payments-api")

    assert forwarded.project.project_slug == "payments-api"
    assert forwarded.project.bank_id == bank_before
    assert forwarded.resolved_from == "github.com-acme-payments-api"


def test_rename_does_not_create_an_empty_project(session, tenant):
    """The failure this tombstone exists to prevent (SPEC §8.6)."""
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "old-slug")
    projects.rename(session, juan, result.project, "new-slug")

    projects.resolve(session, juan, "old-slug")

    assert session.query(Project).count() == 1


def test_chained_rename_still_resolves_in_one_hop(session, tenant):
    from memory.models import RetiredSlug

    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "a")
    projects.rename(session, juan, result.project, "b")
    projects.rename(session, juan, result.project, "c")

    from_a = projects.resolve(session, juan, "a")
    tombstone = session.get(RetiredSlug, (tenant, "a"))

    assert from_a.project.project_slug == "c"
    assert tombstone.project_internal_id == result.project.internal_id


def test_a_retired_slug_cannot_be_reused(session, tenant):
    from memory.errors import ProjectSlugConflict

    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    result = projects.resolve(session, juan, "a")
    projects.rename(session, juan, result.project, "b")
    second = projects.resolve(session, juan, "c")

    with pytest.raises(ProjectSlugConflict):
        projects.rename(session, juan, second.project, "a")


def test_locator_mismatch_refuses_rather_than_merging(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    projects.resolve(
        session, juan, "payments-api", git_locator="github.com/acme/payments-api"
    )

    with pytest.raises(ProjectLocatorMismatch):
        projects.resolve(
            session,
            juan,
            "payments-api",
            git_locator="gitlab.com/customer/payments-api",
        )


def test_a_caller_without_a_locator_is_unaffected(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    projects.resolve(
        session, juan, "payments-api", git_locator="github.com/acme/payments-api"
    )

    result = projects.resolve(session, juan, "payments-api")

    assert result.project.git_locator == "github.com/acme/payments-api"


def test_an_absent_locator_is_filled_in_for_an_authorized_caller(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")
    projects.resolve(session, juan, "payments-api")

    result = projects.resolve(
        session, juan, "payments-api", git_locator="github.com/acme/payments-api"
    )

    assert result.project.git_locator == "github.com/acme/payments-api"


def test_master_key_reaches_any_project_in_its_tenant(session, tenant):
    _user(session, tenant, "usr_juan")
    projects.resolve(session, _principal(tenant, "usr_juan"), "payments-api")

    result = projects.resolve(
        session, _principal(tenant, None, master=True), "payments-api"
    )

    assert result.project.project_slug == "payments-api"


def test_master_key_does_not_lazily_create(session, tenant):
    """A master key has no identity, so there is no owner to assign (§8.1)."""
    with pytest.raises(ProjectNotFound):
        projects.resolve(session, _principal(tenant, None, master=True), "nope")


def test_slug_is_normalized_on_the_way_in(session, tenant):
    _user(session, tenant, "usr_juan")
    juan = _principal(tenant, "usr_juan")

    created = projects.resolve(session, juan, "Payments API")
    found = projects.resolve(session, juan, "payments-api")

    assert created.project.internal_id == found.project.internal_id
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_projects.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.projects'`

- [ ] **Step 3: Add the error classes**

In `src/memory/errors.py`:

```python
class ProjectNotFound(DomainError):
    code = "PROJECT_NOT_FOUND"
    status = 404


class ProjectAccessDenied(DomainError):
    code = "PROJECT_ACCESS_DENIED"
    status = 403


class ProjectSlugConflict(DomainError):
    code = "PROJECT_SLUG_CONFLICT"
    status = 409


class ProjectLocatorMismatch(DomainError):
    code = "PROJECT_LOCATOR_MISMATCH"
    status = 409


class ProjectContextUnavailable(DomainError):
    code = "PROJECT_CONTEXT_UNAVAILABLE"
    status = 400


class InvalidOwnerType(DomainError):
    code = "INVALID_OWNER_TYPE"
    status = 400
```

- [ ] **Step 4: Write `src/memory/projects.py`**

```python
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import audit, ids
from memory.auth.principal import Principal
from memory.errors import (
    GroupNotFound,
    InvalidOwnerType,
    ProjectAccessDenied,
    ProjectLocatorMismatch,
    ProjectNotFound,
    ProjectSlugConflict,
    UserNotFound,
)
from memory.models import Group, GroupMember, Project, RetiredSlug, User
from memory.slugs import normalize_slug


@dataclass(frozen=True)
class Resolution:
    project: Project
    # The slug the caller asked for, when it was a retired one (SPEC §8.6).
    # None when they used the project's current slug.
    resolved_from: str | None


def _live(db: Session, tenant_id: str, slug: str) -> Project | None:
    return db.scalar(
        select(Project).where(
            Project.tenant_id == tenant_id, Project.project_slug == slug
        )
    )


def _forwarded(db: Session, tenant_id: str, slug: str) -> Project | None:
    tombstone = db.get(RetiredSlug, (tenant_id, slug))
    if tombstone is None:
        return None
    # Tenant-filtered again rather than a bare PK load: the tombstone key is
    # already tenant-scoped, so this is redundant today, but it keeps the
    # isolation guarantee local to this function instead of inferred from a
    # caller three layers up.
    return db.scalar(
        select(Project).where(
            Project.internal_id == tombstone.project_internal_id,
            Project.tenant_id == tenant_id,
        )
    )


def authorize(
    db: Session,
    principal: Principal,
    project: Project,
    requested_slug: str | None = None,
) -> None:
    """SPEC §7. The error names the slug and the owner KIND, never the owner.

    Revealing owner_type turns "denied" into "ask a human or ask for a group",
    which is the recovery path §8.5 trades that disclosure for. Revealing
    owner_id would leak who works on what.

    Echo back the slug the caller ASKED for, not the project's current one: a
    denial after following a tombstone would otherwise disclose the rename
    target to someone who only knew the retired name.
    """
    if principal.is_master:
        return
    if project.owner_type == "user" and project.owner_id == principal.user_id:
        return
    if project.owner_type == "group" and db.get(
        GroupMember, (project.owner_id, principal.user_id)
    ):
        return
    raise ProjectAccessDenied(
        "no access to that project",
        project_slug=requested_slug or project.project_slug,
        owner_type=project.owner_type,
    )


def resolve(
    db: Session,
    principal: Principal,
    slug: str,
    git_locator: str | None = None,
    create: bool = True,
) -> Resolution:
    """Slug -> project, creating it lazily for a user credential.

    Order matters: live projects, then retired slugs, then creation. Checking
    tombstones before creating is what stops a rename from silently producing a
    second, empty project (SPEC §8.6).
    """
    slug = normalize_slug(slug)

    project = _live(db, principal.tenant_id, slug)
    resolved_from = None
    if project is None:
        project = _forwarded(db, principal.tenant_id, slug)
        if project is not None:
            resolved_from = slug

    if project is None:
        if not create or principal.is_master:
            # A master key has no identity, so there is no owner to assign.
            raise ProjectNotFound("no such project", project_slug=slug)
        return Resolution(_create(db, principal, slug, git_locator), None)

    authorize(db, principal, project, requested_slug=slug)

    if git_locator:
        if project.git_locator and project.git_locator != git_locator:
            raise ProjectLocatorMismatch(
                "that project is bound to a different repository",
                project_slug=project.project_slug,
            )
        if not project.git_locator:
            # Enrichment, only for a caller already authorized (SPEC §8.3).
            project.git_locator = git_locator

    return Resolution(project, resolved_from)


def _create(
    db: Session, principal: Principal, slug: str, git_locator: str | None
) -> Project:
    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=principal.tenant_id,
        project_slug=slug,
        git_locator=git_locator,
        owner_type="user",
        owner_id=principal.user_id,
        bank_id=ids.new_project_bank_id(),
    )
    try:
        with db.begin_nested():
            db.add(project)
    except IntegrityError:
        # Lost the creation race (SPEC §9). The winner's project is now the
        # truth; reload it and authorize this caller against it — which is
        # usually a denial, and correctly so. A savepoint, not a bare rollback,
        # so any earlier write in this request survives.
        existing = _live(db, principal.tenant_id, slug)
        if existing is None:
            raise
        authorize(db, principal, existing)
        return existing
    return project


def rename(
    db: Session, principal: Principal, project: Project, new_slug: str
) -> Project:
    """Change the public slug, leaving a forwarding tombstone (SPEC §8.6)."""
    authorize(db, principal, project)
    new_slug = normalize_slug(new_slug)
    if new_slug == project.project_slug:
        return project

    if _live(db, principal.tenant_id, new_slug) or db.get(
        RetiredSlug, (principal.tenant_id, new_slug)
    ):
        # Uniqueness spans live projects AND tombstones (inv. 13): a retired
        # name stays reserved, or a forward would start pointing somewhere new.
        raise ProjectSlugConflict("that slug is taken", project_slug=new_slug)

    old_slug = project.project_slug
    project.project_slug = new_slug

    # Nothing repoints existing tombstones, and nothing needs to: a rename
    # mutates the slug on the same Project row, so internal_id never changes.
    # Every tombstone already points at the row, so resolution after a chain of
    # renames is still ONE lookup — no transitive walk, no cycles.
    db.add(
        RetiredSlug(
            tenant_id=principal.tenant_id,
            retired_slug=old_slug,
            project_internal_id=project.internal_id,
        )
    )
    audit.record(db, principal, "project.rename", f"{old_slug} -> {new_slug}")
    return project


def transfer(
    db: Session,
    principal: Principal,
    project: Project,
    owner_type: str,
    owner_id: str,
) -> Project:
    """Move ownership. Any authorized caller may do this in v1 (inv. 15).

    Accepted consequence, stated in SPEC §6.1: a single group member can
    transfer a group-owned project to themselves and lock the group out. The
    alternative is a group-admin role, and v1 has no permission model. The
    audit event is the mitigation.
    """
    authorize(db, principal, project)
    if owner_type not in ("user", "group"):
        # Guarded here and not only at the API edge: a bad owner_type would
        # make authorize() fall through to a denial for everyone, silently
        # orphaning the project.
        raise InvalidOwnerType("owner type must be user or group")

    # The new owner must exist in this tenant. An unchecked id silently
    # orphans the project: authorize() then denies everyone and only a master
    # key can undo it.
    if owner_type == "user":
        owner = db.get(User, owner_id)
        if owner is None or owner.tenant_id != principal.tenant_id:
            raise UserNotFound(user_id=owner_id)
    else:
        owner = db.get(Group, owner_id)
        if owner is None or owner.tenant_id != principal.tenant_id:
            raise GroupNotFound(group_id=owner_id)

    previous = f"{project.owner_type}:{project.owner_id}"
    project.owner_type = owner_type
    project.owner_id = owner_id
    audit.record(
        db,
        principal,
        "project.transfer",
        f"{project.project_slug}: {previous} -> {owner_type}:{owner_id}",
    )
    return project
```

- [ ] **Step 5: Add `resolve_project_bank` to `src/memory/banks.py`**

```python
def resolve_project_bank(
    db: Session,
    principal: Principal,
    slug: str | None,
    git_locator: str | None = None,
) -> tuple[str, str | None]:
    """Map scope=project to a bank ID. Returns (bank_id, resolved_from)."""
    if not slug:
        raise ProjectContextUnavailable(
            "scope=project needs MEMORY_PROJECT or a Git repository"
        )
    result = projects.resolve(db, principal, slug, git_locator)
    return result.project.bank_id, result.resolved_from
```

with `from memory import projects` and
`from memory.errors import ProjectContextUnavailable` added to its imports.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_projects.py -v`
Expected: PASS — 17 passed

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -v -m "not integration"`
Expected: PASS, no warnings.

- [ ] **Step 8: Commit**

```bash
git add src/memory/projects.py src/memory/banks.py src/memory/errors.py tests/test_projects.py
git commit -m "feat: project resolution, ownership authorization, rename and transfer"
```

---

### Task 6: Project control plane

**Files:**
- Create: `src/memory/api/projects.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_projects_api.py`

**Interfaces:**
- Consumes: `memory.projects`, `memory.api.app.current_principal`, `memory.db.get_session`.
- Produces: routes `POST /v1/projects`, `GET /v1/projects`, `GET /v1/projects/{project_slug}`, `PATCH /v1/projects/{project_slug}`, `PATCH /v1/projects/{project_slug}/owner`.

- [ ] **Step 1: Write the failing test**

`tests/test_projects_api.py`:

```python
import pytest


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


def test_a_user_creates_a_project_owned_by_itself(client, juan, tenant):
    response = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    assert response.status_code == 201
    body = response.json()
    assert body["project_slug"] == "payments-api"
    assert body["owner"] == {"type": "user", "id": juan["user_id"]}


def test_the_project_response_never_exposes_internals(client, juan, tenant):
    body = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    ).json()

    assert "bank_id" not in body
    assert "internal_id" not in body
    assert "prj_" not in str(body)
    assert "project_" not in str(body)


def test_a_user_cannot_create_a_project_owned_by_someone_else(
    client, juan, master_headers, tenant
):
    other = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]

    response = client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "owner": {"type": "user", "id": other}},
        headers=juan["headers"],
    )

    assert response.status_code == 403


def test_master_creates_a_project_owned_by_a_group(client, master_headers, tenant):
    client.post("/v1/groups", json={"id": "grp_payments"}, headers=master_headers)

    response = client.post(
        "/v1/projects",
        json={
            "project_slug": "payments-api",
            "owner": {"type": "group", "id": "grp_payments"},
        },
        headers=master_headers,
    )

    assert response.status_code == 201
    assert response.json()["owner"] == {"type": "group", "id": "grp_payments"}


def test_creating_an_existing_slug_conflicts(client, juan, tenant):
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_SLUG_CONFLICT"


def test_rename_forwards_the_old_slug(client, juan, tenant):
    client.post(
        "/v1/projects",
        json={"project_slug": "github.com-acme-payments-api"},
        headers=juan["headers"],
    )

    renamed = client.patch(
        "/v1/projects/github.com-acme-payments-api",
        json={"project_slug": "payments-api"},
        headers=juan["headers"],
    )
    forwarded = client.get(
        "/v1/projects/github.com-acme-payments-api", headers=juan["headers"]
    )

    assert renamed.status_code == 200
    assert forwarded.status_code == 200
    assert forwarded.json()["project_slug"] == "payments-api"
    assert forwarded.json()["resolved_from"] == "github.com-acme-payments-api"
    assert forwarded.json()["notice"] == "PROJECT_RENAMED"


def test_renaming_onto_a_retired_slug_conflicts(client, juan, tenant):
    client.post("/v1/projects", json={"project_slug": "a"}, headers=juan["headers"])
    client.patch("/v1/projects/a", json={"project_slug": "b"}, headers=juan["headers"])
    client.post("/v1/projects", json={"project_slug": "c"}, headers=juan["headers"])

    response = client.patch(
        "/v1/projects/c", json={"project_slug": "a"}, headers=juan["headers"]
    )

    assert response.status_code == 409


def test_transfer_to_a_group_lets_a_member_in(client, juan, master_headers, tenant):
    alice = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    alice_key = client.post(
        f"/v1/users/{alice}/keys", json={}, headers=master_headers
    ).json()["key"]
    client.post("/v1/groups", json={"id": "grp_payments"}, headers=master_headers)
    client.put(f"/v1/groups/grp_payments/members/{alice}", headers=master_headers)
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    transferred = client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "group", "id": "grp_payments"},
        headers=juan["headers"],
    )
    for_alice = client.get(
        "/v1/projects/payments-api", headers={"Authorization": f"Bearer {alice_key}"}
    )

    assert transferred.status_code == 200
    assert for_alice.status_code == 200


def test_an_outsider_is_denied_without_learning_the_owner(
    client, juan, master_headers, tenant
):
    bob = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    bob_key = client.post(
        f"/v1/users/{bob}/keys", json={}, headers=master_headers
    ).json()["key"]
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )

    response = client.get(
        "/v1/projects/payments-api", headers={"Authorization": f"Bearer {bob_key}"}
    )

    assert response.status_code == 403
    details = response.json()["error"]["details"]
    assert details["project_slug"] == "payments-api"
    assert details["owner_type"] == "user"
    assert juan["user_id"] not in str(response.json())


def test_listing_shows_only_projects_the_caller_can_reach(
    client, juan, master_headers, tenant
):
    bob = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    bob_key = client.post(
        f"/v1/users/{bob}/keys", json={}, headers=master_headers
    ).json()["key"]
    client.post("/v1/projects", json={"project_slug": "mine"}, headers=juan["headers"])
    client.post(
        "/v1/projects",
        json={"project_slug": "theirs"},
        headers={"Authorization": f"Bearer {bob_key}"},
    )

    listed = client.get("/v1/projects", headers=juan["headers"]).json()

    assert [p["project_slug"] for p in listed] == ["mine"]


def test_transfer_writes_an_audit_event(client, juan, master_headers, tenant, session):
    from memory.models import AuditEvent

    client.post("/v1/groups", json={"id": "grp_payments"}, headers=master_headers)
    client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )
    client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "group", "id": "grp_payments"},
        headers=juan["headers"],
    )

    actions = [e.action for e in session.query(AuditEvent).all()]
    assert "project.transfer" in actions
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_projects_api.py -v`
Expected: FAIL — 404 on `/v1/projects`.

- [ ] **Step 3: Write `src/memory/api/projects.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import ids
from memory import projects as domain
from memory.api.app import current_principal
from memory.auth.principal import Principal
from memory.db import get_session
from memory.errors import Forbidden, ProjectSlugConflict
from memory.models import Group, GroupMember, Project, RetiredSlug, User
from memory.slugs import normalize_slug

router = APIRouter(prefix="/v1/projects", tags=["projects"])


class Owner(BaseModel):
    type: str
    id: str


class CreateProjectRequest(BaseModel):
    project_slug: str
    owner: Owner | None = None
    git_locator: str | None = None


class RenameProjectRequest(BaseModel):
    project_slug: str


class ProjectResponse(BaseModel):
    project_slug: str
    owner: Owner
    git_locator: str | None = None
    # Set only when the caller used a retired slug (SPEC §8.6). Not an error:
    # forwarding succeeds, and this tells the client to update its config.
    resolved_from: str | None = None
    notice: str | None = None


def _response(project: Project, resolved_from: str | None = None) -> ProjectResponse:
    """Built field by field. Never serialize the row: it carries bank_id and
    internal_id, neither of which may cross the boundary (inv. 29, inv. 34)."""
    return ProjectResponse(
        project_slug=project.project_slug,
        owner=Owner(type=project.owner_type, id=project.owner_id),
        git_locator=project.git_locator,
        resolved_from=resolved_from,
        notice="PROJECT_RENAMED" if resolved_from else None,
    )


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(
    body: CreateProjectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    slug = normalize_slug(body.project_slug)

    existing = db.scalar(
        select(Project).where(
            Project.tenant_id == principal.tenant_id, Project.project_slug == slug
        )
    )
    retired = db.get(RetiredSlug, (principal.tenant_id, slug))
    if existing is not None or retired is not None:
        # Uniqueness spans live projects AND tombstones (inv. 13).
        raise ProjectSlugConflict("that slug is taken", project_slug=slug)

    owner = body.owner
    if owner is None:
        if principal.is_master:
            raise Forbidden("a master-key create must name an owner")
        owner = Owner(type="user", id=principal.user_id)
    elif not principal.is_master and not (
        owner.type == "user" and owner.id == principal.user_id
    ):
        # A user key may only create a project owned by itself (SPEC §16.2).
        raise Forbidden("a user key may only create a project it owns")

    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=principal.tenant_id,
        project_slug=slug,
        git_locator=body.git_locator,
        owner_type=owner.type,
        owner_id=owner.id,
        bank_id=ids.new_project_bank_id(),
    )
    db.add(project)
    db.commit()
    return _response(project)


@router.get("", response_model=list[ProjectResponse])
def list_projects(
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> list[ProjectResponse]:
    rows = db.scalars(
        select(Project)
        .where(Project.tenant_id == principal.tenant_id)
        .order_by(Project.project_slug)
    ).all()
    if principal.is_master:
        return [_response(p) for p in rows]

    memberships = set(
        db.scalars(
            select(GroupMember.group_id).where(
                GroupMember.user_id == principal.user_id
            )
        ).all()
    )
    visible = [
        p
        for p in rows
        if (p.owner_type == "user" and p.owner_id == principal.user_id)
        or (p.owner_type == "group" and p.owner_id in memberships)
    ]
    return [_response(p) for p in visible]


@router.get("/{project_slug}", response_model=ProjectResponse)
def get_project(
    project_slug: str,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    result = domain.resolve(db, principal, project_slug, create=False)
    return _response(result.project, result.resolved_from)


@router.patch("/{project_slug}", response_model=ProjectResponse)
def rename_project(
    project_slug: str,
    body: RenameProjectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    result = domain.resolve(db, principal, project_slug, create=False)
    project = domain.rename(db, principal, result.project, body.project_slug)
    db.commit()
    return _response(project)


@router.patch("/{project_slug}/owner", response_model=ProjectResponse)
def transfer_project(
    project_slug: str,
    body: Owner,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    result = domain.resolve(db, principal, project_slug, create=False)
    project = domain.transfer(db, principal, result.project, body.type, body.id)
    db.commit()
    return _response(project)
```

- [ ] **Step 4: Wire the router**

In `create_app()`:

```python
    from memory.api import projects as project_routes

    app.include_router(project_routes.router)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_projects_api.py -v`
Expected: PASS — 11 passed

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -v -m "not integration"`
Expected: PASS, no warnings.

- [ ] **Step 7: Commit**

```bash
git add src/memory/api/projects.py src/memory/api/app.py tests/test_projects_api.py
git commit -m "feat: project control plane with rename, transfer and forwarding"
```

---

### Task 7: `scope=project` in the data plane

**Files:**
- Modify: `src/memory/api/memory.py`
- Modify: `tests/test_memory_api.py`
- Modify: `scripts/smoke.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: `memory.banks.resolve_project_bank`.
- Produces: `scope=project` accepted on `retain`, `sync_retain` and `recall`, with `project_slug` and `git_locator` on the request body.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_memory_api.py`:

```python
@pytest.fixture
def two_users(client, master_headers, tenant):
    made = []
    for _ in range(2):
        user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
            "user_id"
        ]
        key = client.post(
            f"/v1/users/{user_id}/keys", json={}, headers=master_headers
        ).json()["key"]
        made.append({"user_id": user_id, "key": key})
    return made


@respx.mock
def test_project_scope_reaches_a_project_bank(client, two_users, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan = two_users[0]

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 200
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_user_and_project_scope_use_different_banks(client, two_users, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan = two_users[0]

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x"},
        headers=_headers(juan["key"]),
    )
    user_url = str(route.calls.last.request.url)
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=_headers(juan["key"]),
    )

    assert user_url != str(route.calls.last.request.url)


@respx.mock
def test_a_stranger_cannot_reach_someone_elses_project(client, two_users, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan, alice = two_users
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=_headers(juan["key"]),
    )

    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "payments-api", "query": "x"},
        headers=_headers(alice["key"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"


def test_project_scope_without_a_slug_is_unavailable(client, two_users, tenant):
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "query": "x"},
        headers=_headers(two_users[0]["key"]),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROJECT_CONTEXT_UNAVAILABLE"
```

Delete `test_project_scope_is_not_implemented_yet` — the behavior it locked in
is exactly what this task removes.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_memory_api.py -v`
Expected: FAIL — the new cases get `INVALID_SCOPE`.

- [ ] **Step 3: Accept project scope in `src/memory/api/memory.py`**

Add the two fields to both request models:

```python
class RetainRequest(BaseModel):
    scope: Scope
    content: str
    user_id: str | None = None
    project_slug: str | None = None
    git_locator: str | None = None
    document_id: str | None = None
    update_mode: str = "replace"
    metadata: dict[str, str] | None = None


class RecallRequest(BaseModel):
    scope: Scope
    query: str
    user_id: str | None = None
    project_slug: str | None = None
    git_locator: str | None = None
```

and replace `_resolve_bank`:

```python
def _resolve_bank(body: RetainRequest | RecallRequest, db: Session, principal: Principal) -> str:
    if body.scope == "user":
        return resolve_user_bank(db, principal, body.user_id)
    bank_id, _resolved_from = resolve_project_bank(
        db, principal, body.project_slug, body.git_locator
    )
    return bank_id
```

Update both call sites to pass `body`. Import `resolve_project_bank` from
`memory.banks` and drop the now-unused `InvalidScope` import if nothing else
uses it.

Both handlers must `db.commit()` after a successful call, because project
resolution can lazily create a project or fill in a `git_locator`. Add it right
before each `return`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -v -m "not integration"`
Expected: PASS, no warnings. Report the count.

- [ ] **Step 5: Extend the smoke test**

In `scripts/smoke.sh`, before the final `echo "PASS: ..."`, add a project
round-trip and an isolation check. `${user_key}` and `${other_key}` already
exist in the script:

```bash
# Project memory: shared where authorized, denied where not.
curl -sf -X POST "${API}/v1/memory/sync_retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"project","project_slug":"smoke-project","content":"Migrations in this project run with alembic upgrade head."}' \
  >/dev/null
echo "retained one project fact"

proj=$(curl -sf -X POST "${API}/v1/memory/recall" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"project","project_slug":"smoke-project","query":"how do migrations run"}')
echo "${proj}" | grep -qi "alembic" \
  || { echo "FAIL: project recall did not return the retained fact" >&2; exit 1; }

denied=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${API}/v1/memory/recall" \
  -H "Authorization: Bearer ${other_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"project","project_slug":"smoke-project","query":"how do migrations run"}')
[ "${denied}" = "403" ] \
  || { echo "FAIL: a stranger reached the project, got HTTP ${denied}" >&2; exit 1; }
echo "project memory is owner-scoped"
```

and change the final line to:

```bash
echo "PASS: user and project memory, isolated, no bank_id leak"
```

- [ ] **Step 6: Run the smoke test against the live stack**

```bash
export MEMORY_MASTER_KEY="mem_local_master_change_me"
export MEMORY_MASTER_KEY_HASH=$(python3 -c \
  "import hashlib,os; print(hashlib.sha256(os.environ['MEMORY_MASTER_KEY'].encode()).hexdigest())")
docker compose up -d --build api
docker compose run --rm api python -m alembic upgrade head
./scripts/smoke.sh
```

Expected: `PASS: user and project memory, isolated, no bank_id leak`

- [ ] **Step 7: Update `README.md`**

Replace the "What works today" route list with one that includes the project
control plane and the group provisioning routes, and change the scoping
paragraph to say that `scope=project` now works, that a project is resolved by
`project_slug`, and that the first authenticated user to create a slug owns it.
Remove the sentence saying `scope=project` is refused.

- [ ] **Step 8: Commit**

```bash
git add src/memory/api/memory.py tests/test_memory_api.py scripts/smoke.sh README.md
git commit -m "feat: scope=project end to end"
```

---

## Done when

- `uv run pytest -m "not integration"` is green with no warnings.
- `./scripts/smoke.sh` prints `PASS: user and project memory, isolated, no bank_id leak`.
- `grep -rn "internal_id\|bank_id" src/memory/api/` shows them only in the
  response builders that strip them.
- A rename followed by a resolution of the old slug returns the project, not a
  new empty one.

## Deliberately not in this plan

The MCP server and its tools, provenance metadata and reserved-key validation
(§13.4), documents, curation, operations (Plan 3) · directives, mental models,
the admin plane and Helm (Plan 4) · project aliases as live second names, which
§22 lists as a non-goal · a group-admin role, which would be a permission model
and v1 has none · rate limiting.
