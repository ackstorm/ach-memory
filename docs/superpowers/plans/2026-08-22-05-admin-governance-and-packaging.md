# Admin, Governance and Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close v1. Make the audit trail readable, ship the admin destructive
plane, add the two API-only governance surfaces (directives and mental models),
settle the Memory Defense question with what we measured, and package the thing
so it can be deployed and its container proven to start.

**Architecture:** Everything here is REST-only. Nothing in this plan is an MCP
tool — SPEC §11.6 excludes all of it deliberately, and the frozen-surface test
from Plan 4 is what enforces that. Directives and mental models reuse the same
`_resolve_bank` pipeline as the data plane; the admin plane is master-key-gated
and sits under `/v1/admin`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 sync, httpx,
`mcp` 2.0, pytest, Helm 3, `uv`.

## Global Constraints

- **Nothing in this plan becomes an MCP tool.** SPEC §11.6 lists mental-model
  CRUD, directive CRUD, bank read/configuration, `clear_memories`,
  `delete_bank`, project rename/ownership and project/group/key administration
  as REST-only. Plan 4's `test_the_advertised_tool_surface_is_exactly_the_spec_set`
  must stay green and unchanged; if you find yourself editing it, stop.
- The Hindsight `bank_id` never crosses the API boundary — result, error,
  `details`, or log — and neither does a `prj_` internal id.
- Directives and mental models are **not a new permission model** (SPEC §14):
  the same §7 bank authorization applies. Owner or group member manages them
  for their bank; a master key for any bank in its tenant.
- v1 writes **no retrieval tags** (§13.6). Hindsight's directive and
  mental-model APIs both accept `tags`; do not expose or send them.
- SPEC §14.5: a mental model's `trigger` is **passed through verbatim** when
  supplied and otherwise omitted. The wrapper specifies no refresh default and
  adds no configuration — Hindsight's own defaults already mean no automatic
  refresh at all, which is the cheap and safe behavior.
- SPEC §18's error-code list is closed. Any new code goes into §18 in the same
  commit that introduces it.
- Every destructive admin operation is audited.
- `uv` for dependencies. Never `pip install` outside the venv.

---

## Measured: what Memory Defense actually does in this build

SPEC §25 item 13 has been open since the spec was written. It is now answered,
by probing the running `hindsight-api 0.9.1` on 2026-08-22. **Task 1 exists to
write this down; the rest of the plan assumes it.**

A bank was configured with the full pipeline and the config was accepted and
echoed back verbatim:

```json
"memory_defense": {"enabled": true, "stages": {
  "detect_secrets": {"action": "block"},
  "prompt_injection": {"action": "block"},
  "llm_screen": {"action": "block"}}}
```

Then, against that bank:

| content | result |
|---|---|
| a prompt injection (`Ignore all previous instructions…`) | **200, accepted** |
| an RSA private key block | **200, accepted** — and the **raw key is retrievable through `GET /documents/{id}`** |
| an AWS access key | **blocked** — but by **LiteLLM's** `credential-filter` guardrail, `guardrail_mode: pre_call`, surfacing as `500 "Fact extraction failed"` |

So: **the self-hosted MIT build accepts the Memory Defense configuration and
enforces none of it.** The stages are stored and ignored. What filtering exists
in the path is LiteLLM's, on the extraction call, pattern-based and partial —
and it does not protect stored document text at all, which matters because
`store_document_text` defaults to `true` and `get_document` is an MCP tool an
LLM can call.

