# Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an operator a live view of what agents are doing against this service — Prometheus metrics, a per-call activity trail, and a static console page.

**Architecture:** Two hooks cover both surfaces. `_resolve_bank()` (the single choke point every REST route and all fifteen MCP tools already pass through) fills a per-call record; a finalizer in the REST ASGI middleware and in MCP's `_run` writes one `activity_events` row and bumps the Prometheus counters once the outcome is known. Aggregate, low-cardinality data goes to `/metrics`; per-identity detail goes to Postgres and is read back through two master-key admin routes that a single static HTML page consumes.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, `prometheus-client`, vanilla JS.

**Spec:** `docs/superpowers/specs/2026-08-26-observability-design.md`. The approved visual design is `docs/superpowers/specs/2026-08-26-observability-dashboard-mock.html` — Task 9 turns that exact file into the shipped page.

## Global Constraints

- Every task's requirements implicitly include this section.
- **`bank_id` must never leave the service** (SPEC invariant 29). Store and expose `sha256(bank_id)[:12]` only. `tests/test_leakscan.py` is the gate.
- **No memory content is ever copied into our database.** A copy would survive `DELETE /v1/admin/memory/{scope}`, breaking SPEC §12.3's only complete erasure path.
- **No `user_id` or `project_slug` as a Prometheus label.** Every label value must come from a closed set.
- **Telemetry never breaks a served request.** Every write of a row or a metric is inside a `try` whose `except` logs and continues.
- **Never log an exception's text or an upstream URL** — they can carry SQL, a connection string, or a bank id. Log `type(exc).__name__`. This is why `httpx` is muted at WARNING in `api/app.py` and why `hide_parameters=True` is set in `db.py`.
- **The context propagation rule, which is the trap in this whole plan:** the REST routes are sync `def`, so they run in Starlette's threadpool, and MCP tools run in AnyIO's worker pool. A `ContextVar.set()` performed *inside* a route or a tool is **not** visible to the ASGI middleware afterwards — the child context is a copy. Therefore the middleware sets a **mutable dict** into the ContextVar *before* calling downstream, and `_resolve_bank` **mutates that dict** rather than rebinding the var. Rebinding the var from inside a route silently produces zero rows and passes every mock-level test.
- Python ≥3.12, `uv run pytest -m "not integration"` is the test command, `ruff` line length 100.
- Follow the house comment style: the "why" lives next to the code, especially where a decision looks arbitrary.

---

### Task 1: Metrics module and the `/metrics` endpoint

**Files:**
- Create: `src/memory/metrics.py`
- Modify: `pyproject.toml`, `src/memory/config.py`, `src/memory/api/app.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `memory.metrics.CALLS`, `CALL_DURATION`, `CONTENT_BYTES`, `ERRORS`, `HINDSIGHT`, `HTTP`, `BUILD` (prometheus_client collectors); `Settings.metrics_enabled: bool`, `Settings.admin_ui_enabled: bool`, `Settings.activity_retention_days: int`.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, inside `[project].dependencies`, after the `pyjwt[crypto]` entry:

```toml
    # Exposition format and histogram bucketing for GET /metrics. Production
    # code (src/memory/metrics.py), so it belongs here and not in the dev
    # group -- the same mistake `mcp` and `mcp-types` each shipped once,
    # which crash-looped the image because `uv export --no-dev` dropped them.
    "prometheus-client>=0.21",
```

Run: `uv sync`

- [ ] **Step 2: Write the failing test**

Create `tests/test_metrics.py`:

```python
"""GET /metrics, and the label discipline that keeps it scrapeable.

Cardinality is the whole risk here: a label whose values come from caller
input turns one time series into unbounded thousands, and the failure lands
on the Prometheus, not on us.
"""


