# Memory Quality Phase 2 Working State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit, workspace-isolated Working State that survives session boundaries, rejects stale checkpoints, and appears with age and source in both delivered tiers without entering durable Hindsight memory.

**Architecture:** Store one replace-only Working State row per authorized `(user, project, workspace)` and keep session-to-epoch rows as ordering metadata only. A server-issued identity value supplies a monotonic `session_epoch`; writes are accepted only when their `(session_epoch, checkpoint_seq)` pair is newer, with exact retries idempotent. Clients derive an opaque workspace id from the canonical git worktree root and pass it through brief fetches, cache keys and the two explicit handoff surfaces. `/v1/session-brief` remains the sole compiler and folds the current row into the existing Working State seam and revision fingerprint.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy/Alembic, PostgreSQL, FastMCP, Bash, pytest/respx.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (§4.1 Working State, §10, §11.1–§11.5, §14, §16.2); sequencing and phase gate: `docs/plans/2026-08-29-memory-quality-program.md` Phase 2.

## Review prerequisite from Phase 1

The Phase 1 implementation passed its targeted delivery suite (`188 passed`) and Ruff. The repository suite reached `1086 passed, 2 skipped`; its remaining integration failure/error require a live Hindsight service and `MEMORY_MASTER_KEY`, respectively. Two bounded delivery defects remain and Task 0 closes them before Working State is introduced:

1. `compose_full()` stops allocating later sections when an earlier section's first line does not fit. An oversized user line can therefore suppress a fitting project or Working State section.
2. A legacy Index cache obtains an age from file mtime but has no protocol-2 cache-age slot, so `stamp_cache_age()` returns it unchanged and the delivered cache age is invisible.

## Global Constraints

- Working State is PostgreSQL state, never a Hindsight fact, observation, document or mental-model input.
- There is exactly one state payload per `(tenant, user, project, workspace)`. Replacements keep no payload history and have no TTL or decay.
- Session rows contain only identity/ordering metadata. Reusing the same `session_id` for the same tuple returns the same epoch; it never allocates a newer one.
- The server verifies the submitted `session_id` and `session_epoch` pair. A caller cannot win by inventing a large epoch.
- Ordering is lexicographic on `(session_epoch, checkpoint_seq)`. Lower pairs and same-pair/different-payload calls return a typed conflict; same-pair/same-payload calls are idempotent.
- Phase 2 exposes `set_working_state` only as an explicit human-handoff operation. No hook, proxy startup or background job writes task state automatically.
- `workspace_id` is `ws_` plus the first 32 hexadecimal characters of SHA-256 over the canonical absolute git worktree root. Branch names and git remotes do not participate. If no worktree root is available, Working State is omitted rather than guessed.
- The raw local path never crosses the network or enters logs/cache filenames; only the opaque workspace id does.
- Project resolution uses `create=False` and the existing authorization boundary. Working State cannot create a project or be read across users, tenants or unauthorized projects. Master credentials are not accepted on the agent write surfaces.
- Project linkage in Working State uses `Project.internal_id`, so a project rename does not orphan current state. Public responses continue to name `project_slug`.
- Working State age is derived from server `updated_at` at render time. Age is visible but is not itself a revision input; the stored payload, ordering tuple, source session and update timestamp are.
- Index remains at or below its host character budget. Full remains at or below `FULL_MAX_TOKENS`; neither tier slices a semantic line.
- Index and Full cache keys include workspace id as well as service/project context and remain credential-owner isolated.
- Automatic transcript capture, transcript slicing, shared-workspace merge semantics and production Hindsight mutation remain out of scope.

## Public Contracts

```http
POST /v1/working-state/sessions
PUT  /v1/working-state
GET  /v1/session-brief?workspace_id=ws_<32 hex>
```

```python
start_working_session(
    project_slug: str,
    workspace_id: str,
    session_id: str,
    git_locator: str | None = None,
) -> {session_epoch, session_id, workspace_id, project_slug}

set_working_state(
    project_slug: str,
    workspace_id: str,
    session_id: str,
    session_epoch: int,
    checkpoint_seq: int,
    objective: str,
    current_direction: str | None = None,
    recent_decisions: list[str] = [],
    open_questions: list[str] = [],
    next_steps: list[str] = [],
    git_locator: str | None = None,
) -> {updated_at, session_id, session_epoch, checkpoint_seq, ...}
```

