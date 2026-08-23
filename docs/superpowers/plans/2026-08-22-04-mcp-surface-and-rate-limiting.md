# MCP Surface and Rate Limiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the fifteen MCP tools of SPEC §11, over the REST surface that
already exists, and close the last unimplemented SPEC §20 MUST by rate-limiting
writes per credential.

**Architecture:** The MCP server mounts inside the same FastAPI app and its
tools call the **same domain functions the REST routes call** — `_resolve_bank`,
the Hindsight client, `provenance.build`. Nothing is re-implemented, so the MCP
cannot drift from the REST surface's authorization and cannot bypass it. Tools
are plain `def`: the SDK runs them in an AnyIO worker thread, so this project's
synchronous stack works unchanged.

**Tech Stack:** Python 3.12, `mcp==2.0.0`, FastAPI, Pydantic v2, SQLAlchemy 2.0
sync, httpx, pytest, `uv`.

## Global Constraints

- The Hindsight `bank_id` never crosses the MCP boundary — not in a result, an
  error, or a log. Neither does a project's `prj_` internal id. **The LLM never
  supplies `bank_id`, `tenant_id`, its authenticated `user_id`, ownership, or
  any authorization data** (SPEC §11.1); every tool takes `scope` and resolves
  the rest server-side.
- Every tool follows the same pipeline (SPEC §11.1): authenticate → resolve
  user/project → authorize → resolve the internal bank id → attach provenance →
  invoke Hindsight → normalize. Authentication, scope resolution, authorization
  and bank resolution **must be centralized** — one code path, shared with REST.
- **The excluded set is not advertised** (SPEC §11.6, §11.7): `clear_memories`,
  `delete_bank`, `get_bank`, `update_bank`, `get_bank_stats`, `list_banks`,
  `create_bank`, `dry-run-refresh`, `list_tags`, `retry_operation`,
  `delete_operation`, mental-model management, directive management, project
  rename and ownership, and project/group/key administration. Exclusion is
  enforced by not registering the tool.
- `get_operation`, `list_operations` and `cancel_operation` stay **three tools**
  (SPEC §11.5). Do not collapse them into `manage_operations(action=…)`: get and
  list are read-only, cancel mutates, and separate tools keep the annotations
  honest.
- v1 writes **no retrieval tags** (SPEC §13.6).
- Reserved metadata keys return `INVALID_METADATA` and nothing is written
  (SPEC §13.4). Audit-only runtime fields never reach extraction (§13.2).
- SPEC §18's error-code list is closed. A tool failure must map onto it.
- `uv` for dependencies. Never `pip install` outside the venv.

---

## Measured facts about `mcp==2.0.0`

Verified against a running server on 2026-08-22, not read from documentation.
Every one differs from the published examples.

```python
from mcp.server.mcpserver import MCPServer, Context   # NOT FastMCP
```

- **A tool may be a plain `def`.** It runs in an AnyIO worker thread —
  confirmed by reading `threading.current_thread().name` inside one. So sync
  SQLAlchemy and sync httpx work inside a tool with no `to_thread` wrapper and
  without blocking the event loop. **Do not write `async def` tools**; they
  would run on the loop and every database call would block it.
- Headers arrive as `Context.headers`, a `Mapping[str, str] | None`. The SDK's
  own docstring: *"Headers are client-supplied input - never treat one as an
  identity assertion."*
- Returning a pydantic `BaseModel` gives structured output. Returning a `dict`
  leaves `structured_content` as `None`.
- Mounting under FastAPI requires the **host app's** lifespan to enter
  `mcp.session_manager.run()`. Starlette does not run nested lifespans under a
  `Mount`; forget it and the server accepts connections and then hangs.
- Client side: `streamable_http_client(url, http_client=…)` yields a **2-tuple**
  `(read, write)`, takes **no** `headers=` argument (put them on the
  `httpx2.AsyncClient`), and results are snake_case — `structured_content`,
  `is_error`, `Tool.input_schema`.

---

## The fifteen tools

Every tool takes `scope: Literal["user","project"]` plus, optionally,
`project_slug` and `git_locator`. None takes `user_id`: an MCP caller is a user
key acting for itself, and a master key has no identity to act as (§11.1).

| Tool | Extra parameters | Domain call |
|---|---|---|
| `retain` | `content`, `document_id?`, `update_mode?`, `metadata?`, `operation_id?` | `client.retain(..., is_async=True)` |
| `sync_retain` | same as `retain` | `client.retain(..., is_async=False)` |
| `recall` | `query` | `client.recall` |
| `reflect` | `query` | `client.reflect` |
| `list_memories` | `q?`, `type?`, `state?`, `document_id?`, `limit?`, `offset?` | `client.list_memories` |
| `get_memory` | `memory_id` | `client.get_memory` |
| `forget` | `memory_id`, `reason?` | `client.curate(state="invalidated")` |
| `correct` | `memory_id`, `content` | `client.curate(text=…)` |
| `restore` | `memory_id` | `client.curate(state="valid")` |
| `list_documents` | `q?`, `limit?`, `offset?` | `client.list_documents` |
| `get_document` | `document_id` | `client.get_document` |
| `delete_document` | `document_id` | `client.delete_document` |
| `get_operation` | `operation_id` | `client.get_operation` |
| `list_operations` | `status?`, `type?`, `limit?`, `offset?` | `client.list_operations` |
| `cancel_operation` | `operation_id` | `client.cancel_operation` |