def test_metrics_exposes_build_info(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "memory_build_info" in response.text


def test_metrics_declares_every_collector(client):
    body = client.get("/metrics").text

    for name in (
        "memory_calls_total",
        "memory_call_duration_seconds",
        "memory_content_bytes_total",
        "memory_errors_total",
        "memory_hindsight_request_seconds",
        "memory_http_requests_total",
    ):
        assert name in body, name


def test_metrics_can_be_turned_off(configured_env, monkeypatch):
    """The route is not registered at all, rather than answering 403.

    A deployment that turns metrics off should not advertise that it has
    them.
    """
    from fastapi.testclient import TestClient

    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_METRICS_ENABLED", "false")
    get_settings.cache_clear()

    assert TestClient(create_app()).get("/metrics").status_code == 404
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL — 404 on `/metrics`.

- [ ] **Step 4: Write `src/memory/metrics.py`**

```python
"""Prometheus collectors. Aggregate only -- never per-identity.

Every label value here comes from a closed set: an action name chosen in our
own source, a two-value scope, a two-value surface, an outcome, a SPEC §18
error code, an HTTP method, a route TEMPLATE. `user_id` and `project_slug`
are deliberately absent: label values multiply into separate time series, so
a caller-supplied one is an unbounded-cardinality hole that kills the
scraping Prometheus rather than this service. Per-identity detail is the
activity table's job (memory/activity.py) -- that is why both exist.

Single-process by design: the Dockerfile runs one uvicorn worker with no
`--workers`, so prometheus_client's default in-process registry is correct
and no multiprocess directory is needed. Replicas are separate scrape
targets and sum in PromQL.
"""

from importlib.metadata import PackageNotFoundError, version

from prometheus_client import Counter, Gauge, Histogram

CALLS = Counter(
    "memory_calls_total",
    "Data-plane calls that resolved a bank.",
    ["action", "scope", "surface", "outcome"],
)

CALL_DURATION = Histogram(
    "memory_call_duration_seconds",
    "Wall time of a data-plane call, credential check included.",
    ["action", "surface"],
)

CONTENT_BYTES = Counter(
    "memory_content_bytes_total",
    "Bytes of content accepted for retention.",
    ["scope"],
)

ERRORS = Counter(
    "memory_errors_total",
    "Errors reported to a caller, by SPEC §18 code.",
    ["code"],
)

HINDSIGHT = Histogram(
    "memory_hindsight_request_seconds",
    "Upstream Hindsight calls.",
    # `method` and `status`, never the path: a Hindsight path carries the
    # bank id, so labelling by it would both leak the id into every scrape
    # and make the label unbounded. Which upstream operation is slow can be
    # added later as an explicit closed-set label; "is Hindsight slow or
    # erroring" is answered without it.
    ["method", "status"],
)

HTTP = Counter(
    "memory_http_requests_total",
    "HTTP requests, including those that never reached a bank.",
    # `route` is the FastAPI route TEMPLATE ("/v1/memory/{scope}"), never the
    # raw path -- otherwise a caller mints a new time series per URL they
    # invent. Overlaps memory_calls_total on purpose: this one sees the 401s
    # and the provisioning routes, while over MCP every tool is the same
    # POST /mcp and only memory_calls_total can tell the fifteen apart.
    ["route", "method", "status"],
)

BUILD = Gauge("memory_build_info", "Deployed version.", ["version"])


def _version() -> str:
    try:
        return version("ach-memory")
    except PackageNotFoundError:  # running from a source tree, not installed
        return "unknown"


BUILD.labels(version=_version()).set(1)
```

- [ ] **Step 5: Add the settings**

In `src/memory/config.py`, after the `write_window_seconds` field:

```python
    # Observability. Metrics carry no identities, no project names and no
    # content -- only counts by action, scope, surface, outcome and error
    # code -- so the endpoint is unauthenticated, which is what a Prometheus
    # scrape config expects. The flag exists so a deployment can withdraw it
    # without a code change.
    metrics_enabled: bool = True
    admin_ui_enabled: bool = True
    # Activity rows are operational telemetry, not the audit trail: they age
    # out. 0 disables pruning entirely.
    activity_retention_days: int = Field(default=30, ge=0)
```

- [ ] **Step 6: Register the route**

In `src/memory/api/app.py`, inside `create_app()`, after the `app.include_router(...)` block and before the `/mcp` mount:

```python
    if get_settings().metrics_enabled:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        @app.get("/metrics", include_in_schema=False)
        def metrics() -> Response:
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

Add `Response` to the existing `fastapi.responses` import line:

```python
from fastapi.responses import JSONResponse, Response
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: PASS (3 tests)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/memory/metrics.py src/memory/config.py src/memory/api/app.py tests/test_metrics.py
git commit -m "feat(metrics): expose Prometheus collectors on /metrics"
```

---

### Task 2: HTTP request metrics and the error-code counter

**Files:**
- Create: `src/memory/api/observability.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `memory.metrics.HTTP`, `memory.metrics.ERRORS`.
- Produces: `memory.api.observability.ObservabilityMiddleware(app)` — a pure ASGI middleware. Task 6 extends its `finally` block; nothing else may.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_metrics.py`:

```python
from prometheus_client import REGISTRY


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_http_requests_are_counted_by_route_template(client):
    before = _sample("memory_http_requests_total", route="/metrics", method="GET", status="200")

    client.get("/metrics")

    after = _sample("memory_http_requests_total", route="/metrics", method="GET", status="200")
    assert after == before + 1


def test_an_unmatched_path_cannot_mint_a_label(client):
    """A 404 has no route object, so it must collapse to one fixed label --
    otherwise every invented URL is a new time series."""
    client.get("/nope-a")
    client.get("/nope-b")

    assert _sample("memory_http_requests_total", route="unmatched", method="GET", status="404") >= 2


def test_a_domain_error_increments_its_code(client):
    before = _sample("memory_errors_total", code="UNAUTHORIZED")

    client.post("/v1/memory/recall", json={"scope": "user", "query": "x"})

    assert _sample("memory_errors_total", code="UNAUTHORIZED") == before + 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL — the counters stay at 0.

- [ ] **Step 3: Write the middleware**

Create `src/memory/api/observability.py`:

```python
"""Request-scoped observability, wired as a pure ASGI middleware.

Pure ASGI rather than BaseHTTPMiddleware for one reason that matters later:
Task 6 puts a mutable per-call record in a ContextVar here, before calling
downstream, and reads it back afterwards. BaseHTTPMiddleware runs the
downstream app in a separate task, and the sync routes below it run in a
threadpool -- in both cases the child gets a COPY of the context, so a
`.set()` performed down there never reaches us. Passing a mutable dict DOWN
and mutating it is what survives both boundaries.
"""

from memory import metrics


class ObservabilityMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 500 is the honest default: if the response never starts, the client
        # got nothing, and recording that as anything else would hide it.
        status = 500

        async def _send(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            # Set by Starlette's router once a path matches. "unmatched" for
            # everything else: without the fallback, every 404 on an invented
            # URL would be its own label value.
            route = scope.get("route")
            metrics.HTTP.labels(
                route=getattr(route, "path", "unmatched"),
                method=scope.get("method", "-"),
                status=str(status),
            ).inc()
```

- [ ] **Step 4: Wire it and count error codes**

In `src/memory/api/app.py`:

```python
from memory.api.observability import ObservabilityMiddleware
```

Inside `create_app()`, immediately after `app = FastAPI(...)`:

```python
    app.add_middleware(ObservabilityMiddleware)
```

In the `_domain_error` handler, as its first statement:

```python
        metrics.ERRORS.labels(code=exc.code).inc()
```

In the `_unhandled` handler, as its first statement:

```python
        metrics.ERRORS.labels(code="INTERNAL_ERROR").inc()
```

Add `from memory import metrics` to the imports.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_metrics.py tests/test_app.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/memory/api/observability.py src/memory/api/app.py tests/test_metrics.py
git commit -m "feat(metrics): count HTTP requests by route template and errors by code"
```

---

### Task 3: Upstream Hindsight timing

**Files:**
- Modify: `src/memory/hindsight/client.py`
- Test: `tests/test_hindsight_client.py`

**Interfaces:**
- Consumes: `memory.metrics.HINDSIGHT`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_hindsight_client.py`, following the `respx` pattern already used there:

```python
def test_upstream_calls_are_timed(respx_mock, client_factory):
    """`method` and `status` only. The path carries the bank id and must
    never become a label."""
    from prometheus_client import REGISTRY

    respx_mock.post(url__regex=r".*/memories$").respond(200, json={"ok": True})
    before = REGISTRY.get_sample_value(
        "memory_hindsight_request_seconds_count", {"method": "POST", "status": "200"}
    ) or 0.0

    client_factory().retain("user_x", "hello")

    after = REGISTRY.get_sample_value(
        "memory_hindsight_request_seconds_count", {"method": "POST", "status": "200"}
    )
    assert after == before + 1
```

If `client_factory` does not exist in that file, build the client the way its neighbouring tests already do and keep the assertion identical.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_hindsight_client.py -k timed -v`
Expected: FAIL — sample is `None`.

- [ ] **Step 3: Instrument `_request`**

In `src/memory/hindsight/client.py`, add `import time` and `from memory import metrics`, then wrap the transport call. The existing body starts:

```python
        response = None
        try:
            response = self._http.request(
                method, path, json=payload, params=params,
                timeout=timeout or self._default_timeout,
            )
```

Becomes:

```python
        response = None
        started = time.monotonic()
        # "error" until proven otherwise: a transport failure never yields a
        # status code, and it is the case an operator most needs to see.
        upstream_status = "error"
        try:
            response = self._http.request(
                method, path, json=payload, params=params,
                timeout=timeout or self._default_timeout,
            )
            upstream_status = str(response.status_code)
```

and add, as the last clause of that same `try` statement (after the existing `except` blocks):

```python
        finally:
            metrics.HINDSIGHT.labels(method=method, status=upstream_status).observe(
                time.monotonic() - started
            )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_hindsight_client.py -v`
Expected: PASS (whole file — the `finally` must not change any existing raise path)

- [ ] **Step 5: Commit**

```bash
git add src/memory/hindsight/client.py tests/test_hindsight_client.py
git commit -m "feat(metrics): time upstream Hindsight calls by method and status"
```

---

### Task 4: The `activity_events` table

**Files:**
- Modify: `src/memory/models.py`, `src/memory/ids.py`
- Create: `migrations/versions/<hash>_activity_events.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `memory.models.ActivityEvent`, `memory.ids.new_activity_id() -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_activity_event_stamps_created_at_from_the_database(session):
    """server_default only, no Python-side default -- the same reasoning
    AuditEvent.created_at records: one clock, the database's, or ordering
    across replicas means nothing."""
    from memory import ids
    from memory.models import ActivityEvent

    row = ActivityEvent(
        id=ids.new_activity_id(),
        tenant_id="default",
        action="memory.retain",
        surface="mcp",
        scope="user",
        user_id="usr_1",
        bank_fingerprint="a" * 12,
        outcome="ok",
        duration_ms=12,
    )
    session.add(row)
    session.flush()

    assert row.created_at is not None
    assert row.id.startswith("act_")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_models.py -k activity -v`
Expected: FAIL — `ImportError: cannot import name 'ActivityEvent'`

- [ ] **Step 3: Add the id helper**

In `src/memory/ids.py`, after `new_audit_id`:

```python
def new_activity_id() -> str:
    return f"act_{uuid.uuid4().hex}"
```

- [ ] **Step 4: Add the model**

In `src/memory/models.py`, after `AuditEvent`:

```python
class ActivityEvent(Base):
    """One row per data-plane call. Operational telemetry, NOT the audit
    trail -- these rows age out (memory.activity._prune), while audit_events
    do not.

    No content column, ever. A copy of memory content here would survive
    `DELETE /v1/admin/memory/{scope}`, so SPEC §12.3's only complete erasure
    path would quietly stop being complete. Bytes and a document id say
    enough for "did the write land"; the content itself is read live from
    Hindsight by whoever is authorized to read that bank.
    """

    __tablename__ = "activity_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    # NULL for the master key, exactly as AuditEvent.actor_key_id.
    credential_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    surface: Mapped[str] = mapped_column(String(4))
    scope: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The RESOLVED slug, never the caller's raw argument -- a rename
    # tombstone must not make one project look like two.
    project_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # sha256(bank_id)[:12]. The bank id itself may not leave this service
    # (SPEC inv. 29), and this is enough to correlate a row with Hindsight's
    # own logs without being reversible.
    bank_fingerprint: Mapped[str] = mapped_column(String(16))
    document_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_bytes: Mapped[int | None] = mapped_column(nullable=True)
    # Client-declared and unverified: it is a label that tells two agents
    # apart when they share one credential, never attribution.
    agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(8))
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        # server_default only, no `default=utcnow` -- see AuditEvent.created_at
        # for why having both silently defeats the server default.
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
```

- [ ] **Step 5: Generate and edit the migration**

Run: `uv run alembic revision -m "activity events"`

Then write its body (the head is `557ed1ef53de`; keep whatever revision hash Alembic generated):

```python
def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "activity_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("credential_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("surface", sa.String(length=4), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("project_slug", sa.String(length=128), nullable=True),
        sa.Column("bank_fingerprint", sa.String(length=16), nullable=False),
        sa.Column("document_id", sa.String(length=256), nullable=True),
        sa.Column("content_bytes", sa.Integer(), nullable=True),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_activity_events_tenant_id"), "activity_events", ["tenant_id"])
    op.create_index(op.f("ix_activity_events_action"), "activity_events", ["action"])
    op.create_index(op.f("ix_activity_events_created_at"), "activity_events", ["created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_activity_events_created_at"), table_name="activity_events")
    op.drop_index(op.f("ix_activity_events_action"), table_name="activity_events")
    op.drop_index(op.f("ix_activity_events_tenant_id"), table_name="activity_events")
    op.drop_table("activity_events")
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 7: Verify the migration matches the model**

Run: `uv run alembic upgrade head && uv run alembic check`
Expected: "No new upgrade operations detected."

- [ ] **Step 8: Commit**

```bash
git add src/memory/models.py src/memory/ids.py migrations/versions/ tests/test_models.py
git commit -m "feat(activity): add the activity_events table"
```

---

### Task 5: The activity recorder

**Files:**
- Create: `src/memory/activity.py`
- Test: `tests/test_activity.py`

**Interfaces:**
- Consumes: `memory.models.ActivityEvent`, `memory.ids.new_activity_id`, `memory.db.session_scope`, `memory.metrics.*`, `memory.config.get_settings`.
- Produces:
  - `activity.new_call() -> None`
  - `activity.describe(**fields) -> None`
  - `activity.set_error(code: str) -> None`
  - `activity.finish(surface: str) -> None`
  - `activity.fingerprint(bank_id: str) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_activity.py`:

```python
"""The recorder, tested directly. Its wiring into the two surfaces is
covered by tests/test_activity_api.py."""

from datetime import UTC, datetime, timedelta

from memory import activity, ids
from memory.models import ActivityEvent


def test_fingerprint_is_stable_and_hides_the_bank_id():
    bank = "project_ba378411-348d-4eb2-9c74-ef0c9da982cc"

    fp = activity.fingerprint(bank)

    assert fp == activity.fingerprint(bank)
    assert len(fp) == 12
    assert bank not in fp
    assert fp not in bank


def test_finish_without_an_action_writes_nothing(app, session):
    """A request that never reached _resolve_bank -- a rejected credential,
    a scrape of /metrics -- has no bank to report. Recording those would also
    hand an unauthenticated caller an unbounded INSERT on a public ingress."""
    activity.new_call()

    activity.finish("rest")

    assert session.query(ActivityEvent).count() == 0


def test_finish_writes_one_row(app, session, tenant):
    activity.new_call()
    activity.describe(
        action="memory.retain",
        scope="user",
        tenant_id=tenant,
        credential_id="key_1",
        user_id="usr_1",
        bank_fingerprint="a" * 12,
        content_bytes=41,
    )

    activity.finish("mcp")

    row = session.query(ActivityEvent).one()
    assert (row.action, row.surface, row.outcome) == ("memory.retain", "mcp", "ok")
    assert row.content_bytes == 41
    assert row.duration_ms >= 0


def test_an_error_code_makes_the_outcome_an_error(app, session, tenant):
    activity.new_call()
    activity.describe(
        action="memory.recall", scope="user", tenant_id=tenant,
        bank_fingerprint="b" * 12,
    )
    activity.set_error("HINDSIGHT_ERROR")

    activity.finish("rest")

    row = session.query(ActivityEvent).one()
    assert (row.outcome, row.error_code) == ("error", "HINDSIGHT_ERROR")


def test_an_over_long_agent_is_truncated_not_rejected(app, session, tenant):
    """agent is client-supplied and the column is String(64). Unscreened, an
    over-long value is a psycopg DataError inside the finalizer -- telemetry
    turning a served request into a 500 is the one thing it may never do."""
    activity.new_call()
    activity.describe(
        action="memory.retain", scope="user", tenant_id=tenant,
        bank_fingerprint="c" * 12, agent="x" * 500,
    )

    activity.finish("mcp")

    assert len(session.query(ActivityEvent).one().agent) == 64


def test_prune_drops_rows_past_the_horizon(app, session, tenant, monkeypatch):
    old = ActivityEvent(
        id=ids.new_activity_id(), tenant_id=tenant, action="memory.recall",
        surface="rest", scope="user", bank_fingerprint="d" * 12,
        outcome="ok", duration_ms=1,
        created_at=datetime.now(UTC) - timedelta(days=90),
    )
    session.add(old)
    session.flush()
    monkeypatch.setattr(activity, "_last_prune", 0.0)

    activity.new_call()
    activity.describe(
        action="memory.recall", scope="user", tenant_id=tenant,
        bank_fingerprint="e" * 12,
    )
    activity.finish("rest")

    remaining = session.query(ActivityEvent).all()
    assert len(remaining) == 1
    assert remaining[0].bank_fingerprint == "e" * 12
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_activity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memory.activity'`

- [ ] **Step 3: Write `src/memory/activity.py`**

```python
"""One row per data-plane call, plus the metrics that go with it.

Two hooks cover both surfaces. `new_call()` runs at the edge (the REST
middleware, MCP's `_run`) and puts a MUTABLE dict in a ContextVar.
`describe()` runs deep inside the call -- in `_resolve_bank`, which every
REST route and all fifteen MCP tools already funnel through -- and MUTATES
that dict. `finish()` runs back at the edge and writes the row.

The mutation is load-bearing, not a style choice. Sync FastAPI routes run in
Starlette's threadpool and MCP tools in AnyIO's worker pool; both get a COPY
of the context, so a `ContextVar.set()` performed down there is invisible up
here. Passing the dict down and mutating it is what survives the boundary.
Rebind the var from inside a route and this module silently records nothing,
while every mock-level test still passes.

The row is written ONCE, at the end, never inserted-then-updated: one round
trip, and a row can never claim a retain succeeded when Hindsight answered
502.
"""

import hashlib
import logging
import time
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from memory import ids, metrics
from memory.config import get_settings
from memory.db import session_scope
from memory.models import ActivityEvent

logger = logging.getLogger("memory.activity")

_call: ContextVar[dict | None] = ContextVar("memory_activity_call", default=None)

# Module-level, so pruning costs one DELETE an hour per process instead of
# one per call. N replicas run the same idempotent DELETE N times, which is
# cheaper than any coordination would be.
_PRUNE_INTERVAL_SECONDS = 3600.0
_last_prune = 0.0

_AGENT_MAX = 64


def fingerprint(bank_id: str) -> str:
    """What stands in for the bank id everywhere it would otherwise appear.

    SPEC inv. 29: the bank id may not leave this service. This is stable
    across restarts (so it correlates with Hindsight's own logs) and not
    reversible.
    """
    return hashlib.sha256(bank_id.encode()).hexdigest()[:12]


def new_call() -> None:
    _call.set({"t0": time.monotonic()})


def describe(**fields: object) -> None:
    """Fill in what this call turned out to be. No-op outside a call."""
    call = _call.get()
    if call is not None:
        call.update(fields)


def set_error(code: str) -> None:
    describe(error_code=code)


def finish(surface: str) -> None:
    """Write the row and the metrics. Never raises."""
    call = _call.get()
    _call.set(None)
    # No action means the call never reached _resolve_bank: a rejected
    # credential, a /metrics scrape, a 404. There is no bank to report -- and
    # on a public ingress, recording anonymous traffic would be an unbounded
    # INSERT vector. Rejections are still visible as
    # memory_errors_total{code="UNAUTHORIZED"}.
    if call is None or "action" not in call:
        return

    duration = time.monotonic() - call["t0"]
    outcome = "error" if call.get("error_code") else "ok"
    action = call["action"]
    scope = call["scope"]

    try:
        metrics.CALLS.labels(
            action=action, scope=scope, surface=surface, outcome=outcome
        ).inc()
        metrics.CALL_DURATION.labels(action=action, surface=surface).observe(duration)
        if call.get("content_bytes"):
            metrics.CONTENT_BYTES.labels(scope=scope).inc(call["content_bytes"])

        agent = call.get("agent")
        with session_scope() as db:
            db.add(
                ActivityEvent(
                    id=ids.new_activity_id(),
                    tenant_id=call["tenant_id"],
                    credential_id=call.get("credential_id"),
                    action=action,
                    surface=surface,
                    scope=scope,
                    user_id=call.get("user_id"),
                    project_slug=call.get("project_slug"),
                    bank_fingerprint=call["bank_fingerprint"],
                    document_id=call.get("document_id"),
                    content_bytes=call.get("content_bytes"),
                    # Truncated, not rejected: `agent` is client-supplied and
                    # the column is String(64). An over-long value would be a
                    # psycopg DataError raised from telemetry -- which must
                    # never be what turns a served request into a 500.
                    agent=str(agent)[:_AGENT_MAX] if agent else None,
                    outcome=outcome,
                    error_code=call.get("error_code"),
                    duration_ms=int(duration * 1000),
                )
            )
            db.commit()
            _prune(db)
    except Exception as exc:
        # Telemetry is never worth an error the caller can see. Type only --
        # the exception text can carry SQL or a bank id, which is why
        # db.py sets hide_parameters=True.
        logger.warning("activity not recorded: %s", type(exc).__name__)


def _prune(db: Session) -> None:
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune = now

    days = get_settings().activity_retention_days
    if days <= 0:  # 0 means keep everything
        return
    cutoff = datetime.now(UTC) - timedelta(days=days)
    db.execute(delete(ActivityEvent).where(ActivityEvent.created_at < cutoff))
    db.commit()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_activity.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/memory/activity.py tests/test_activity.py
git commit -m "feat(activity): record one row per call, written once at the end"
```

---

### Task 6: Wire the recorder into the REST surface

**Files:**
- Modify: `src/memory/api/memory.py`, `src/memory/api/observability.py`, `src/memory/api/app.py`
- Test: `tests/test_activity_api.py`

**Interfaces:**
- Consumes: `activity.new_call/describe/set_error/finish/fingerprint`.
- Produces: `_resolve_bank` now calls `activity.describe(...)` before returning; the middleware calls `new_call()` and `finish("rest")`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_activity_api.py` (follow the `respx` and header conventions in `tests/test_memory_api.py`):

```python
from memory.models import ActivityEvent


def test_a_retain_records_one_row(client, session, user_key_headers, hindsight_mock):
    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "hello", "metadata": {"agent": "claude-code"}},
        headers=user_key_headers,
    )

    row = session.query(ActivityEvent).one()
    assert (row.action, row.surface, row.scope, row.outcome) == (
        "memory.retain", "rest", "user", "ok",
    )
    assert row.content_bytes == len("hello")
    assert row.agent == "claude-code"
    assert row.bank_fingerprint and len(row.bank_fingerprint) == 12