The REST and MCP schemas share the same Pydantic request models. Text fields reject blank/control-character-only values; objective is required, lists are bounded to 10 items, each item is bounded to 512 characters, and `checkpoint_seq >= 0`.

## File Map

- `src/memory/models.py`: `WorkingSession`, `WorkingState`, and workspace-scoped `ContextRevision` mapping.
- `migrations/versions/c3d4e5f6a7b8_working_state.py`: additive Working State tables and context-revision key migration from Alembic head `b2c3d4e5f6a7`.
- `src/memory/working_state.py`: session allocation, stale-write protection, current-row lookup, fingerprint and tier renderers.
- `src/memory/api/working_state.py`: validated session-start and explicit-handoff REST contracts.
- `src/memory/api/app.py`: router registration.
- `src/memory/api/brief.py`: workspace-aware state lookup, fingerprint and compiler input.
- `src/memory/revisions.py`: workspace-scoped revision lookup/creation.
- `src/memory/brief.py`: Phase 1 allocation fix, section survival reporting and Working State delivery.
- `src/memory/mcp/tools.py`: `start_working_session` and `set_working_state` tools.
- `src/memory/mcp/proxy.py`: workspace derivation/argument injection, workspace-aware brief fetch and Index cache isolation.
- `src/memory/cli.py`: pass the resolved workspace to Index and manual brief fetches.
- `plugins/claude-code/scripts/session-start.sh`: derive/pass workspace and isolate the Full cache by worktree.
- `tests/test_working_state.py`: repository ordering, idempotency, isolation and rename behavior.
- `tests/test_working_state_api.py`: REST validation, authorization and no-create behavior.
- `tests/test_brief.py`: rendered age/source, revision and delivery budgets.
- `tests/test_mcp_tools.py`, `tests/test_mcp_surface_honesty.py`: MCP contract, annotations and security tables.
- `tests/test_mcp_proxy.py`, `tests/test_cli.py`, `tests/test_agent_bundle.py`: local workspace propagation and cache isolation.
- `tests/test_app.py`, `tests/test_models.py`: frozen route/schema contract.

---

### Task 0: Close the two Phase 1 review findings

**Files:**

- Modify: `src/memory/brief.py:515-526`
- Modify: `src/memory/mcp/proxy.py:199-315`
- Test: `tests/test_brief.py`
- Test: `tests/test_mcp_proxy.py`

**Interfaces:**

- Preserve `compose_full(...)`, `startup_instructions(...)`, protocol 2 and both budgets.
- Add a private proxy helper that exposes age on both protocol-2 and legacy cached Index payloads.

- [ ] **Step 1: Pin both regressions with failing tests**

Add a Full test whose first user line exceeds the remaining budget while a short project line and a short Working State line fit. Assert the oversized line is absent, both later sections survive whole, and `token_upper_bound(text) <= FULL_MAX_TOKENS`.

Add a proxy test using a valid legacy owner/instructions cache, a fixed mtime and a failed refresh. Assert the delivered payload contains its original `brief rev`, contains an explicit cached-Index age, and stays at or below `brief.SMALLEST_BUDGET`.

- [ ] **Step 2: Verify the tests fail for the reviewed reasons**

Run:

```bash
uv run pytest \
  tests/test_brief.py -k 'full and oversized' \
  tests/test_mcp_proxy.py -k 'legacy and age' -v
```

Expected: the project/Working State assertions fail because `compose_full()` breaks at the oversized user floor, and the legacy payload has no age marker.

- [ ] **Step 3: Make the two surgical corrections**

In `compose_full()`, skip an unaffordable section floor with `continue`; do not terminate allocation of later sections. Preserve authority order and round-robin filling for sections that received a floor.

In the proxy, route cached delivery through one helper:

- protocol-2 payload: replace the reserved digits with `brief.stamp_cache_age()`;
- legacy payload: add one compact cached-Index age line using the mtime-derived age, then remove only complete lowest-priority trailing content lines until the full payload is within `SMALLEST_BUDGET`;
- never slice a line, remove the revision header, or return a cache entry without visible age.

- [ ] **Step 4: Run the Phase 1 gate**

Run:

```bash
uv run pytest tests/test_brief.py tests/test_mcp_proxy.py tests/test_agent_bundle.py tests/test_cli.py -q
uv run ruff check src tests
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/memory/brief.py src/memory/mcp/proxy.py tests/test_brief.py tests/test_mcp_proxy.py
git commit -m "fix(brief): preserve later sections and legacy cache age"
```

---

### Task 1: Add the workspace-scoped persistence model

**Files:**

- Modify: `src/memory/models.py:231-257`
- Create: `migrations/versions/c3d4e5f6a7b8_working_state.py`
- Modify: `tests/test_models.py`
- Create: `tests/test_working_state.py`

**Schema:**

```text
working_sessions
  session_epoch BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY
  tenant_id, user_id, project_internal_id, workspace_id, session_id
  started_at
  UNIQUE (tenant_id, user_id, project_internal_id, workspace_id, session_id)

working_states
  tenant_id, user_id, project_internal_id, workspace_id  PRIMARY KEY
  objective, current_direction
  recent_decisions JSON, open_questions JSON, next_steps JSON
  updated_at, session_id, session_epoch, checkpoint_seq

context_revisions
  PRIMARY KEY (tenant_id, user_id, project_slug, workspace_id)
```

- [ ] **Step 1: Write failing model tests**

Pin all of the following in `tests/test_models.py` and `tests/test_working_state.py`:

- two workspaces for one user/project can each hold one state;
- a duplicate `(user, project, workspace, session_id)` cannot allocate a second session row;
- session epochs increase when two distinct session ids are inserted;
- the revision table accepts separate rows for `workspace_id=""`, `ws_a...` and `ws_b...`;
- JSON list fields round-trip as lists and `updated_at` is timezone-aware.

- [ ] **Step 2: Verify the model tests fail**

Run:

```bash
uv run pytest tests/test_models.py tests/test_working_state.py -v
```

Expected: FAIL because the models/tables do not exist and `ContextRevision` has no workspace key.

- [ ] **Step 3: Add ORM models and migration**

Use `Project.internal_id` and `User.id` foreign keys. Use `String(35)` for `workspace_id`, `String(128)` for `session_id`, `Text` for prose, SQLAlchemy `JSON` for lists, `BigInteger` for epoch/sequence and database time for `started_at`/`updated_at`. Add non-negative check constraints for epoch/sequence where applicable.

Migrate existing `context_revisions` rows by adding non-null `workspace_id` with temporary server default `""`, replacing the primary key, then removing the server default. Existing no-workspace snapshots retain their revision sequence.

The downgrade drops Working State tables, restores the original context-revision primary key and removes `workspace_id`. This loses only Phase 2 ephemeral state, never Hindsight data.

- [ ] **Step 4: Exercise migration and model tests**

Run:

```bash
uv run alembic upgrade head
uv run alembic downgrade b2c3d4e5f6a7
uv run alembic upgrade head
uv run pytest tests/test_models.py tests/test_working_state.py -q
```

Expected: every migration command and test passes; `uv run alembic heads` reports only `c3d4e5f6a7b8`.

- [ ] **Step 5: Commit**

```bash
git add src/memory/models.py migrations/versions/c3d4e5f6a7b8_working_state.py tests/test_models.py tests/test_working_state.py
git commit -m "feat(working-state): add workspace-scoped storage"
```

---

### Task 2: Implement session allocation and lexicographic replacement

**Files:**

- Create: `src/memory/working_state.py`
- Modify: `src/memory/errors.py`
- Modify: `tests/test_working_state.py`

**Interfaces:**

