# Observability: metrics, activity trail, and an operator console

**Status:** design approved 2026-08-26, not yet implemented.
**Companion artifact:** `2026-08-26-observability-dashboard-mock.html` in this
directory — the approved visual design, rendered with fake data. It is the seed
for `src/memory/static/dashboard.html`, not a throwaway.

## The problem

An operator running several agents against this service cannot see anything.
There is no `/metrics`, no health route, and no record of ordinary traffic.

That last one is not an oversight, it is the current design: `audit.record()`
fires only for master-key delegation and for mutations of identity objects
(`user.create`, `project.create`, `key.create`, `key.revoke`, `group.*`,
`project.rename`, `slug.release`). `admin.list_audit`'s own docstring explains
why reads are exempt — "a routine `GET /v1/projects` on every agent start
drowns the log that matters". The consequence is that a user key doing
`retain` over MCP all day leaves **no trace of any kind**. A fleet of agents
sharing one credential is, from outside the process, indistinguishable from a
fleet that is doing nothing at all.

So the operator cannot answer: is anything arriving; which agent went quiet;
what was written and where; did the write actually land.

## What this adds

1. `GET /metrics` — Prometheus exposition, aggregate only.
2. `activity_events` — one row per data-plane call, per-identity detail.
3. `GET /v1/admin/activity` and `/v1/admin/activity/summary` — master-key reads
   over that table.
4. `GET /admin/ui` — a single static page, no build step, three tabs.

Two stores on purpose. Prometheus answers "how much and how healthy" over time
and must stay low-cardinality; the table answers "who, where, what" and must
not be a time series. Neither can do the other's job.

## Decisions and their reasons

### The bank id is never in the answer

SPEC invariant 29: `bank_id` must not leave the service. `_strip_bank_id()`
redacts it even as a substring inside an upstream `chunk_id`, and
`tests/test_leakscan.py` fails the build if one appears.

The activity row stores `bank_fingerprint = sha256(bank_id)[:12]` instead.
It is stable, non-reversible, and enough to correlate a row with Hindsight's
own logs. It is not the identity an operator actually thinks in: a bank is 1:1
with a user (`scope=user`) or a project (`scope=project`), so `user_id` and
`project_slug` are the human-readable spelling and the console leads with them.

### Content is never copied

The activity row records `content_bytes`, `document_id` and `agent` — never the
content itself.

A content copy in our database would survive `DELETE /v1/admin/memory/{scope}`,
which SPEC §12.3 defines as the only complete right-to-erasure path. The
erasure would silently stop being complete. It would also duplicate the
disclosure surface the whole service exists to keep narrow.

Forensics is served by reading the bank live: the console's Peek tab calls the
existing `POST /v1/memory/list` with a master key and `user_id`/`project_slug`.
That path already exists, already authorizes, and already writes an
`audit_events` row for the delegated access — so reading someone's memory from
the console leaves a compliance trail, as it should.

### One row is written once, after the outcome is known

Two hooks, not twenty:

- The edge (`ObservabilityMiddleware`, MCP's `_run`) puts a mutable per-call
  dict in a `ContextVar` and starts a monotonic clock; `_resolve_bank()`
  (`src/memory/api/memory.py`) mutates that dict with the identity fields. Every REST route and all fifteen
  MCP tools already funnel through it — its docstring is explicit that this is
  the single choke point, which is what makes both surfaces reachable from one
  edit.
- A finalizer writes one INSERT once the call has finished: from the REST
  middleware's `finally`, and from `_run`'s `finally` in
  `src/memory/mcp/tools.py`.

Insert-at-the-end rather than insert-then-update: one round trip, and a row can
never claim a retain succeeded when Hindsight returned 502. The finalizer opens
its own short-lived session, so it survives a request whose session was rolled
back.

If the record carries no `action` the finalizer writes nothing — a request that
never reached `_resolve_bank` has no bank to report.

The dict is mutated, never rebound. Sync FastAPI routes run in Starlette's
threadpool and MCP tools in AnyIO's worker pool, so both get a *copy* of the
context: a `ContextVar.set()` performed inside a route is invisible to the
middleware afterwards. Passing the dict down and mutating it is the only form
that survives the boundary — and getting this wrong records nothing while every
mock-level test still passes.

### Authentication failures are metrics-only

The ingress is public (decided below), so writing a row per rejected credential
is an unbounded INSERT vector for anonymous traffic. Rejections are counted in
`memory_errors_total{code="UNAUTHENTICATED"}` and surfaced on the console's stat
strip as "rejected keys". A misconfigured agent still diagnoses cleanly: it
shows as a silent fleet row plus a 401 spike.

### Retention: 30 days, pruned opportunistically

`MEMORY_ACTIVITY_RETENTION_DAYS`, default 30. The finalizer runs
`DELETE FROM activity_events WHERE created_at < now() - interval` at most once
per hour per process, guarded by a module-level timestamp. No scheduler, no
cron, no background thread. With N replicas the same idempotent DELETE runs N
times, which costs nothing.

### Exposure: public, by the operator's decision

The Helm ingress publishes `path: /` with `pathType: Prefix`, so `/metrics` and
`/admin/ui` are reachable from the internet once they exist. This was raised and
accepted.

What `/metrics` discloses if scraped by a stranger: call counts by action,
scope, surface and outcome, error codes, and latency. No identities, no project
names, no content, no bank ids. `/admin/ui` is a static page that does nothing
without the master key.

`MEMORY_METRICS_ENABLED` (default `true`) and `MEMORY_ADMIN_UI_ENABLED`
(default `true`) exist so a future deployment can turn either off without a code
change.

## Components

### `src/memory/metrics.py`

Declares the collectors. Depends on
`prometheus-client` (new production dependency: the stdlib has no exposition
format and no histogram bucketing, and hand-rolling both is ~200 lines against
four declarations).

The `Dockerfile` runs a single uvicorn worker with no `--workers`, so
multiprocess mode is not needed. Prometheus scrapes each pod; replicas sum in
PromQL.

| metric | type | labels |
|---|---|---|
| `memory_calls_total` | counter | `action`, `scope`, `surface`, `outcome` |
| `memory_call_duration_seconds` | histogram | `action`, `surface` |
| `memory_content_bytes_total` | counter | `scope` |
| `memory_errors_total` | counter | `code` |
| `memory_hindsight_request_seconds` | histogram | `method`, `status` |
| `memory_http_requests_total` | counter | `route`, `method`, `status` |
| `memory_build_info` | gauge (=1) | `version` |

Every label value comes from a closed set. **No `user_id` or `project_slug`
label, ever** — unbounded label values are how a Prometheus dies. `route` is the
FastAPI route template from `request.scope["route"].path`
(`/v1/memory/{scope}`), never the raw path, so a caller cannot mint labels by
varying the URL.

`memory_http_requests_total` overlaps `memory_calls_total` deliberately: HTTP
sees the 401s and the provisioning routes that never reach `_resolve_bank`,
while over MCP every one of the fifteen tools is the same `POST /mcp` and only
`memory_calls_total` can tell them apart. Each covers the other's blind spot.

`memory_hindsight_request_seconds` is recorded inside
`HindsightClient._request` — the one function every upstream call passes
through. It is labelled by method and status, never by path: a Hindsight path
carries the bank id. Which upstream operation is slow can be added later as an
explicit closed-set label; "is Hindsight slow or erroring" is answered without
it.

### `src/memory/models.py` — `ActivityEvent`

| column | type | notes |
|---|---|---|
| `id` | `String(64)` PK | `act_<uuid4hex>`, via a new `ids.new_activity_id()` |
| `tenant_id` | FK `tenants.id`, indexed | every query filters on it |
| `created_at` | `datetime`, indexed | server default `now()`, the retention key |
| `credential_id` | `String(64)` nullable | NULL = master key, same convention as `AuditEvent.actor_key_id` |
| `action` | `String(64)` indexed | the string `_resolve_bank` already receives |
| `surface` | `String(4)` | `rest` or `mcp` |
| `scope` | `String(8)` | `user` or `project` |
| `user_id` | `String(128)` nullable | |
| `project_slug` | `String(128)` nullable | the resolved slug, never the caller's raw one |
| `bank_fingerprint` | `String(16)` | `sha256(bank_id)[:12]` |
| `document_id` | `String(256)` nullable | writes only |
| `content_bytes` | `Integer` nullable | writes only |
| `agent` | `String(64)` nullable | unverified, from retain metadata |
| `outcome` | `String(8)` | `ok` or `error` |
| `error_code` | `String(32)` nullable | SPEC §18's closed list |
| `duration_ms` | `Integer` | |

`agent` is client-declared and carries no authority — `provenance.py` already
receives `agent` and `client_name` in retain metadata and currently discards
`client_name` entirely. It is a label that helps tell two agents apart when they
share a credential and a project; it is never attribution.

One Alembic migration, following `migrations/versions/`.

### `src/memory/api/activity.py`

- `GET /v1/admin/activity` — master key. Filters: `action`, `user_id`,
  `project_slug`, `scope`, `outcome`, `since`, `limit`/`offset` bounded by the
  existing `MAX_PAGE_SIZE = 500`. Ordered `created_at DESC, id DESC` for the
  same reason `list_audit` is: several events can share a timestamp, and
  without the tiebreak repeated calls disagree with each other.
- `GET /v1/admin/activity/summary?since=` — `GROUP BY scope, user_id,
  project_slug, agent`, returning `calls`, `retains`, `recalls`, `errors`,
  `bytes`, `last_seen`, and 24 hourly buckets. This is the Fleet screen in one
  query.

Responses are built field by field, never by serializing the row — the same
rule `_audit_response` states: a column added later must not leak through by
default.

Values Postgres cannot store are treated as filters that match nothing (the
`is_unstorable` guard `list_audit` already uses), so a control character is an
empty result and not a 500.

### `src/memory/static/dashboard.html`

One file. Vanilla JS, no framework, no build step, no CDN — which is also the
rule that keeps third-party JavaScript off a page that holds the master key.
System mono for data (tabular numerals, so columns align), system sans for
headings.

The master key is typed into a field, held in `sessionStorage` (dies with the
tab, never `localStorage`), and sent as the `Authorization` header.

Three tabs:

- **Fleet** — one row per bank, with a 24-cell hourly ribbon. Silence is a
  visible gap rather than a timestamp to subtract; quiet rows dim and are
  tagged. This is the page's one loud element and everything else stays flat.
- **Activity** — the raw call log, filterable, colour-railed by kind.
- **Peek** — pick a bank, read what is actually stored in it, with the notice
  that content is read live and that the read is audited.

Colour carries meaning and is not decoration: sodium amber = retain (permanent,
costs extraction, no undo), frost = recall (free, leaves nothing), rust =
failed. The read/write asymmetry is the domain's central fact.

Served by FastAPI as a `FileResponse`. It ships inside the wheel (hatchling
includes package data under `src/memory`) and inside the image (the
`Dockerfile` already copies `src/`).

## Testing

Following the house pattern in `tests/`:

- `tests/test_metrics.py` — the scrape parses; counters move on a call; label
  sets are the closed ones; `route` is the template and not a caller-supplied
  path; the endpoint is off when `MEMORY_METRICS_ENABLED=false`.
- `tests/test_activity_api.py` — a row is written for REST and for MCP; the row
  records `error` with the right code when the upstream call fails; filters and
  the pagination ceiling; tenant isolation; a user key gets 403; auth failures
  write no row.
- `tests/test_activity_retention.py` — the prune deletes past the horizon and
  spares rows inside it, on a frozen clock, and runs at most once an hour.
- `tests/test_leakscan.py` — a new assertion that no bank id appears in
  `/metrics` or in any activity response, fingerprint included.

## Explicitly out of scope

- A `/healthz` route. Real gap — the Helm probes currently point at `/docs`
  (`values.yaml:129` says so) — but it is a separate change and does not
  belong to this one.
- A read-only viewer credential (`MEMORY_VIEWER_KEY_HASH`). The right end state
  the day a second person opens the console; additive, no rework. Not built
  until then.
- Grafana dashboards. `/metrics` is standard; whoever wants Grafana can build
  the panels.
- MCP `clientInfo` from the initialize handshake as a stronger `agent` label.
  Worth revisiting if the metadata-supplied `agent` proves too sparse.