def test_an_upstream_failure_is_recorded_as_an_error(
    client, session, user_key_headers, hindsight_error_mock
):
    """The row must not claim the write landed. This is the whole reason the
    INSERT happens at the end instead of at resolution."""
    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "hello"},
        headers=user_key_headers,
    )

    row = session.query(ActivityEvent).one()
    assert (row.outcome, row.error_code) == ("error", "HINDSIGHT_ERROR")


def test_a_rejected_credential_records_no_row(client, session):
    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "x"},
        headers={"Authorization": "Bearer mem_nope"},
    )

    assert session.query(ActivityEvent).count() == 0


def test_the_resolved_slug_is_recorded_not_the_caller_argument(
    client, session, user_key_headers, hindsight_mock, renamed_project
):
    """A rename tombstone must not make one project look like two."""
    client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": renamed_project.old_slug, "query": "x"},
        headers=user_key_headers,
    )

    assert session.query(ActivityEvent).one().project_slug == renamed_project.new_slug


def test_no_bank_id_reaches_the_row(client, session, user_key_headers, hindsight_mock):
    import leakscan

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "hello"},
        headers=user_key_headers,
    )

    row = session.query(ActivityEvent).one()
    assert leakscan.find(row.bank_fingerprint) is None
```

Reuse the fixtures that already exist in `tests/test_memory_api.py` for a user key, a `respx` Hindsight mock, a failing Hindsight, and a renamed project; lift them into `tests/conftest.py` only if they are not already shared.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_activity_api.py -v`
Expected: FAIL — zero rows.