```python
def start_session(
    db: Session, principal: Principal, project_slug: str,
    workspace_id: str, session_id: str, git_locator: str | None = None,
) -> WorkingSession

def replace(
    db: Session, principal: Principal, request: WorkingStateWrite,
) -> tuple[WorkingState, bool]

def get_current(
    db: Session, principal: Principal, project_internal_id: str,
    workspace_id: str,
) -> WorkingState | None

def state_fingerprint(state: WorkingState | None) -> str | None
```

- [ ] **Step 1: Write failing repository tests**

Cover:

- same session start is idempotent and returns the original epoch;
- a second session gets a higher epoch;
- checkpoint `2` replaces checkpoint `1` within one session;
- checkpoint `1` cannot replace checkpoint `2`;
- an older session cannot overwrite a newer session even with a larger checkpoint;
- an identical retry at the same pair succeeds without changing `updated_at`;
- different content at the same pair returns `WORKING_STATE_CONFLICT`;
- user, tenant, project and workspace isolation;
- project rename preserves state because the row points to `internal_id`;
- starting or setting state for an absent/unauthorized project creates no project.

Add a two-connection race test for first writes from two sessions. The surviving row must carry the lexicographically greater pair regardless of arrival order.

- [ ] **Step 2: Verify the repository tests fail**

Run:

```bash
uv run pytest tests/test_working_state.py -v
```

Expected: FAIL because the repository and typed conflicts do not exist.

- [ ] **Step 3: Implement the domain boundary**

Resolve projects with `projects.resolve(..., create=False)` and reuse `projects.authorize`; never resolve a bank. `start_session()` first reads the unique session tuple, then inserts through a savepoint so concurrent repeats converge on one identity-generated epoch.

`replace()` must:

1. verify that `(session_id, session_epoch)` belongs to the same authorized tuple;
2. lock the current Working State row with `SELECT ... FOR UPDATE`;
3. compare ordering pairs lexicographically;
4. reject lower pairs;
5. return the row unchanged for an exact same-pair/same-payload retry;
6. reject same-pair/different-payload input;
7. replace every payload field and `updated_at` for a greater pair.

Handle the no-row insertion race with a nested transaction and re-read/recompare after `IntegrityError`; do not use last-commit-wins logic.

`state_fingerprint()` serializes all stored payload fields, source identifiers, ordering fields and `updated_at` with sorted JSON keys. Rendering age is intentionally excluded.

- [ ] **Step 4: Run repository tests**

Run:

```bash
uv run pytest tests/test_working_state.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/memory/working_state.py src/memory/errors.py tests/test_working_state.py
git commit -m "feat(working-state): enforce monotonic handoffs"
```

---

### Task 3: Add validated REST session and handoff endpoints

**Files:**

- Create: `src/memory/api/working_state.py`
- Modify: `src/memory/api/app.py:160-182`
- Modify: `tests/test_app.py:18-74`
- Create: `tests/test_working_state_api.py`

**Interfaces:**

- `POST /v1/working-state/sessions` calls `start_session()` and commits.
- `PUT /v1/working-state` calls `replace()` and commits.
- Both return resolved `project_slug`, opaque workspace id and server state; neither returns internal ids.

- [ ] **Step 1: Write failing API tests**

Pin the exact two new routes in `EXPECTED_ROUTES`, then test:

- a user starts a session and receives a positive server epoch;
- retrying the request returns the same epoch;
- explicit state write returns its stored ordering/source fields;
- lower and conflicting pairs return HTTP 409 with stable domain codes;
- malformed workspace ids, blank objective/list items, oversized lists/items and negative sequences return 422;
- master credentials, another user, another tenant and unauthorized group membership cannot write;
- an unknown project returns not-found and the projects row count does not change;
- `extra="forbid"` rejects misspelled fields.

- [ ] **Step 2: Verify the API tests fail**

Run:

```bash
uv run pytest tests/test_working_state_api.py tests/test_app.py::test_the_route_set_is_exactly_the_documented_surface -v
```

Expected: FAIL because the router and models do not exist.

- [ ] **Step 3: Implement and register the router**

Define shared Pydantic models in this module so MCP reuses the exact same bounds. Reject master principals explicitly; agent Working State has no `On-Behalf-Of` path. Keep commits at the API/tool boundary, not inside domain functions.