Annotations (`ToolAnnotations`) matter — they are what an MCP client shows a
user before allowing a call:

```text
read_only_hint=True   recall reflect list_memories get_memory
                      list_documents get_document get_operation list_operations
destructive_hint=True forget delete_document cancel_operation
idempotent_hint=True  sync_retain restore
```

`delete_document` is the only irreversible one (SPEC §12.2) and must say so in
its description: it removes the document *and every memory derived from it*.
`forget` must say it invalidates rather than deletes and is reversible with
`restore` (§12.1) — an agent that believes `forget` destroys evidence will reach
for something heavier.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/memory/mcp/__init__.py` (create) | package marker |
| `src/memory/mcp/server.py` (create) | `build_mcp()`, the shared tool pipeline, auth from headers |
| `src/memory/mcp/tools.py` (create) | the fifteen registrations |
| `src/memory/api/app.py` (modify) | mount, and the lifespan that runs the session manager |
| `src/memory/ratelimit.py` (create) | per-credential write limiting |
| `src/memory/config.py` (modify) | the limit settings |
| `tests/test_mcp_server.py` (create) | tool surface, auth, pipeline |
| `tests/test_mcp_tools.py` (create) | per-tool behavior and IDOR |
| `tests/test_ratelimit.py` (create) | the limiter |

---

### Task 1: MCP scaffolding — mount, authenticate, and one shared pipeline

**Files:**
- Create: `src/memory/mcp/__init__.py`, `src/memory/mcp/server.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `memory.auth.principal.resolve_principal`, `memory.db.get_session_factory` (see Step 3 — you may need to expose one), `memory.api.memory._resolve_bank`, `_strip_bank_id`.
- Produces: `memory.mcp.server.build_mcp() -> MCPServer`, `ToolContext`, `tool_session(ctx)`.

**The problem this task solves.** A REST route gets its `Principal` and its
`Session` from FastAPI dependencies. An MCP tool has neither — it gets a
`Context` with raw headers. So the pipeline has to be reassembled once, here,
and every tool must go through it. If a tool ever opens its own session or
parses its own header, the centralization SPEC §11.1 requires is gone.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_server.py`:

```python
import httpx2
import pytest
from mcp.server.mcpserver import MCPServer

from memory.mcp import server as mcp_server


def test_build_mcp_returns_a_server_with_no_tools_of_its_own():
    """Scaffolding only. The tools land in Task 2 and after."""
    mcp = mcp_server.build_mcp()

    assert isinstance(mcp, MCPServer)


def test_a_missing_authorization_header_is_unauthorized(tenant):
    from memory.errors import Unauthorized

    with pytest.raises(Unauthorized):
        with mcp_server.tool_session(_headers({})):
            pass


def test_a_bad_key_is_unauthorized(tenant):
    from memory.errors import Unauthorized

    with pytest.raises(Unauthorized):
        with mcp_server.tool_session(_headers({"authorization": "Bearer nope"})):
            pass