- [ ] **Step 3: Describe the call inside `_resolve_bank`**

In `src/memory/api/memory.py`, add `from memory import activity` to the imports. Then in `_resolve_bank`, replace each `return` with a described one.

The `scope == "user"` branch:

```python
        _describe(
            action, "user", principal, bank_id,
            user_id=body.user_id or principal.user_id, body=body,
        )
        return bank_id, None, None
```

The project branch:

```python
    _describe(action, "project", principal, bank_id, project_slug=project_slug, body=body)
    return bank_id, resolved_from, project_slug
```

And add, directly above `_resolve_bank`:

```python
def _describe(
    action: str,
    scope: str,
    principal: Principal,
    bank_id: str,
    *,
    body: ScopedRequest,
    user_id: str | None = None,
    project_slug: str | None = None,
) -> None:
    """Fill in the activity record for this call (memory/activity.py).

    Here, not at each route, for exactly the reason the rate-limit check
    lives here: every REST handler and every MCP tool already funnels
    through `_resolve_bank`, so one call site covers both surfaces and
    neither can drift.

    `getattr` rather than isinstance: only RetainRequest carries content,
    document_id and metadata, and a type check would have to be repeated for
    every future subclass that adds one.
    """
    content = getattr(body, "content", None)
    metadata = getattr(body, "metadata", None) or {}
    activity.describe(
        action=action,
        scope=scope,
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        user_id=user_id,
        # The RESOLVED slug, never body.project_slug: a caller that followed
        # a rename tombstone would otherwise show up as a second project.
        project_slug=project_slug,
        bank_fingerprint=activity.fingerprint(bank_id),
        document_id=getattr(body, "document_id", None),
        content_bytes=len(content.encode("utf-8")) if content else None,
        agent=metadata.get("agent"),
    )
```