Two incidental effects look like protection and are not: the fact extractor
*summarises* rather than storing raw text in a memory, and an injection string
produced no extracted facts. Neither is a control; both are side effects of
extraction that different content would not get.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/memory/api/admin.py` (create) | `/v1/admin/*` — audit read, clear, delete, slug release |
| `src/memory/api/directives.py` (create) | directive CRUD |
| `src/memory/api/mental_models.py` (create) | mental-model CRUD, refresh, clear |
| `src/memory/hindsight/paths.py` (modify) | the directive, mental-model and clear paths |
| `src/memory/hindsight/client.py` (modify) | the matching calls |
| `deploy/helm/ach-memory/` (create) | the chart |
| `.github/workflows/ci.yml` (create) | lint, tests, and a production-image start check |

---

### Task 1: Write down what Memory Defense actually does

**Files:** `SPEC-v1.md`, `src/memory/hindsight/client.py`, `docs/PROJECT-STATE.md`, `README.md`. Test: `tests/test_hindsight_client.py`.

**Interfaces:** No new code interfaces. This task changes claims, one comment, and possibly one config value.

This is first because it is a security-posture decision the rest of the plan
sits on, and because three documents currently describe a control that does not
exist.

- [ ] **Step 1: Amend `SPEC-v1.md` §20.2 and §25 item 13**

§20.2 currently says the tier caveat is unresolved and that v1 "must confirm
this before treating injection screening as a control rather than an
aspiration." It is confirmed. Replace the caveat with the measured result — the
table above, including that the AWS-key block came from LiteLLM and not from
Hindsight — and state the accepted v1 position plainly: **injection screening is
absent, memory poisoning is an accepted risk, and credential filtering is
whatever the LLM gateway happens to provide.** Mark §25 item 13 resolved with
the date and the version probed.

Do not overstate in either direction. LiteLLM's guardrail is real and did fire;
it is just not ours, not complete, and not a property of the memory service.

- [ ] **Step 2: Fix the comment in `ensure_bank`**

`src/memory/hindsight/client.py` says the config PATCH exists "to attach
`memory_defense` — the single field v1 sets". Keep setting it: it costs one
call, it is correct if the tier ever changes, and removing it would have to be
remembered later. But the comment must stop implying it protects anything. Say
that it is accepted-and-ignored by this build, with the date measured, so the
next reader does not re-derive it or trust it.

- [ ] **Step 3: Decide `store_document_text`**

The raw private key was retrievable via `get_document`, which is an MCP tool.
`store_document_text: false` would close that specific path.

This is a real trade-off, not a cleanup: document text is what
`delete_document` deletes and what makes a document worth having, and turning
it off may degrade recall quality. **Measure before deciding** — set it false on
a scratch bank, retain, recall, and see what changes. Then choose, and write
the reasoning into `docs/PROJECT-STATE.md` under design decisions. Either
answer is defensible; an unexamined default is not.

- [ ] **Step 4: Add a test that pins the claim, not the behavior**

You cannot test Hindsight's non-enforcement from our suite. What you *can* pin
is that `ensure_bank` still sends the field, so a future refactor does not drop
it silently while the docs say we set it.

- [ ] **Step 5: Correct `README.md` and `docs/PROJECT-STATE.md`**

Both should say what a user actually gets. `PROJECT-STATE.md`'s open-questions
section carries this as unresolved; move it to the traps or design-decisions
section with the measurement.

- [ ] **Step 6: Commit**

```bash
git commit -am "record what Memory Defense actually enforces in this build"
```

---

### Task 2: `GET /v1/admin/audit`, and close the audit gaps

**Files:** Create `src/memory/api/admin.py`; modify `src/memory/api/app.py`, `src/memory/api/users.py`, `src/memory/api/groups.py`, `src/memory/api/projects.py`. Test: `tests/test_admin_api.py`.

**Interfaces:**
- Consumes: `require_master`, `get_session`, `memory.models.AuditEvent`.
- Produces: `GET /v1/admin/audit`.

**Why this is first among the features.** Plan 3 built the audit trail and Plan 4
multiplied what lands in it, and **nothing can read it**. SPEC §6.1 names the
audit event as *the* mitigation for the accepted consequence that any group
member can transfer a group-owned project to themselves and lock the group out.
Until the table is readable, that mitigation does not exist — it is a promise
backed by `psql`.

- [ ] **Step 1: Write the failing test**

`tests/test_admin_api.py`:

```python
def test_the_audit_read_requires_the_master_key(client, juan, tenant):
    response = client.get("/v1/admin/audit", headers=juan["headers"])

    assert response.status_code == 403


def test_it_returns_events_newest_first(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers)

    events = client.get("/v1/admin/audit", headers=master_headers).json()

    actions = [e["action"] for e in events]
    assert actions[:2] == ["key.create", "user.create"]


def test_it_filters_by_action_and_by_actor(client, master_headers, tenant):
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    client.post("/v1/groups", json={"id": "grp_a"}, headers=master_headers)

    only = client.get(
        "/v1/admin/audit?action=user.create", headers=master_headers
    ).json()

    assert [e["action"] for e in only] == ["user.create"]
    assert only[0]["resource"] == user_id


def test_an_audit_row_never_carries_a_bank_id(client, master_headers, tenant, session):
    """The table is a disclosure surface the moment it is readable."""
    from memory.models import AuditEvent

    client.post("/v1/users", json={}, headers=master_headers)

    body = client.get("/v1/admin/audit", headers=master_headers).text
    assert "bank_id" not in body
    assert "prj_" not in body
    assert not [e for e in session.query(AuditEvent).all() if "user_" in (e.resource or "") and "-" in (e.resource or "")]


def test_it_is_scoped_to_the_callers_tenant(client, master_headers, tenant, session):
    from memory.models import AuditEvent, Tenant

    session.add(Tenant(id="other"))
    session.flush()
    session.add(
        AuditEvent(
            id="aud_other", tenant_id="other", actor_key_id=None,
            on_behalf_of=None, action="user.create", resource="usr_elsewhere",
        )
    )
    session.flush()

    events = client.get("/v1/admin/audit", headers=master_headers).json()

    assert all(e["resource"] != "usr_elsewhere" for e in events)


def test_the_page_size_is_bounded(client, master_headers, tenant):
    response = client.get("/v1/admin/audit?limit=100000", headers=master_headers)

    assert response.status_code == 422
```

- [ ] **Step 2: Run it, watch it 404.**

- [ ] **Step 3: Write the route**

`GET /v1/admin/audit`, `Depends(require_master)`, ordered by `created_at`
descending then `id` descending — **the secondary key matters**: several events
in one request share a timestamp, and `test_it_returns_events_newest_first`
depends on a total order. Filters: `action`, `actor_key_id`, `on_behalf_of`,
`since`, `limit` (bounded, `le=500`), `offset`. Tenant-filtered, always.

Build the response field by field, as `api/projects.py` does — never serialize
the row.

- [ ] **Step 4: Audit the five control-plane reads**

`GET /v1/users/{id}`, `GET /v1/groups`, `GET /v1/groups/{id}`,
`GET /v1/projects`, `GET /v1/projects/{slug}` write no event under a master key.
SPEC §20.3 says every master-key action is auditable.

These are identity-metadata reads with no memory content, so **decide** rather
than reflexively instrumenting: a `GET /v1/projects` on every agent start would
drown the log, which is the same reasoning that exempts a user reading their own
memory. Whatever you choose, say why in your report, and make the log's own
claim match — if some master reads are unaudited, `README.md` must not say
"every master-key action".

- [ ] **Step 5: Run the suite and commit.**

---

### Task 3: The admin destructive plane

**Files:** Modify `src/memory/api/admin.py`, `src/memory/hindsight/paths.py`, `src/memory/hindsight/client.py`. Test: `tests/test_admin_api.py`.

**Interfaces:**
- Produces: `POST /v1/admin/memory/{scope}/clear`, `DELETE /v1/admin/memory/{scope}`, `POST /v1/admin/slugs/{retired_slug}/release`; client `clear_memories`, `delete_bank`.

Pinned Hindsight paths, verified against the live `openapi.json`:

```text
DELETE /v1/{t}/banks/{b}/memories        clear_memories   ?type=world|experience|observation
DELETE /v1/{t}/banks/{b}                 delete_bank
```

**These are the two operations SPEC §11.7 keeps off the MCP surface entirely**,
on the stated grounds that "an LLM that decides memory is 'stale' will use
them". They are master-key REST only, they are irreversible, and every one is
audited. `delete_bank` is also §12.3's right-to-erasure path — deleting a
departing user's bank is the only complete erasure the system has.

Both take the same `{scope}` shape as the data plane, so a caller names
`user` + `user_id` or `project` + `project_slug`; resolve through
`_resolve_bank` with `create=False` so an admin cannot conjure a bank by
clearing one that never existed.

**Slug release** (§8.6) frees a retired slug for reuse. It is deliberately an
explicit admin action because the alternative — a tombstone expiring on its own
— is precisely the "silently create a new empty project" failure §8.6 exists to
prevent. Releasing deletes the `RetiredSlug` row; it must **not** touch the
project the tombstone pointed at.

- [ ] **Step 1: Write the failing tests**

Cover: each of the three requires the master key (a user key that *owns* the
bank must still be refused — that is the whole point of §11.7); `clear` reaches
Hindsight's `DELETE .../memories` and passes `type` through when given; `delete`
reaches `DELETE .../banks/{id}`; deleting a user's bank leaves the `users` row
and its `bank_id` intact or removes both — **decide which, and test it**, because
a dangling `bank_id` pointing at a deleted bank is a trap for the next reader;
releasing a retired slug lets a new project take that slug while the original
project keeps its current one; each of the three writes an audit event.

- [ ] **Step 2-4: Run, implement, run.**

- [ ] **Step 5: Commit.**

---

### Task 4: Directives

**Files:** Create `src/memory/api/directives.py`; modify `paths.py`, `client.py`, `app.py`. Test: `tests/test_directives_api.py`.

**Interfaces:** `POST/GET /v1/directives`, `PATCH/DELETE /v1/directives/{directive_id}`.

Pinned against the live server:

```text
POST   /v1/{t}/banks/{b}/directives        body {name, content, priority?, is_active?}
GET    /v1/{t}/banks/{b}/directives        ?active_only=true&limit=100&offset=0
GET    /v1/{t}/banks/{b}/directives/{id}
PATCH  /v1/{t}/banks/{b}/directives/{id}   body {name?, content?, priority?, is_active?}
DELETE /v1/{t}/banks/{b}/directives/{id}
```

`CreateDirectiveRequest` requires `name` and `content`. It also accepts `tags`,
whose description is *"Directive execution scope. Empty means global"* — **do not
expose or send it.** v1 writes no tags (§13.6), and a tag here is a visibility
dimension inside the bank that nothing else in this service understands.

A directive is a rule injected into future prompts — *"Always use uv for Python
dependency management"* — and for project scope it is **shared by every user and
agent on that project**. That is why §14.1 keeps it off the MCP surface: it
steers other people's agents. Say so in the route docstrings, because the next
person to read them will wonder why an agent cannot manage its own rules.

Same `{scope}` body shape as the data plane, `create=False`, `_resolve_bank`,
authorization inherited. `directive_id` is a secondary resource inside an
already-authorized bank, so §20.1 applies: **an IDOR test per mutating route**,
and the upstream call must not happen when the bank is refused.

- [ ] **Steps:** failing tests → run → implement → run → commit.

---

### Task 5: Mental models

**Files:** Create `src/memory/api/mental_models.py`; modify `paths.py`, `client.py`, `app.py`. Test: `tests/test_mental_models_api.py`.

**Interfaces:** `POST/GET /v1/mental-models`, `GET/PATCH/DELETE /v1/mental-models/{id}`, `POST /v1/mental-models/{id}/refresh`, `POST /v1/mental-models/{id}/clear`.

Pinned:

```text
POST   /v1/{t}/banks/{b}/mental-models          body {name, source_query, id?, max_tokens?, trigger?}
GET    /v1/{t}/banks/{b}/mental-models          ?detail=full&limit=100&offset=0
GET    /v1/{t}/banks/{b}/mental-models/{id}
PATCH  /v1/{t}/banks/{b}/mental-models/{id}     body {name?, source_query?, max_tokens?, trigger?}
DELETE /v1/{t}/banks/{b}/mental-models/{id}
POST   /v1/{t}/banks/{b}/mental-models/{id}/refresh
POST   /v1/{t}/banks/{b}/mental-models/{id}/clear
```

`CreateMentalModelRequest` requires `name` and `source_query`. `tags` again:
**not exposed.** `dry-run-refresh` exists upstream and is excluded by §11.7 —
"it costs exactly the same as a real refresh; the name invites the model to
treat it as free" — so do not wire it on any surface, not even REST.

**`trigger` is passed through verbatim or omitted entirely** (§14.5). Do not
default it, do not validate its shape, do not add configuration around it.
Hindsight's own defaults are `mode=full`, `refresh_after_consolidation=false`,
`refresh_cron=null`, which means a model created without a trigger performs no
automatic refresh at all — the cheapest and safest behavior, and it costs us
nothing to adopt. A caller who sets `refresh_after_consolidation: true` is
making a decision with real spend behind it (each refresh is a full `reflect`,
and §19.4 means that spend is not attributable), and it is their decision to
make. Test that an omitted trigger sends no `trigger` key at all, rather than
sending `{}` or a default.

Same authorization shape as directives; `mental_model_id` is a secondary
resource, so §20.1 applies with per-route IDOR tests.

- [ ] **Steps:** failing tests → run → implement → run → commit.

---

### Task 6: Helm chart

**Files:** Create `deploy/helm/ach-memory/{Chart.yaml,values.yaml,templates/*}`, `deploy/helm/README.md`.

**Interfaces:** Produces a deployable chart.

SPEC §16 places this service beside LiteLLM in Kubernetes, reached with one API
key. The chart ships the service; **Postgres and Hindsight are dependencies you
point it at, not things it runs** — an in-chart database is how test data ends up
in production.

What it must express:

- a `Deployment` running the API, with `MEMORY_DATABASE_URL`,
  `MEMORY_HINDSIGHT_URL`, `MEMORY_HINDSIGHT_API_KEY`, `MEMORY_TENANT_ID`,
  `MEMORY_WRITE_LIMIT`, `MEMORY_WRITE_WINDOW_SECONDS` and
  **`MEMORY_MCP_ALLOWED_HOSTS`**;
- **`MEMORY_MASTER_KEY_HASH` from a `Secret`, never a value in `values.yaml`.**
  It is the credential that reaches every bank in the tenant. The chart should
  make the wrong thing hard: reference an existing secret by name, and fail
  rendering if neither that nor an explicit value is given;
- a migration `Job` or init container running `alembic upgrade head`, ordered
  before the Deployment becomes ready;
- liveness and readiness probes;
- an optional `Ingress`.

**Two things this codebase has already measured that the chart must get right:**

1. `MEMORY_MCP_ALLOWED_HOSTS` must contain the hostname clients will send,
   **including the port when it is non-default** — the MCP SDK's DNS-rebinding
   guard matches the `Host` header including port, and a wrong value means every
   MCP call returns `421 Misdirected Request` while REST works fine. Default it
   from the ingress host if one is configured, and say this in
   `deploy/helm/README.md`.
2. The rate limiter is **in-process**. With `replicaCount: N` the effective
   limit is N times the configured one. Put that next to `replicaCount` in
   `values.yaml`, not only in the prose — someone scaling to 5 replicas should
   see it there.

- [ ] **Step 1: Write the chart.**
- [ ] **Step 2: Verify it renders and is valid**

```bash
helm lint deploy/helm/ach-memory
helm template ach-memory deploy/helm/ach-memory --set masterKeySecret.name=mem-secret
```

Both must succeed. Also render with `--set replicaCount=3` and confirm nothing
silently breaks. If `helm` is unavailable, say so in your report rather than
skipping the check — an unrendered chart is not a deliverable.

- [ ] **Step 3: Commit.**

---

### Task 7: CI, and prove the shipped image starts

**Files:** Create `.github/workflows/ci.yml`; modify `README.md`, `docs/PROJECT-STATE.md`.

**Interfaces:** Produces a CI workflow.

**Why this task exists, specifically.** In Plan 4, `mcp` was declared a dev
dependency while production code imported it. The Dockerfile builds with
`uv export --no-dev`, so **the shipped container crash-looped for seven tasks**
with `ModuleNotFoundError`, and every one of those tasks was green — because
`uv run pytest` installs the dev group regardless. Nothing automated caught it;
a human rebuilding the image and reading the logs did.

So the workflow's most valuable job is not the unit tests. It is: **build the
production image and prove it starts.**

- [ ] **Step 1: Write the workflow**

Jobs: `lint` (`ruff check`), `test` (Postgres service container,
`uv run pytest -m "not integration"`), and `image` — `docker build`, run the
container against a Postgres service with a dummy master-key hash, and assert
the health endpoint answers. The last one is the regression guard for the class
of defect above, and it must fail if a production import is missing.

Give every wait a bound and an explicit failure path. No naked polling loops:
when the target never appears, `until … sleep` hangs forever with no signal.

- [ ] **Step 2: Verify the image job locally before trusting it**

```bash
docker build -t ach-memory:citest .
docker run --rm ach-memory:citest python -c "import memory.api.app, memory.mcp.tools"
```

The second command is the whole guard in one line: it imports the production
entry points inside the shipped image, with no dev group. Confirm it passes now
and that it **fails** if you move a production dependency into the dev group —
mutate `pyproject.toml`, rebuild, watch it fail, restore.

- [ ] **Step 3: Document and commit.**

---

## Done when

- `uv run pytest -m "not integration"` is green with no warnings.
- `./scripts/smoke.sh` and `uv run python scripts/mcp-smoke.py` both pass, twice
  in a row.
- `GET /v1/admin/audit` returns the trail, master-key only, tenant-scoped, with
  no bank id in it.
- The three destructive admin operations work, are master-key only, and are
  audited.
- Directives and mental models work for an owner, a group member and a master
  key, and are refused for everyone else, with per-route IDOR tests.
- The MCP tool surface is **still exactly fifteen** — Plan 4's pinning test
  unchanged and green.
- `helm lint` and `helm template` both succeed.
- The production image starts, and the check that proves it fails when a
  production dependency is moved to the dev group.
- SPEC §25 item 13 is marked resolved with the measurement behind it.

## Deliberately not in this plan

Retrieval tags (§13.6) · `dry-run-refresh`, `list_banks`, `create_bank`,
`get_bank_stats`, `retry_operation`, `delete_operation` (§11.7) · a distributed
rate limiter · multi-tenancy beyond the configured tenant · `MEMORY_PROJECT` /
Git-locator derivation, which is the MCP client's job (§10) · an in-chart
Postgres or Hindsight · OAuth on the MCP transport.