def test_a_valid_user_key_yields_its_own_principal(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    with mcp_server.tool_session(_headers({"authorization": f"Bearer {key}"})) as tc:
        assert tc.principal.user_id == user_id
        assert tc.principal.is_master is False


def test_the_session_is_closed_when_the_tool_returns(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    with mcp_server.tool_session(_headers({"authorization": f"Bearer {key}"})) as tc:
        session = tc.db
    assert not session.is_active or session.get_bind() is not None


def _headers(mapping: dict[str, str]):
    """A stand-in for mcp.Context, which carries only what the pipeline reads."""

    class _Ctx:
        headers = mapping

    return _Ctx()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.mcp'`.

- [ ] **Step 3: Expose a session factory**

`memory.db` currently offers `get_session`, a FastAPI dependency. A tool needs a
session outside the request cycle. Add, in `src/memory/db.py`, next to it:

```python
@contextmanager
def session_scope() -> Iterator[Session]:
    """A Session outside the FastAPI request cycle, for MCP tools.

    Same discipline as get_session: rolls back on exception, never commits.
    Callers commit explicitly, before they return, for the same reason —
    a commit that runs after the caller already has its answer cannot be
    reported to it.
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

Import `contextmanager` from `contextlib` and `Iterator` from
`collections.abc`. Do not duplicate `_session_factory`; reuse the one the
module already has, so the test suite's dependency override and the production
engine stay one thing.

- [ ] **Step 4: Write `src/memory/mcp/server.py`**

```python
"""The MCP surface's shared pipeline.

Every tool goes through `tool_session`. A REST route gets its Principal and its
Session from FastAPI dependencies; a tool gets neither, only raw headers — so
the pipeline is reassembled once, here. SPEC §11.1 requires authentication,
scope resolution, authorization and bank resolution to be centralized, and a
tool that parsed its own header or opened its own session would end that.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, resolve_principal
from memory.db import session_scope


class HasHeaders(Protocol):
    """What the pipeline needs from an mcp Context: nothing but headers."""

    headers: Mapping[str, str] | None


@dataclass
class ToolContext:
    principal: Principal
    db: Session


@contextmanager
def tool_session(ctx: HasHeaders) -> Iterator[ToolContext]:
    """Authenticate the caller and open a session for one tool call.

    The header is client-supplied input and is treated as a credential to be
    verified, never as an identity assertion — `resolve_principal` is the same
    function the REST surface uses, so an MCP caller cannot become anyone a
    REST caller could not.
    """
    headers = ctx.headers or {}
    authorization = headers.get("authorization") or headers.get("Authorization")

    with session_scope() as db:
        yield ToolContext(principal=resolve_principal(authorization, db), db=db)


def build_mcp() -> MCPServer:
    """The server, with no tools registered yet.

    Tools are added by `memory.mcp.tools.register(mcp)`, which the app calls.
    Keeping registration out of this module is what makes the exclusion test in
    Task 6 meaningful: the advertised set is one list in one place.
    """
    return MCPServer(
        name="ach-memory",
        instructions=(
            "Durable memory for coding agents. `scope` selects whose memory: "
            "'user' is your own, 'project' is the shared memory of the project "
            "named by project_slug. You never supply a bank id."
        ),
    )
```

- [ ] **Step 5: Mount it**

In `src/memory/api/app.py`, inside `create_app()`:

```python
    from memory.mcp.server import build_mcp

    mcp = build_mcp()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Starlette does not run nested lifespans under a Mount, so the HOST
        # app must enter the session manager. Without this the server accepts
        # connections and then hangs — with no error to explain it.
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="ach-memory", version="0.1.0", lifespan=lifespan)
```

and after the routers are included:

```python
    app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))
```

`create_app()` builds the `FastAPI` before the routers today; the lifespan must
be passed to that constructor, so build `mcp` first. Import `contextlib` and
`AsyncIterator` from `collections.abc`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS — 5 passed.

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -m "not integration"`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/memory/mcp src/memory/api/app.py src/memory/db.py tests/test_mcp_server.py
git commit -m "mount an MCP server and centralize its tool pipeline"
```

---

### Task 2: The four core memory tools

**Files:**
- Create: `src/memory/mcp/tools.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `tool_session`, `memory.api.memory._resolve_bank`, `_strip_bank_id`, `memory.provenance`, `memory.hindsight.client.get_client`.
- Produces: `memory.mcp.tools.register(mcp)`; tools `retain`, `sync_retain`, `recall`, `reflect`.

**What the tools must reuse, and why.** `_resolve_bank` performs scope
resolution, project authorization, retired-slug forwarding and bank resolution
in one place, and it is the function every REST route uses. A tool calls it with
a `ScopedRequest` it constructs from its own arguments. Do not re-derive any of
that: the whole point of §11.1's "must be centralized" is that the MCP surface
inherits every property the REST surface was tested for.

Note `_resolve_bank` takes `create` — `retain`, `sync_retain`, `recall` and
`reflect` are first-touch paths and keep the default `True`; every tool in
Tasks 3–5 is a lookup and must pass `create=False`, or any caller can
permanently squat project slugs.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_tools.py`:

```python
import httpx
import pytest
import respx

BASE = "http://hindsight.test"


@pytest.fixture
def call_tool(app, client, master_headers, tenant):
    """Invoke a registered tool the way the SDK would, with real headers.

    Goes through the same registry the transport uses, so a tool that is not
    registered raises here exactly as it would over the wire.
    """
    from memory.mcp import tools as tool_module

    def _make_user() -> str:
        user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
            "user_id"
        ]
        return client.post(
            f"/v1/users/{user_id}/keys", json={}, headers=master_headers
        ).json()["key"]

    def _call(name: str, key: str, **kwargs):
        class _Ctx:
            headers = {"authorization": f"Bearer {key}"}

        return tool_module.REGISTRY[name](ctx=_Ctx(), **kwargs)

    _call.make_user = _make_user
    return _call


@respx.mock
def test_retain_reaches_the_callers_own_bank(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    key = call_tool.make_user()

    result = call_tool("retain", key, scope="user", content="uv, not pip")

    assert result.result == {"operation_id": "op_1"}
    assert "banks/user_" in str(route.calls.last.request.url)


@respx.mock
def test_a_tool_never_returns_a_bank_id(call_tool):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(
            200,
            json={"bank_id": "user_leak", "results": [{"chunk_id": "user_leak_d_0"}]},
        )
    )
    key = call_tool.make_user()

    result = call_tool("recall", key, scope="user", query="deps")

    assert "user_leak" not in str(result.model_dump())


@respx.mock
def test_a_tool_cannot_reach_another_users_project(call_tool):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    from memory.errors import ProjectAccessDenied

    with pytest.raises(ProjectAccessDenied):
        call_tool("recall", alice, scope="project", project_slug="payments", query="x")


@respx.mock
def test_a_reserved_metadata_key_is_refused_and_nothing_is_retained(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    key = call_tool.make_user()

    from memory.errors import InvalidMetadata

    with pytest.raises(InvalidMetadata):
        call_tool(
            "retain", key, scope="user", content="x", metadata={"user_id": "someone"}
        )

    assert route.call_count == 0


@respx.mock
def test_reflect_reaches_the_reflect_endpoint(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "uv"})
    )
    key = call_tool.make_user()

    call_tool("reflect", key, scope="user", query="deps?")

    assert route.call_count == 1


def _mock_bank() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/config").mock(
        return_value=httpx.Response(200, json={})
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_mcp_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.mcp.tools'`.

- [ ] **Step 3: Write `src/memory/mcp/tools.py`**

```python
"""The fifteen MCP tools of SPEC §11.

Each is a plain `def` on purpose: the SDK runs a synchronous tool in an AnyIO
worker thread, so this project's synchronous stack works here unchanged, while
an `async def` would run on the event loop and block it on every database call.

Every tool body is the same four lines — build a ScopedRequest, run the shared
pipeline, call the client, strip the bank id — because SPEC §11.1 requires the
authenticate/resolve/authorize/bank steps to be centralized. A tool that grew
its own version of any of them would be the bug.
"""

from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from memory import provenance
from memory.api.memory import ScopedRequest, _resolve_bank, _strip_bank_id
from memory.hindsight.client import get_client
from memory.mcp.server import tool_session

Scope = Literal["user", "project"]

# Populated by register(). The tests call through it, so an unregistered tool
# fails there exactly as it would over the wire.
REGISTRY: dict[str, Any] = {}


class ToolResult(BaseModel):
    """A BaseModel, not a dict: the SDK only emits structured output for one."""

    result: dict[str, Any]
    # Set only when the caller used a retired project slug (SPEC §8.6).
    project_slug: str | None = None
    resolved_from: str | None = None
    notice: str | None = None


def _run(ctx: Context, scope: Scope, project_slug: str | None,
         git_locator: str | None, create: bool, call) -> ToolResult:
    """The shared pipeline. `call` receives (bank_id, db) and returns a dict."""
    body = ScopedRequest(
        scope=scope, project_slug=project_slug, git_locator=git_locator
    )
    with tool_session(ctx) as tc:
        bank_id, resolved_from, slug = _resolve_bank(
            body, tc.db, tc.principal, create=create
        )
        # Commit before the upstream call: resolution may have created the
        # project that owns this bank_id, and rolling that back after the bank
        # is materialized upstream orphans it unreachably.
        tc.db.commit()
        result = call(bank_id, tc.db, tc.principal)
    return ToolResult(
        result=_strip_bank_id(result, bank_id),
        project_slug=slug,
        resolved_from=resolved_from,
        notice="PROJECT_RENAMED" if resolved_from else None,
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Store something worth remembering. Returns immediately with an "
            "operation you can follow with get_operation; use sync_retain when "
            "you need to read it back straight away."
        ),
    )
    def retain(
        scope: Scope,
        content: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        document_id: str | None = None,
        update_mode: str = "replace",
        metadata: dict[str, str] | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        return _retain(
            ctx, scope, content, project_slug, git_locator, document_id,
            update_mode, metadata, operation_id, is_async=True,
        )

    @mcp.tool(
        description="Store something and wait until it is searchable.",
        annotations=ToolAnnotations(idempotent_hint=True),
    )
    def sync_retain(
        scope: Scope,
        content: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        document_id: str | None = None,
        update_mode: str = "replace",
        metadata: dict[str, str] | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        return _retain(
            ctx, scope, content, project_slug, git_locator, document_id,
            update_mode, metadata, operation_id, is_async=False,
        )

    @mcp.tool(
        description="Search memory and return the matching facts.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def recall(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, True,
            lambda bank, db, p: get_client().recall(bank, query),
        )

    @mcp.tool(
        description=(
            "Ask memory a question and get a synthesized answer rather than "
            "a list of facts. Costs more than recall."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def reflect(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, True,
            lambda bank, db, p: get_client().reflect(bank, query),
        )

    REGISTRY.update(
        retain=retain, sync_retain=sync_retain, recall=recall, reflect=reflect
    )


def _retain(ctx, scope, content, project_slug, git_locator, document_id,
            update_mode, metadata, operation_id, *, is_async: bool) -> ToolResult:
    def call(bank_id, db, principal):
        extraction, _audit = provenance.build(
            metadata, project_slug=project_slug, user_id=principal.user_id,
            on_behalf_of=None,
        )
        client = get_client()
        client.ensure_bank(bank_id)
        return client.retain(
            bank_id, content, document_id=document_id,
            metadata=extraction or None, context=provenance.context_line(extraction),
            update_mode=update_mode, is_async=is_async, operation_id=operation_id,
        )

    return _run(ctx, scope, project_slug, git_locator, True, call)
```

**Note on `provenance.build`:** it must run *before* the upstream call so a
reserved key means nothing is written. `_run` commits before `call`, so a
reserved key still leaves a lazily created project behind — acceptable, and the
same as the REST path — but the Hindsight call must not happen. Check that
`call` raises before `ensure_bank`; if `build` is invoked after it, move it.

- [ ] **Step 4: Register the tools in `create_app()`**

```python
    from memory.mcp.tools import register as register_tools

    register_tools(mcp)
```

immediately after `mcp = build_mcp()`.

- [ ] **Step 5: Run the tests, then the suite**

Run: `uv run pytest tests/test_mcp_tools.py -v` — 5 passed.
Run: `uv run pytest -m "not integration"`.

- [ ] **Step 6: Commit**

```bash
git add src/memory/mcp/tools.py src/memory/api/app.py tests/test_mcp_tools.py
git commit -m "add the four core memory tools"
```

---

### Task 3: The five curation tools

**Files:** Modify `src/memory/mcp/tools.py`, `tests/test_mcp_tools.py`.

**Interfaces:** Produces `list_memories`, `get_memory`, `forget`, `correct`,
`restore` in `REGISTRY`.

Each follows the Task 2 pattern exactly: a `@mcp.tool()` whose body is a single
`_run(...)` call with the right `call` lambda, **and `create=False`** — these
are maintenance on things that already exist, and passing `True` lets any caller
permanently squat project slugs by looping a lookup.

Signatures and bodies:

```python
    @mcp.tool(
        description="List stored memories, most recent first.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def list_memories(
        scope: Scope, ctx: Context, project_slug: str | None = None,
        git_locator: str | None = None, q: str | None = None,
        type: str | None = None, state: str | None = None,
        document_id: str | None = None, limit: int | None = None,
        offset: int | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().list_memories(
                bank, q=q, type=type, state=state, document_id=document_id,
                limit=limit, offset=offset,
            ),
        )

    @mcp.tool(
        description="Fetch one memory by id.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def get_memory(
        scope: Scope, memory_id: str, ctx: Context,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().get_memory(bank, memory_id),
        )

    @mcp.tool(
        description=(
            "Retire a memory that is wrong or obsolete. It is invalidated, not "
            "deleted: it leaves the active set but the record survives, and "
            "restore brings it back."
        ),
        annotations=ToolAnnotations(destructive_hint=True),
    )
    def forget(
        scope: Scope, memory_id: str, ctx: Context, reason: str | None = None,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().curate(
                bank, memory_id, state="invalidated", reason=reason
            ),
        )

    @mcp.tool(description="Replace the text of an existing memory.")
    def correct(
        scope: Scope, memory_id: str, content: str, ctx: Context,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().curate(bank, memory_id, text=content),
        )

    @mcp.tool(
        description="Bring back a memory that forget retired.",
        annotations=ToolAnnotations(idempotent_hint=True),
    )
    def restore(
        scope: Scope, memory_id: str, ctx: Context,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().curate(bank, memory_id, state="valid"),
        )
```

and extend the `REGISTRY.update(...)` call with all five.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_tools.py`. Use a **valid UUID** for `memory_id` — the
Hindsight client rejects a non-UUID locally before any HTTP call, so an IDOR
test using `"mem_1"` would pass whether or not authorization ran. That mistake
was shipped twice in the previous plan.

```python
GHOST = "22222222-2222-2222-2222-222222222222"


@respx.mock
def test_forget_invalidates_rather_than_deleting(call_tool):
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    key = call_tool.make_user()

    call_tool("forget", key, scope="user", memory_id=GHOST, reason="wrong")

    assert b'"state":"invalidated"' in route.calls.last.request.read()


@respx.mock
def test_restore_reverts_it(call_tool):
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    key = call_tool.make_user()

    call_tool("restore", key, scope="user", memory_id=GHOST)

    assert b'"state":"valid"' in route.calls.last.request.read()


@respx.mock
def test_a_curation_tool_does_not_create_a_project(call_tool, session):
    from memory.models import Project
    from memory.errors import ProjectNotFound

    _mock_bank()
    key = call_tool.make_user()

    with pytest.raises(ProjectNotFound):
        call_tool(
            "list_memories", key, scope="project", project_slug="never-seen"
        )

    assert session.query(Project).filter_by(project_slug="never-seen").count() == 0


@respx.mock
def test_idor_a_curation_tool_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    curate = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(ProjectAccessDenied):
        call_tool(
            "correct", alice, scope="project", project_slug="payments",
            memory_id=GHOST, content="mine now",
        )

    assert curate.call_count == 0
```

- [ ] **Step 2: Run, implement, run** — as Task 2.
- [ ] **Step 3: Commit**

```bash
git commit -am "add the five curation tools"
```

---

### Task 4: The three document tools

**Files:** Modify `src/memory/mcp/tools.py`, `tests/test_mcp_tools.py`.

Same pattern, `create=False`. `document_id` is caller-managed and arbitrary
(SPEC §11.4) — `github:acme/payments-api:pr:382` is a legitimate id — so it is
**not** UUID-validated. The path builder already refuses traversal-shaped ids;
do not add a second check, and do not namespace the id by user or agent.

```python
    @mcp.tool(
        description=(
            "List the documents memories were derived from. A document id is "
            "yours to choose — a PR, a file, a session — and is shared by "
            "everyone authorized for this memory."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def list_documents(
        scope: Scope, ctx: Context, project_slug: str | None = None,
        git_locator: str | None = None, q: str | None = None,
        limit: int | None = None, offset: int | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().list_documents(
                bank, q=q, limit=limit, offset=offset
            ),
        )

    @mcp.tool(
        description="Fetch one document by its id.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def get_document(
        scope: Scope, document_id: str, ctx: Context,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().get_document(bank, document_id),
        )

    @mcp.tool(
        description=(
            "Delete a document AND every memory derived from it. This is "
            "irreversible — unlike forget, nothing restores it. The document "
            "is shared, so this affects everyone using this memory."
        ),
        annotations=ToolAnnotations(destructive_hint=True),
    )
    def delete_document(
        scope: Scope, document_id: str, ctx: Context,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().delete_document(bank, document_id),
        )
```

- [ ] **Step 1: Write the failing tests** — a document id with colons and
  slashes reaching Hindsight **verbatim** (assert on
  `route.calls.last.request.url`, not on the mock matching: asserting the mock
  matched is what hid a critical traversal defect in the previous plan), a
  traversal-shaped id refused with no upstream call, and an IDOR case on
  `delete_document`.
- [ ] **Step 2: Run, implement, run.**
- [ ] **Step 3: Commit** — `git commit -am "add the three document tools"`

---

### Task 5: The three operation tools

**Files:** Modify `src/memory/mcp/tools.py`, `tests/test_mcp_tools.py`.

Same pattern, `create=False`. Three tools, never one with an `action` argument
(SPEC §11.5). No `retry_operation`, no `delete_operation`.

```python
    @mcp.tool(
        description="Check whether an async retain has finished.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def get_operation(
        scope: Scope, operation_id: str, ctx: Context,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().get_operation(bank, operation_id),
        )

    @mcp.tool(
        description="List recent async operations.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def list_operations(
        scope: Scope, ctx: Context, project_slug: str | None = None,
        git_locator: str | None = None, status: str | None = None,
        type: str | None = None, limit: int | None = None,
        offset: int | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().list_operations(
                bank, status=status, type=type, limit=limit, offset=offset
            ),
        )

    @mcp.tool(
        description="Cancel a pending async operation.",
        annotations=ToolAnnotations(destructive_hint=True),
    )
    def cancel_operation(
        scope: Scope, operation_id: str, ctx: Context,
        project_slug: str | None = None, git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx, scope, project_slug, git_locator, False,
            lambda bank, db, p: get_client().cancel_operation(bank, operation_id),
        )
```

- [ ] **Step 1: Write the failing tests** — `cancel_operation` reaching
  `DELETE .../operations/{id}` and **not** `.../{id}/delete`, and an IDOR case
  with a valid UUID.
- [ ] **Step 2: Run, implement, run.**
- [ ] **Step 3: Commit** — `git commit -am "add the three operation tools"`

---

### Task 6: Pin the advertised surface

**Files:** Test only — `tests/test_mcp_tools.py`.

**Interfaces:** Consumes `build_mcp` and `register`.

The exclusions in SPEC §11.6 and §11.7 are enforced by nobody having written the
tool. That is true right up until somebody does. The REST side already has a
frozen-route-set test; this is its MCP twin, and it is the cheapest test in the
plan.

- [ ] **Step 1: Write the test**

```python
EXPECTED_TOOLS = {
    "retain", "sync_retain", "recall", "reflect",
    "list_memories", "get_memory", "forget", "correct", "restore",
    "list_documents", "get_document", "delete_document",
    "get_operation", "list_operations", "cancel_operation",
}

# SPEC §11.6 and §11.7. Each is excluded for a stated reason: whole-bank
# destruction an LLM would reach for when it decides memory is "stale"; bank
# configuration that is policy for every user of a project; shared, persistent
# state that steers future agents; a "dry run" whose name invites the model to
# treat it as free when it costs exactly the same as the real thing.
FORBIDDEN_TOOLS = {
    "clear_memories", "delete_bank", "get_bank", "update_bank", "get_bank_stats",
    "list_banks", "create_bank", "dry_run_refresh", "dry-run-refresh",
    "list_tags", "retry_operation", "delete_operation",
    "create_mental_model", "get_mental_model", "list_mental_models",
    "update_mental_model", "refresh_mental_model", "clear_mental_model",
    "delete_mental_model",
    "create_directive", "list_directives", "delete_directive",
    "rename_project", "transfer_project", "create_project",
    "create_user", "create_group", "create_key",
}


@pytest.mark.anyio
async def test_the_advertised_tool_surface_is_exactly_the_spec_set():
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    advertised = {t.name for t in await mcp.list_tools()}

    assert advertised == EXPECTED_TOOLS
    assert advertised & FORBIDDEN_TOOLS == set()
```

`pytest.mark.anyio` needs `anyio_backend` — add to `tests/conftest.py`:

```python
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

- [ ] **Step 2: Run it, confirm it passes, then confirm it BITES**

Register a throwaway `delete_bank` tool, re-run, confirm the test fails, remove
it. A frozen-list test that cannot fail is worse than none.

- [ ] **Step 3: Commit** — `git commit -am "pin the advertised MCP tool surface"`

---

### Task 7: Rate-limit writes per credential

**Files:**
- Create: `src/memory/ratelimit.py`, `tests/test_ratelimit.py`
- Modify: `src/memory/config.py`, `src/memory/api/memory.py`, `src/memory/mcp/tools.py`, `src/memory/errors.py`

**Interfaces:**
- Produces: `ratelimit.check(principal)`, `errors.RateLimited`.

**Why now.** "Rate-limit memory writes per credential" is the last unimplemented
MUST in SPEC §20, and this plan multiplies the surface it protects: fifteen
tools, an LLM deciding when to call them, and `reflect` spending model tokens on
a server-level LiteLLM key with **no per-user cost attribution** (§19.4). An
unbounded `reflect` loop from one key is a billing incident, not a tidiness
issue.

**Scope it honestly.** A single-process in-memory limiter is the right v1: it is
a few lines, it needs no Redis, and it bounds the runaway case that actually
threatens us. It does **not** survive a restart and does **not** coordinate
across replicas — say so in the docstring and in `PROJECT-STATE.md` rather than
implying a guarantee it cannot make.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from memory.errors import RateLimited
from memory.ratelimit import Limiter


def test_it_allows_up_to_the_limit_then_refuses():
    limiter = Limiter(limit=3, window_seconds=60.0, now=iter([1.0, 1.1, 1.2, 1.3]).__next__)

    for _ in range(3):
        limiter.check("key_a")
    with pytest.raises(RateLimited):
        limiter.check("key_a")


def test_credentials_are_counted_separately():
    limiter = Limiter(limit=1, window_seconds=60.0, now=lambda: 1.0)

    limiter.check("key_a")
    limiter.check("key_b")


def test_the_window_slides():
    clock = iter([1.0, 2.0, 100.0])
    limiter = Limiter(limit=1, window_seconds=60.0, now=clock.__next__)

    limiter.check("key_a")
    with pytest.raises(RateLimited):
        limiter.check("key_a")
    limiter.check("key_a")  # the first call has aged out


def test_the_error_says_when_to_retry():
    limiter = Limiter(limit=1, window_seconds=60.0, now=lambda: 1.0)
    limiter.check("key_a")

    with pytest.raises(RateLimited) as caught:
        limiter.check("key_a")

    assert caught.value.details["retry_after_seconds"] > 0
```

- [ ] **Step 2: Run it and watch it fail.**

- [ ] **Step 3: Add the error**

```python
class RateLimited(DomainError):
    code = "RATE_LIMITED"
    status = 429
```

`RATE_LIMITED` is not in SPEC §18's list. Add it there in the same commit, with
one line saying what it means — §18 is a closed list and this plan is what makes
it incomplete. That is a contract amendment, not drift, and it belongs in the
change that causes it.

- [ ] **Step 4: Write `src/memory/ratelimit.py`**

A sliding window of timestamps per credential id. Keyed on `principal.key_id`
so it is per credential, and falling back to a constant for the master key —
whose traffic is ACH's, not a human's, and which SPEC §5.2 already treats as
privileged. Evict entries older than the window on each check so the dict cannot
grow without bound.

Include, verbatim, the honesty about what it is not:

```python
    """In-process, per-credential sliding window.

    Deliberately not Redis-backed. This bounds the runaway case that actually
    threatens us — one key looping retain or reflect — and needs no new
    infrastructure to do it. What it does NOT do: survive a restart, or
    coordinate across replicas. With N replicas the effective limit is N times
    the configured one. Say so before relying on it as a quota.
    """
```

- [ ] **Step 5: Wire it into the write paths**

The REST write routes (`retain`, `sync_retain`, `forget`, `correct`, `restore`,
`documents/delete`, `operations/cancel`) and `reflect` — which is a read but the
expensive one — plus their MCP twins. The single shared place is
`memory.api.memory._resolve_bank`'s callers; decide whether to check inside the
pipeline or at each route, and say why in your report. Checking in one place is
better if you can express "this is a write" there; if you cannot, per-route is
honest and greppable.

- [ ] **Step 6: Settings**

`MEMORY_WRITE_LIMIT` (default a number you can defend — state your reasoning)
and `MEMORY_WRITE_WINDOW_SECONDS`. Both in `src/memory/config.py`, following the
existing settings' shape.

- [ ] **Step 7: Run the suite and commit**

```bash
git commit -am "rate-limit writes per credential"
```

---

### Task 8: Prove the MCP against a live stack, and document it

**Files:** Create `scripts/mcp-smoke.py`; modify `README.md`, `docs/PROJECT-STATE.md`.

**Interfaces:** Consumes the running stack.

**Why a separate script.** `scripts/smoke.sh` is curl against REST. The MCP
needs an MCP client, which means Python and the SDK's async client. Keep them
separate rather than bending one into the other.

- [ ] **Step 1: Write `scripts/mcp-smoke.py`**

It must, against the live stack: connect over streamable HTTP with a real user
key; `list_tools` and assert the set equals the fifteen; `sync_retain` a fact;
`recall` it and check the content came back; `list_memories`, `forget` the id,
confirm it leaves the active set, `restore` it; chain an async `retain`'s
`operation_id` into `get_operation`; and assert **no bank id in any response** —
scan every result for `user_`/`project_` followed by a UUID, the same check
`smoke.sh` does, because that is the check that has actually caught a real leak.

Use the measured client API: `streamable_http_client(url, http_client=…)`
yielding a 2-tuple, auth on the `httpx2.AsyncClient`, and snake_case result
attributes.

Give it a bounded wait for the endpoint and an explicit failure path — never a
naked `until … sleep` loop.

- [ ] **Step 2: Run it**

```bash
docker compose up -d --build
docker compose run --rm api python -m alembic upgrade head
uv run python scripts/mcp-smoke.py
```

Expected: `PASS: 15 tools, retain -> recall -> forget -> restore, operation
followed, no bank_id leak`.

If a tool behaves differently against real Hindsight than against the mocks,
**that is a real finding**: fix the code and the mocks together and report it.
Three of the previous plan's most important defects were found exactly this way,
and one of them was a bank id leaking through a field no mock included.

- [ ] **Step 3: Document**

`README.md`: how to point an agent at the MCP endpoint, the fifteen tools and
what each is for, and a plain statement that `forget` is reversible while
`delete_document` is not. `docs/PROJECT-STATE.md`: the state table, the test
count, and anything the live MCP run taught that the mocks did not.

- [ ] **Step 4: Commit**

```bash
git add scripts/mcp-smoke.py README.md docs/PROJECT-STATE.md
git commit -m "prove the MCP surface against live Hindsight, document it"
```

---

## Done when

- `uv run pytest -m "not integration"` is green with no warnings.
- `./scripts/smoke.sh` and `uv run python scripts/mcp-smoke.py` both pass.
- The advertised tool set is exactly the fifteen, pinned by a test that has been
  shown to fail when a forbidden tool is registered.
- Every tool with a secondary id has an IDOR test using a **valid UUID**, so the
  assertion observes the bank check rather than the client's local id guard.
- No tool result, error or log contains a `bank_id` or a `prj_` internal id.
- A write past the limit returns `RATE_LIMITED`, and SPEC §18 lists that code.

## Deliberately not in this plan

Directives and mental models (REST, API-only, SPEC §14) · the admin plane:
`clear_memories`, `delete_bank`, `GET /v1/admin/audit`, retired-slug release
(§16.4) · Helm packaging · the Memory Defense tier verification (§25 item 13) ·
`MEMORY_PROJECT` / Git-locator derivation, which is the MCP *client's* job
(§10) — the wrapper takes a `project_slug` · retrieval tags (§13.6) · a
distributed rate limiter · the five control-plane GETs that write no audit event.