- [ ] **Step 4: Open and close the call in the middleware**

In `src/memory/api/observability.py`, add `from memory import activity, metrics` and change `__call__`:

```python
        activity.new_call()
        status = 500
        ...
        try:
            await self.app(scope, receive, _send)
        finally:
            activity.finish("rest")
            route = scope.get("route")
            ...
```

`new_call()` runs before the downstream app, so the dict exists in the parent context and every child — threadpool included — mutates the same object.

- [ ] **Step 5: Record the error code from the exception handler**

In `src/memory/api/app.py`, in `_domain_error`, after the metrics line added in Task 2:

```python
        activity.set_error(exc.code)
```

and in `_unhandled`:

```python
        activity.set_error("INTERNAL_ERROR")
```

Add `from memory import activity, metrics` to the imports. Both handlers run inside the request's context, so they mutate the same dict the middleware will read.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_activity_api.py tests/test_memory_api.py tests/test_admin_api.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/memory/api/memory.py src/memory/api/observability.py src/memory/api/app.py tests/test_activity_api.py
git commit -m "feat(activity): record REST data-plane calls"
```

---

### Task 7: Wire the recorder into the MCP surface

**Files:**
- Modify: `src/memory/mcp/tools.py`
- Test: `tests/test_activity_api.py`

**Interfaces:**
- Consumes: `activity.new_call/set_error/finish`, `memory.metrics.ERRORS`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_activity_api.py`, using the in-process tool-call helper `tests/test_mcp_tools.py` already uses:

```python
def test_an_mcp_tool_call_records_a_row(session, mcp_call, hindsight_mock):
    mcp_call("retain", {"scope": "user", "content": "hello"})

    row = session.query(ActivityEvent).one()
    assert (row.surface, row.action, row.outcome) == ("mcp", "memory.retain", "ok")


def test_an_mcp_tool_error_is_recorded_with_its_code(session, mcp_call):
    mcp_call("recall", {"scope": "project", "project_slug": "nope", "query": "x"},
             expect_error=True)

    row = session.query(ActivityEvent).one()
    assert row.outcome == "error"
    assert row.error_code
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_activity_api.py -k mcp -v`
Expected: FAIL — zero rows.

- [ ] **Step 3: Wrap `_run`**

In `src/memory/mcp/tools.py`, add `from memory import activity, metrics`. Then bracket the existing `try` in `_run`:

```python
    activity.new_call()
    try:
        with tool_session(ctx) as tc:
            ...
    except DomainError as exc:
        metrics.ERRORS.labels(code=exc.code).inc()
        activity.set_error(exc.code)
        raise MCPToolError(exc.code, exc.message, exc.details) from None
    except ValidationError as exc:
        metrics.ERRORS.labels(code="INVALID_REQUEST").inc()
        activity.set_error("INVALID_REQUEST")
        raise MCPToolError("INVALID_REQUEST", _validation_message(exc)) from None
    except MCPToolError as exc:
        activity.set_error(exc.code)
        raise
    except Exception as exc:
        logger.error("unhandled MCP tool error", exc_info=exc)
        metrics.ERRORS.labels(code="INTERNAL_ERROR").inc()
        activity.set_error("INTERNAL_ERROR")
        raise MCPToolError("INTERNAL_ERROR", "internal error") from None
    finally:
        activity.finish("mcp")
```

`new_call()` and `finish()` run in the same worker thread as the tool body, so no context copy is involved on this surface — but the dict discipline is identical, and it must stay identical: `_resolve_bank` is shared code and cannot know which surface called it.

If `MCPToolError` has no `.code` attribute, use `getattr(exc, "code", "INTERNAL_ERROR")` in that branch rather than adding one.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_activity_api.py tests/test_mcp_tools.py tests/test_mcp_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/memory/mcp/tools.py tests/test_activity_api.py
git commit -m "feat(activity): record MCP tool calls"
```

---

### Task 8: The admin read routes

**Files:**
- Create: `src/memory/api/activity.py`
- Modify: `src/memory/api/app.py`
- Test: `tests/test_activity_api.py`

**Interfaces:**
- Consumes: `require_master`, `MAX_PAGE_SIZE`, `is_unstorable`, `ActivityEvent`.
- Produces: `GET /v1/admin/activity`, `GET /v1/admin/activity/summary`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_activity_api.py`:

```python
def test_activity_requires_the_master_key(client, user_key_headers):
    assert client.get("/v1/admin/activity", headers=user_key_headers).status_code == 403


def test_activity_lists_newest_first(client, master_headers, seeded_activity):
    body = client.get("/v1/admin/activity", headers=master_headers).json()

    assert [r["action"] for r in body] == ["memory.recall", "memory.retain"]
    assert "bank_id" not in body[0]


def test_activity_filters_by_project(client, master_headers, seeded_activity):
    body = client.get(
        "/v1/admin/activity", params={"project_slug": "alpha"}, headers=master_headers
    ).json()

    assert {r["project_slug"] for r in body} == {"alpha"}


def test_activity_rejects_an_oversize_page(client, master_headers):
    assert client.get(
        "/v1/admin/activity", params={"limit": 10**20}, headers=master_headers
    ).status_code == 422


def test_an_unstorable_filter_is_an_empty_result_not_a_500(client, master_headers):
    response = client.get(
        "/v1/admin/activity", params={"action": "a\x00b"}, headers=master_headers
    )

    assert response.status_code == 200
    assert response.json() == []


def test_summary_rolls_up_per_bank(client, master_headers, seeded_activity):
    body = client.get("/v1/admin/activity/summary", headers=master_headers).json()

    row = next(r for r in body if r["project_slug"] == "alpha")
    assert row["retains"] == 1
    assert row["calls"] == 2
    assert len(row["hours"]) == 24
    assert row["last_seen"]
```

Add a `seeded_activity` fixture to `tests/conftest.py` that inserts two `ActivityEvent` rows for project `alpha` — one `memory.retain` with `content_bytes=10`, one later `memory.recall` — plus one row for a second tenant, so tenant isolation is exercised.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_activity_api.py -k "activity_ or summary" -v`
Expected: FAIL — 404 on both routes.

- [ ] **Step 3: Write the router**

Create `src/memory/api/activity.py`:

```python
"""Reading the activity trail. Master key only, tenant-filtered always.

Separate from api/admin.py because that module is the destructive plane
(whole-bank clear and delete) and this one is read-only telemetry. Keeping
them apart keeps the file that can erase a bank small enough to read in one
sitting.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory.api.app import require_master
from memory.api.memory import MAX_PAGE_SIZE
from memory.auth.principal import Principal
from memory.db import get_session
from memory.identifiers import is_unstorable
from memory.models import ActivityEvent

router = APIRouter(prefix="/v1/admin/activity", tags=["admin"])

HOURS = 24


class ActivityResponse(BaseModel):
    id: str
    created_at: str
    credential_id: str | None
    action: str
    surface: str
    scope: str
    user_id: str | None
    project_slug: str | None
    bank_fingerprint: str
    document_id: str | None
    content_bytes: int | None
    agent: str | None
    outcome: str
    error_code: str | None
    duration_ms: int


class FleetRow(BaseModel):
    scope: str
    user_id: str | None
    project_slug: str | None
    agent: str | None
    bank_fingerprint: str
    calls: int
    retains: int
    recalls: int
    errors: int
    bytes_written: int
    last_seen: str
    # Oldest first, one per hour, so the console renders it left to right.
    hours: list[int]


def _response(row: ActivityEvent) -> ActivityResponse:
    """Built field by field, never by serializing the row -- the same rule
    admin._audit_response states: a column added later must not leak through
    this endpoint by default."""
    return ActivityResponse(
        id=row.id,
        created_at=row.created_at.isoformat(),
        credential_id=row.credential_id,
        action=row.action,
        surface=row.surface,
        scope=row.scope,
        user_id=row.user_id,
        project_slug=row.project_slug,
        bank_fingerprint=row.bank_fingerprint,
        document_id=row.document_id,
        content_bytes=row.content_bytes,
        agent=row.agent,
        outcome=row.outcome,
        error_code=row.error_code,
        duration_ms=row.duration_ms,
    )


@router.get("", response_model=list[ActivityResponse])
def list_activity(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
    action: str | None = None,
    user_id: str | None = None,
    project_slug: str | None = None,
    scope: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ActivityResponse]:
    """Filters, not lookups: a value Postgres cannot store matches nothing,
    so an empty result IS the correct answer. Unguarded, psycopg raises
    DataError at parameter adaptation, which no `except` here catches and
    which surfaces as a 500 blaming us for a caller's typo -- the same guard
    admin.list_audit already carries."""
    if any(is_unstorable(v) for v in (action, user_id, project_slug, scope, outcome)):
        return []

    stmt = select(ActivityEvent).where(ActivityEvent.tenant_id == principal.tenant_id)
    for column, value in (
        (ActivityEvent.action, action),
        (ActivityEvent.user_id, user_id),
        (ActivityEvent.project_slug, project_slug),
        (ActivityEvent.scope, scope),
        (ActivityEvent.outcome, outcome),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if since is not None:
        stmt = stmt.where(ActivityEvent.created_at >= since)

    # created_at DESC, id DESC -- not created_at alone. Rows from one burst
    # share a timestamp, so created_at is not a total order and repeated
    # calls would disagree with each other. Same tiebreak, same limits, as
    # admin.list_audit.
    stmt = (
        stmt.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_response(row) for row in db.scalars(stmt).all()]


@router.get("/summary", response_model=list[FleetRow])
def summary(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
    hours: Annotated[int, Query(ge=1, le=168)] = HOURS,
) -> list[FleetRow]:
    """The console's Fleet screen in one request.

    Two queries, not one per row: the totals, and the hourly histogram, both
    grouped the same way and merged here. A per-row query would be N+1 on
    the one screen an operator refreshes constantly.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    group = (
        ActivityEvent.scope,
        ActivityEvent.user_id,
        ActivityEvent.project_slug,
        ActivityEvent.agent,
        ActivityEvent.bank_fingerprint,
    )
    where = (ActivityEvent.tenant_id == principal.tenant_id, ActivityEvent.created_at >= since)

    totals = db.execute(
        select(
            *group,
            func.count().label("calls"),
            func.count().filter(ActivityEvent.action == "memory.retain").label("retains"),
            func.count().filter(ActivityEvent.action == "memory.recall").label("recalls"),
            func.count().filter(ActivityEvent.outcome == "error").label("errors"),
            func.coalesce(func.sum(ActivityEvent.content_bytes), 0).label("bytes_written"),
            func.max(ActivityEvent.created_at).label("last_seen"),
        )
        .where(*where)
        .group_by(*group)
    ).all()

    bucket = func.date_trunc("hour", ActivityEvent.created_at).label("bucket")
    hourly: dict[tuple, dict] = {}
    for row in db.execute(
        select(*group, bucket, func.count().label("calls")).where(*where).group_by(*group, bucket)
    ).all():
        hourly.setdefault(row[:5], {})[row.bucket] = row.calls

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    slots = [now - timedelta(hours=h) for h in range(hours - 1, -1, -1)]

    return [
        FleetRow(
            scope=t.scope,
            user_id=t.user_id,
            project_slug=t.project_slug,
            agent=t.agent,
            bank_fingerprint=t.bank_fingerprint,
            calls=t.calls,
            retains=t.retains,
            recalls=t.recalls,
            errors=t.errors,
            bytes_written=t.bytes_written,
            last_seen=t.last_seen.isoformat(),
            hours=[hourly.get(t[:5], {}).get(slot, 0) for slot in slots],
        )
        for t in totals
    ]