Return 200 for session starts, including first creation, because the same request is deliberately idempotent. Map stale/equal-conflict domain errors to 409 through the existing error handler.

- [ ] **Step 4: Run API tests**

Run:

```bash
uv run pytest tests/test_working_state_api.py tests/test_app.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/memory/api/working_state.py src/memory/api/app.py tests/test_working_state_api.py tests/test_app.py
git commit -m "feat(api): expose explicit working-state handoff"
```

---

### Task 4: Derive and propagate workspace identity locally

**Files:**

- Modify: `src/memory/mcp/proxy.py:42-323`
- Modify: `src/memory/cli.py:640-690`
- Modify: `plugins/claude-code/scripts/session-start.sh`
- Modify: `tests/test_mcp_proxy.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_agent_bundle.py`

**Interfaces:**

```python
def resolve_workspace_context(cwd: str | None = None) -> str | None

def fetch_brief(..., workspace_id: str | None = None, ...) -> dict | None

def startup_instructions(..., workspace_id: str | None = None, ...) -> str
```

- [ ] **Step 1: Write failing client tests**

Create a repository plus two git worktrees and assert:

- repeated resolution of one worktree is stable;
- the two worktree roots produce different ids even when checked out at the same commit;
- a branch change in one worktree does not change its id;
- a symlinked cwd resolves to the same id as the real root;
- a non-worktree returns `None`;
- brief requests include `workspace_id` only when resolved;
- Index cache paths differ by workspace and still never include credentials/raw paths;
- middleware fills missing workspace/project arguments only for `start_working_session` and `set_working_state`, preserving explicit caller values;
- CLI Index/manual brief calls pass the resolved workspace;
- Claude Full fetch includes the same hash and its cache files differ across worktrees.

- [ ] **Step 2: Verify the propagation tests fail**

Run:

```bash
uv run pytest tests/test_mcp_proxy.py tests/test_cli.py tests/test_agent_bundle.py -k 'workspace or worktree' -v
```

Expected: FAIL because workspace resolution/propagation is absent.

- [ ] **Step 3: Implement one canonical algorithm on both clients**

Python: run `git rev-parse --show-toplevel`, resolve the returned root with `Path.resolve()`, SHA-256 its UTF-8 absolute string and return `ws_<first 32 hex>`. Fail open on missing git, timeout, invalid output or filesystem errors.

Shell: use `git rev-parse --show-toplevel`, canonicalize with `cd "$root" && pwd -P`, then the existing `sha256sum || shasum -a 256` portability pattern. Send only the opaque id via `--data-urlencode workspace_id=...`.

Extend cache digests with workspace id. Existing no-workspace cache files remain readable only on the no-workspace path; never reuse them for a resolved workspace because that can replay another worktree's state.

Change `ProjectContextMiddleware` to hold project and workspace context, inspect the tool name, and inject project/locator/workspace only into the two new project-only tools. Existing `scope="project"` auto-fill behavior remains unchanged.

- [ ] **Step 4: Run client tests**

Run:

```bash
uv run pytest tests/test_mcp_proxy.py tests/test_cli.py tests/test_agent_bundle.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/memory/mcp/proxy.py src/memory/cli.py plugins/claude-code/scripts/session-start.sh tests/test_mcp_proxy.py tests/test_cli.py tests/test_agent_bundle.py
git commit -m "feat(client): isolate working state by worktree"
```

---

### Task 5: Expose the explicit handoff over MCP

**Files:**

- Modify: `src/memory/mcp/tools.py:292-772`
- Modify: `tests/test_mcp_tools.py`
- Modify: `tests/test_mcp_surface_honesty.py`

**Tool annotations:**

| Tool | readOnlyHint | idempotentHint | destructiveHint | create project | rate-limited write |
|---|---:|---:|---:|---:|---:|
| `start_working_session` | false | true | false | false | true |
| `set_working_state` | false | true | true | false | true |

- [ ] **Step 1: Write failing MCP surface tests**