```

- [ ] **Step 4: Register the router**

In `src/memory/api/app.py`'s `create_app()`, alongside the other imports and includes:

```python
    from memory.api import activity as activity_routes
    ...
    app.include_router(activity_routes.router)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_activity_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/memory/api/activity.py src/memory/api/app.py tests/test_activity_api.py tests/conftest.py
git commit -m "feat(activity): read the trail through two master-key routes"
```

---

### Task 9: The console page

**Files:**
- Create: `src/memory/static/dashboard.html`
- Modify: `src/memory/api/app.py`, `Dockerfile` (verify only)
- Test: `tests/test_admin_ui.py`

**Interfaces:**
- Consumes: `GET /v1/admin/activity`, `GET /v1/admin/activity/summary`, `POST /v1/memory/list`.
- Produces: `GET /admin/ui`.

- [ ] **Step 1: Start from the approved mockup**

```bash
cp docs/superpowers/specs/2026-08-26-observability-dashboard-mock.html src/memory/static/dashboard.html
```

Do not redesign it. The palette, the 24-cell ribbon, the tab structure and the copy are the approved design.

- [ ] **Step 2: Write the failing test**

Create `tests/test_admin_ui.py`:

```python
def test_the_console_is_served(client):
    response = client.get("/admin/ui")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ach-memory" in response.text


def test_the_console_loads_no_third_party_code(client):
    """The page holds the master key. A CDN script tag would put that key one
    supply-chain compromise away from an attacker."""
    body = client.get("/admin/ui").text

    assert "http://" not in body
    assert "https://" not in body


def test_the_console_can_be_turned_off(configured_env, monkeypatch):
    from fastapi.testclient import TestClient

    from memory.api.app import create_app
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_ADMIN_UI_ENABLED", "false")
    get_settings.cache_clear()

    assert TestClient(create_app()).get("/admin/ui").status_code == 404
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_admin_ui.py -v`
Expected: FAIL — 404.

- [ ] **Step 4: Serve the file**

In `src/memory/api/app.py`'s `create_app()`, next to the `/metrics` registration:

```python
    if get_settings().admin_ui_enabled:
        from pathlib import Path

        from fastapi.responses import FileResponse

        dashboard = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"

        @app.get("/admin/ui", include_in_schema=False)
        def admin_ui() -> FileResponse:
            # One file, read from the package. No StaticFiles mount: a mount
            # serves a whole directory, and this directory should never gain
            # a second servable file by accident.
            return FileResponse(dashboard, media_type="text/html")
```

- [ ] **Step 5: Replace the mock data with fetches**

In `src/memory/static/dashboard.html`, delete the `FLEET`, `LOG` and `MEMORIES` constants and add the key handling plus three fetches. Keep every render function exactly as it is — they already take the shapes the API returns.

```javascript
/* The master key lives in sessionStorage, never localStorage: it dies with
   the tab. It is the tenant's most powerful credential and this page is the
   only place a human types it. */
const KEY = () => sessionStorage.getItem("ach-memory-key") || "";

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${KEY()}`, "Content-Type": "application/json",
               ...(options.headers || {}) },
  });
  if (response.status === 401 || response.status === 403) {
    sessionStorage.removeItem("ach-memory-key");
    askForKey("That key was refused. Try another.");
    throw new Error("refused");
  }
  if (!response.ok) throw new Error(`${path} answered ${response.status}`);
  return response.json();
}

async function load() {
  const [fleet, log] = await Promise.all([
    api("/v1/admin/activity/summary"),
    api("/v1/admin/activity?limit=100"),
  ]);
  renderFleet(fleet);
  renderLog(log);
}
```

`renderFleet` maps the mockup's row fields onto `FleetRow`: `scope`, `user_id`/`project_slug`, `agent`, `bank_fingerprint`, `retains`, `recalls`, `bytes_written`, `errors`, `last_seen`, `hours`. A row is "silent" when `hours` ends in six or more zeroes. `renderLog` takes `ActivityResponse` objects; a row's kind is `err` when `outcome === "error"`, `write` when the action is `memory.retain`, `memory.sync_retain`, `memory.forget`, `memory.correct` or starts with `admin.`, and `read` otherwise.

Peek posts to the existing route:

```javascript
document.getElementById("peek-go").addEventListener("click", async () => {
  const [kind, id] = document.getElementById("peek-target").value.split(":");
  const body = kind === "p" ? { scope: "project", project_slug: id }
                            : { scope: "user", user_id: id };
  renderMemories(await api("/v1/memory/list", { method: "POST", body: JSON.stringify(body) }));
});
```

Peek's target list is built from the fleet response, so it only ever offers banks that exist.

`askForKey(message)` renders a single input in place of the panels and stores the value on submit. The stat strip reads from the fleet response: writers = rows with a non-zero last hour, quiet = rows silent for six hours or more, and the totals are sums.

- [ ] **Step 6: Run the tests and look at it**

Run: `uv run pytest tests/test_admin_ui.py -v`
Then: `docker compose up -d --build` and open `http://127.0.0.1:8000/admin/ui`, paste the compose master key, and confirm the three tabs render against real rows.
Expected: PASS, and a Fleet screen with at least one row after a `scripts/smoke.sh` run.

- [ ] **Step 7: Confirm it ships**

Run: `uv build && python -c "import zipfile,sys; print([n for n in zipfile.ZipFile(sorted(__import__('glob').glob('dist/*.whl'))[-1]).namelist() if 'static' in n])"`
Expected: `memory/static/dashboard.html` is listed. If it is not, add it to `[tool.hatch.build.targets.wheel.force-include]` the way `plugins/opencode` already is.

- [ ] **Step 8: Commit**

```bash
git add src/memory/static/dashboard.html src/memory/api/app.py tests/test_admin_ui.py
git commit -m "feat(console): serve the operator dashboard at /admin/ui"
```

---

### Task 10: Leak gate, chart, and documentation

**Files:**
- Modify: `tests/test_leakscan.py`, `README.md`, `docs/PROJECT-STATE.md`, `deploy/helm/ach-memory/values.yaml`, `deploy/helm/ach-memory/templates/deployment.yaml`
- Test: `tests/test_leakscan.py`

**Interfaces:**
- Consumes: everything above.
- Produces: no new code interfaces.

- [ ] **Step 1: Write the failing leak test**

Append to `tests/test_leakscan.py`:

```python
def test_no_bank_id_reaches_metrics_or_activity(client, master_headers, seeded_activity):
    """The two new read surfaces, held to the same gate as every other
    response in this service."""
    for path in ("/metrics", "/v1/admin/activity", "/v1/admin/activity/summary"):
        assert leakscan.find(client.get(path, headers=master_headers).text) is None


def test_a_fingerprint_is_not_a_bank_id():
    """sha256(bank_id)[:12] must not itself match the scanner -- if it did,
    every activity response would be a false positive and the gate would be
    turned off in frustration."""
    from memory.activity import fingerprint

    assert leakscan.find(fingerprint(BANK)) is None
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_leakscan.py -v`
Expected: PASS (if it fails, the leak is real — fix the leak, never the scanner)

- [ ] **Step 3: Document the surfaces in the README**

Add a section after the admin API documentation:

```markdown
## Seeing what is happening

`GET /metrics` — Prometheus exposition. Aggregate only: counts by action,
scope, surface, outcome and error code, plus upstream latency. No identities,
no project names, no content, no bank ids. Disable with
`MEMORY_METRICS_ENABLED=false`.

`GET /v1/admin/activity` and `GET /v1/admin/activity/summary` — master key
only. One record per data-plane call: who (credential), what (action), where
(scope, user, project), how much (bytes, duration), and whether it landed.
Content is never stored — `MEMORY_ACTIVITY_RETENTION_DAYS` (default 30) ages
the rows out.

`GET /admin/ui` — a single static page over those two routes, plus a Peek tab
that reads a bank live through `POST /v1/memory/list`. The master key is typed
in and held for the tab only. Disable with `MEMORY_ADMIN_UI_ENABLED=false`.

Both `/metrics` and `/admin/ui` sit behind the same ingress as everything
else. If that ingress is public, so are they.
```

- [ ] **Step 4: Add the chart values**

In `deploy/helm/ach-memory/values.yaml`, next to the other env-var documentation:

```yaml
# Observability. Both routes ride the same ingress as the API -- if
# ingress.enabled is true and the host is public, so are they. /metrics
# carries counts only (no identities, no project names, no content, no bank
# ids); /admin/ui is inert without the master key.
metrics:
  enabled: true
adminUi:
  enabled: true
# Activity rows are telemetry and age out. 0 keeps them forever.
activityRetentionDays: 30
```

and wire the three into `templates/deployment.yaml`'s env block as
`MEMORY_METRICS_ENABLED`, `MEMORY_ADMIN_UI_ENABLED` and
`MEMORY_ACTIVITY_RETENTION_DAYS`, following the pattern of the vars already
there.

- [ ] **Step 5: Update PROJECT-STATE**

Add a row to the status table and a paragraph to "What works today" recording:
the two hooks and why they are the only two; that a `ContextVar` holding a
mutable dict is what crosses the threadpool boundary, and that rebinding it
from inside a route records nothing while every test still passes; that
`/metrics` labels exclude `user_id` and `project_slug` on purpose; and that
the activity table holds no content so `delete_bank` stays a complete
erasure.

- [ ] **Step 6: Run everything**

Run: `uv run pytest -m "not integration"`
Then: `docker compose up -d --build && ./scripts/smoke.sh && uv run python scripts/mcp-smoke.py`
Expected: the full suite passes; both smokes pass; `/v1/admin/activity` shows
rows from both, one `rest` and one `mcp`.

- [ ] **Step 7: Commit**

```bash
git add tests/test_leakscan.py README.md docs/PROJECT-STATE.md deploy/helm/
git commit -m "docs(observability): document the metrics, activity and console surfaces"
```

---

## Self-review notes

- **Spec coverage:** metrics (Tasks 1–3), table (4), recorder (5), both surfaces wired (6–7), read routes (8), console (9), leak gate and docs (10). The spec's four out-of-scope items are absent from every task, as intended.
- **Type consistency:** `activity.describe(**fields)` keys match `ActivityEvent` columns exactly; `FleetRow.hours` is 24 entries oldest-first in both Task 8 and the Task 9 renderer; `fingerprint()` is 12 characters in the model comment, the recorder, and both tests.
- **Known soft spot:** Task 3's and Task 6's tests name fixtures (`client_factory`, `user_key_headers`, `hindsight_mock`, `hindsight_error_mock`, `renamed_project`, `mcp_call`) that may be spelled differently in the existing suite. Reuse whatever those files already use; the assertions are what matter.