Extend the frozen registry, read-only, create and write tables. Assert the generated schemas preserve the REST bounds and that tool descriptions state:

- `start_working_session` allocates ordering metadata for an explicit handoff;
- `set_working_state` replaces ephemeral context, creates no durable memory/history and must only follow an explicit human handoff;
- agents must not call it proactively from inferred task progress.

Test authorization, missing project, stale pair, idempotent retry and middleware-filled project/workspace context through `REGISTRY`.

- [ ] **Step 2: Verify the MCP tests fail**

Run:

```bash
uv run pytest tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py -k 'working_state or security_tables or readonly_table' -v
```

Expected: FAIL because the registry has no Working State tools.

- [ ] **Step 3: Register both tools through the shared security pipeline**

Reuse the REST request models. Authenticate with `tool_session()`, resolve with `create=False`, call the domain functions, commit, and translate domain/Pydantic errors through the existing `MCPToolError` conventions. Mark both as writes for rate limiting and neither as project-creating.

Do not route either tool through Hindsight `_run()`; they have no bank and must not acquire one as an accidental side effect.

- [ ] **Step 4: Run MCP tests**

Run:

```bash
uv run pytest tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py -q
```

Expected: PASS with all 17 registered tools covered by both frozen security tables.

- [ ] **Step 5: Commit**

```bash
git add src/memory/mcp/tools.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py
git commit -m "feat(mcp): add explicit working-state handoff tools"
```

---

### Task 6: Compile Working State into both workspace snapshots

**Files:**

- Modify: `src/memory/working_state.py`
- Modify: `src/memory/revisions.py:18-93`
- Modify: `src/memory/api/brief.py:23-174`
- Modify: `src/memory/brief.py:105-125, 267-383, 417-567`
- Modify: `tests/test_working_state.py`
- Modify: `tests/test_brief.py`

**Rendered contract:**

```text
Index headline (one semantic line):
objective: <...>; next: <...>; age: <duration>

Full section:
objective: <...>
current direction: <...>
recent decision: <...>
open question: <...>
next step: <...>
age: <duration>
source session: <session_id> (epoch <n>, checkpoint <n>)
```

- [ ] **Step 1: Write failing compiler/API tests**

Cover:

- no `workspace_id` means no Working State and uses the existing empty-string revision namespace;
- a matching workspace gets only its own row;
- Index contains one Working State headline with objective, bounded next-step text and age;
- Full contains all bounded fields, age and source session/order;
- old age remains visible without expiry/decay classification, and Full always says to verify Working State against repository state before acting;
- another workspace receives neither state nor its revision;
- a state update increments `brief_revision` for that workspace only;
- rendering age changing with time does not increment revision;
- Index and Full from the same state carry the same revision;
- `sections["working_state"]` describes delivered content accurately;
- both delivery budgets still hold when every Working State field is at its input maximum.

- [ ] **Step 2: Verify the delivery tests fail**

Run:

```bash
uv run pytest tests/test_brief.py tests/test_working_state.py -k 'working_state or workspace_revision' -v
```

Expected: FAIL because the endpoint passes `None` into the compiler and revisions are not workspace-scoped.

- [ ] **Step 3: Make revisions workspace-scoped**

Extend `_locked()` and `current()` with `workspace_id`, and persist it in new rows. Keep `""` for no-workspace snapshots. Update concurrency tests so the existing insert-race behavior is preserved independently per workspace.

In `/v1/session-brief`, validate optional `workspace_id`, resolve the authorized project first, and read Working State only when both project and workspace are present. Include `state_fingerprint()` in `revisions.fingerprint()` and pass the workspace id into `revisions.current()`.

- [ ] **Step 4: Render safe Index and Full sections**

Create separate pure renderers in `working_state.py`; do not reuse the multiline Full body as the Index input because `INDEX_CAPS["working_state"] == 1`. Sanitize every stored item through the compiler's inert/heading protections before delivery.

Pass the headline `Section` to `compose_index()` and the full `Section` to `compose_full()`. Extend `survived()` and `BriefResponse.sections` with `working_state`. Add `workspace_id` to `BriefResponse` so a JSON consumer knows which revision sequence it received.

Age is computed from `updated_at` for each response and is always present, including old state. Full also names the source session and always states that Working State is context, not an agenda, and must be checked against the repository. There is no invented staleness threshold.

- [ ] **Step 5: Run the compiler and revision suite**

Run:

```bash
uv run pytest tests/test_brief.py tests/test_working_state.py tests/test_working_state_api.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/memory/working_state.py src/memory/revisions.py src/memory/api/brief.py src/memory/brief.py tests/test_working_state.py tests/test_brief.py
git commit -m "feat(brief): deliver workspace working state"
```

---

### Task 7: Prove the Phase 2 delivered-payload gate

**Files:**

- Modify as required by gate failures only: files already named above
- Test: `tests/test_brief.py`
- Test: `tests/test_working_state.py`
- Test: `tests/test_working_state_api.py`
- Test: `tests/test_mcp_tools.py`
- Test: `tests/test_mcp_surface_honesty.py`
- Test: `tests/test_mcp_proxy.py`
- Test: `tests/test_agent_bundle.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Add one end-to-end acceptance test**

The test must use the host-facing paths, not call render helpers directly:

1. create an authorized project;
2. derive/select workspace A and start session A;
3. write checkpoint 2 as an explicit handoff;
4. prove checkpoint 1 and an older session cannot overwrite it;
5. fetch delivered Index and Full for workspace A and assert objective, next step, age, source, matching revision and budgets;
6. fetch workspace B and assert A's state is absent;
7. make the live Full fetch fail and prove A's cache is used with cache age while B cannot read it.

- [ ] **Step 2: Run the Phase 2 targeted gate**

Run:

```bash
uv run pytest \
  tests/test_working_state.py \
  tests/test_working_state_api.py \
  tests/test_brief.py \
  tests/test_mcp_tools.py \
  tests/test_mcp_surface_honesty.py \
  tests/test_mcp_proxy.py \
  tests/test_agent_bundle.py \
  tests/test_cli.py -q
uv run ruff check src tests
```

Expected: PASS.

- [ ] **Step 3: Run the repository suite and classify environmental integration failures**

Run:

```bash
uv run pytest -q
```

Expected: all hermetic tests pass. If the two existing live-integration cases remain unavailable, report them explicitly as environment requirements (`MEMORY_MASTER_KEY` and a reachable Hindsight service); do not classify them as Phase 2 regressions and do not silently waive any new failure.

- [ ] **Step 4: Verify the migration and diff**

Run:

```bash
uv run alembic heads
git diff --check
git status --short
```

Expected: one Alembic head (`c3d4e5f6a7b8`), no whitespace errors, and only intentional changes.

- [ ] **Step 5: Commit any gate-only test adjustments**

```bash
git add tests src plugins migrations
git commit -m "test(working-state): prove the phase 2 delivery gate"
```

## Rollout and rollback

1. Deploy the additive migration and server code before updated clients. Old clients omit `workspace_id` and continue receiving no Working State in the existing `workspace_id=""` revision namespace.
2. Deploy proxy/CLI/plugin propagation next. New clients can read state only after an explicit session start and handoff; no automatic writer is enabled.
3. Observe 409 stale/conflict counts, brief budget tests and cache fallback behavior before declaring the gate closed.
4. Application rollback is safe while the additive tables/column remain. Schema downgrade removes only ephemeral Working State/session metadata and workspace revision partitions; it does not mutate Hindsight or project/profile data.
5. Do not begin Phase 3 automatic capture until the Phase 2 gate passes on delivered payloads.

## Definition of done

- An explicit human handoff is delivered in Index and Full with age and source session.
- Lower ordering pairs cannot overwrite higher pairs, including concurrent first writes.
- Two git worktrees of the same project remain isolated in storage, revisions and both caches.
- Working State changes bump only the matching workspace's `brief_revision`.
- No Working State path creates a project or writes to Hindsight.
- Phase 1 delivery regressions are closed and both context budgets remain enforced.
- Targeted tests and Ruff pass; unavailable live integrations are reported separately with their exact requirements.
