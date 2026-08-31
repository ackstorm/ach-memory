# Memory Quality Phase 3 Capture and Write Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace routine mid-task `retain` calls with one replay-safe transcript checkpoint pipeline that locally removes avoidable sensitive/noisy content, emits one-claim classified candidates, files them without Hindsight reclassification, and automatically advances the workspace Working State.

**Architecture:** Claude Code `Stop` and `PreCompact` hooks invoke a silent local checkpoint client. The client locks a per-session cursor, reads only the unprocessed complete JSONL records, sanitizes them before network transmission, and submits a content-addressed slice. The API authenticates and resolves the existing project/workspace/session boundary, then idempotently persists the sanitized slice in PostgreSQL. A separate database-leased worker is the only semantic extractor: it uses Hindsight's read-only `dry-run-extract` endpoint with a strict JSON-envelope prompt, validates and structurally normalizes the result, files already-extracted claims through a named `verbatim` retain strategy with deterministic operation IDs, waits for those operations, and finally writes Working State with the slice end offset as `checkpoint_seq`. Replaying any stage is safe.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy/Alembic, PostgreSQL leases, Hindsight API 0.9.1, FastMCP, Bash, pytest/respx, Docker Compose and Helm.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (§4.2–§6, §7–§10, §16.3, §17 Phase 3); sequencing, deferrals and gate: `docs/plans/2026-08-29-memory-quality-program.md` Phase 3.

## Review prerequisite from Phase 2

Phase 2 is integrated at `8a081ba`. Its targeted delivery gate passes (`365 passed`), Ruff is clean, Alembic has the single head `c3d4e5f6a7b8`, and the repository suite reaches `1180 passed, 2 skipped`. The remaining repository failure/error are live-integration prerequisites: no `MEMORY_MASTER_KEY` and no reachable Hindsight service.

The implementation review found no critical persistence or concurrency defect, but seven bounded contract gaps remain. Task 0 closes them before automatic writers depend on the Phase 2 boundary:

1. maximum-size Full payloads can omit mandatory Working State `age` and `source session` lines;
2. MCP schemas do not advertise the Pydantic bounds enforced at runtime;
3. `session_id` is not safely bounded/rendered and workspace validation uses prefix matching;
4. the Phase 2 migration downgrade collides after multiple workspace revisions exist;
5. master `On-Behalf-Of` brief reads omit the effective user's Working State;
6. Working State responses return a retired input slug rather than the resolved current slug;
7. proxy middleware can pair an explicitly named alternate project with the current repository's locator.

No Phase 3 code lands until the focused Task 0 regressions pass.

## Non-negotiable contracts

- The transcript hook is capture plumbing, not a semantic agent. It never decides memory kind, durability, bank or Working State content.
- Raw transcript bytes, credentials and cheaply reproducible repository file bodies never cross the network. The server receives only the locally sanitized slice and its identity metadata.
- A slice identity is exactly `(session_id, start_offset, end_offset, content_hash)`. `content_hash` is SHA-256 of the original complete-record byte slice; a separate sanitized payload hash protects transport/storage integrity.
- The local cursor advances only after the server acknowledges durable acceptance. A lost acknowledgement resubmits the exact same slice identity.
- The API does not call an LLM or Hindsight on the request path. Acceptance is one bounded PostgreSQL transaction and returns `202`.
- The queue state machine is durable. It may execute a stage more than once after a crash, but must never create duplicate Hindsight evidence or advance Working State twice.
- The background pass is the sole semantic extractor. Actual Hindsight retain uses a named `candidate_verbatim` strategy and must preserve each candidate as one fact without splitting, merging, rephrasing or inference.
- The model may propose `kind`, `origin`, `subject`, provenance and a Working State. It may not set `bank`, `profile_eligible`, eligibility tags/scopes, operation IDs or document IDs. Those are derived by code.
- `profile_eligible` is derived only from the validated `(kind, origin, provenance)` matrix. `inferred` is always evidence-only. An `observed` claim without an artifact is downgraded to `inferred`.
- Plans, proposed work, research directions and permission to execute are Working State or noise, never confirmed durable decisions.
- Project claims use impersonal wording. Personal preferences are rejected from the project route rather than silently rewritten into project truth.
- Explicit negative constraints retain their negation and exact subject scope.
- Explicit human correction supersession is fenced to its resolved bank and eligibility observation scope. It never invalidates or rewrites another user's or project's evidence.
- `set_working_state` remains the explicit handoff/override surface. The background worker is the only automatic writer.
- The default deployed state is `MEMORY_CAPTURE_ENABLED=false`. Phase 3 adds plumbing, tests and a local/dev gate; it does not mutate production Hindsight or switch mental-model inputs. Final production enablement remains a separate Phase 0 decision.
- Existing mental models keep their current fact inputs. Phase 3 may configure and test `profile_eligible` observation scopes, but the observation-only input switch and correction-triggered production refresh remain disabled until the Phase 0 invalidation/delta-refresh probe passes.
- Phase 3 enforces the provisional 15/25 eligible-observation source limits through Hindsight scope limits. Phase 4 still owns structured profile schemas, ordering and deterministic item displacement.

## Public and internal contracts

```http
POST /v1/capture/checkpoints
Authorization: Bearer <user credential>

{
  "host": "claude-code",
  "session_id": "...",
  "project_slug": "...",
  "git_locator": "...",
  "workspace_id": "ws_<32 hex>",
  "start_offset": 0,
  "end_offset": 4812,
  "content_hash": "<64 lowercase hex>",
  "sanitized_hash": "<64 lowercase hex>",
  "content": "<sanitized JSONL-derived text>"
}

202
{
  "capture_id": "<UUID>",
  "status": "pending|extracting|retaining|applying|completed|failed",
  "duplicate": false,
  "session_epoch": 17,
  "checkpoint_seq": 4812,
  "project_slug": "current-slug",
  "resolved_from": null
}
```

The response never exposes internal project IDs, bank IDs, stored content, Hindsight operation IDs or errors containing upstream identifiers.

```text
ach-memory capture-checkpoint --url <service>
  stdin: one Claude hook JSON object
  stdout: always empty
  exit: 0 for hook safety; diagnostics only in a private local log when opt-in

ach-memory capture-worker --once
ach-memory capture-worker

ach-memory capture-check --scope user|project --project <slug>
  read-only dry-run verification; no config or memory mutation
```

## Queue record and state machine

```text
capture_slices
  id UUID PRIMARY KEY
  tenant_id, user_id, project_internal_id, workspace_id
  host, session_id, session_epoch
  start_offset, end_offset, content_hash, sanitized_hash
  sanitized_content
  extraction JSON
  hindsight_operations JSON
  status, attempt_count, available_at, lease_owner, lease_until, last_error_code
  created_at, updated_at, completed_at

  UNIQUE (
    tenant_id, user_id, project_internal_id, workspace_id,
    session_id, start_offset, end_offset, content_hash
  )
```

State transitions are monotonic:

```text
pending -> extracting -> retaining -> applying -> completed
                      \-> failed (retryable through available_at)
```

- A lease is obtained with `FOR UPDATE SKIP LOCKED`, a bounded `lease_until`, and a random worker owner.
- `extraction` is persisted before any retain call. A retry never reruns a successful semantic extraction.
- One deterministic UUIDv5 operation ID is derived per `(capture_id, bank_kind)` and persisted before the call.
- All candidates for one resolved bank are sent in one async retain request. Each item has the slice document ID, fixed host context, exact metadata/tags/scopes and `strategy="candidate_verbatim"`.
- The worker polls recorded operations. Failed/incomplete operations never advance Working State.
- After all retain operations succeed, the worker applies Working State and marks the row complete in one database transaction. A crash between those actions retries the identical `(session_epoch, checkpoint_seq, payload)`, which Phase 2 treats as an idempotent no-op.
- On completion, `sanitized_content` and transient extractor output are cleared. Identity, status, non-sensitive counters and operation state remain for replay/audit.
- Retry uses exponential backoff capped at five minutes. After eight failed attempts the row remains `failed`; a master-only operational retry may reset `available_at`, but replaying the original checkpoint request does not replace the row or document.

## Extractor envelope

Each `ExtractedFact.text` returned by `dry-run-extract` must be one minified JSON object matching one of these discriminated records:

```json
{"record":"candidate","text":"Do not run destructive Git commands without approval.","kind":"preference","origin":"stated","subject":"user","provenance":{"type":"transcript","start":121,"end":188},"negative":true,"correction":false}
{"record":"working_state","objective":"Prepare Phase 3","current_direction":"Close Phase 2 review findings first","recent_decisions":[],"open_questions":[],"next_steps":["Implement Task 0"]}
```

Allowed semantic enums:

```text
kind    = preference | decision | convention | gotcha | technical_claim
origin  = stated | confirmed | observed | inferred
subject = user | project
```

The harness then performs, in order:

1. validate JSON and reject unknown fields/oversized text;
2. route Working State before candidate precedence;
3. require artifact provenance for `observed`, otherwise downgrade to `inferred`;
4. require failure plus cause/reproduction for `gotcha`, otherwise downgrade to `technical_claim`;
5. map `subject=user` to the caller's user bank and `subject=project` to the resolved project bank;
6. reject personal language from project candidates and non-impersonal project wording that cannot be safely preserved;
7. compute eligibility from the spec matrix;
8. attach exactly `kind:<kind>` plus one of `profile_eligible` / `evidence_only`;
9. attach exactly `[["profile_eligible"]]` or `[["evidence_only"]]` as observation scopes;
10. attach server-owned provenance/session metadata and deterministic identities.

The LLM never gets a `profile_eligible` output field. If an envelope contains it, validation fails closed and the slice is retried/flagged rather than trusting it.

## Hindsight configuration under test

Phase 3 defines the desired bank override but does not apply it in production:

```yaml
retain_default_strategy: candidate_verbatim
retain_strategies:
  candidate_verbatim:
    retain_extraction_mode: verbatim
    retain_mission: >-
      Each item is one already-extracted claim. Store it as one fact, as
      written. Do not split, merge, rephrase, infer, or add facts. Keep the
      metadata.
enable_observations: true
observations_mission: >-
  Observations are current truth. Merge restatements and count distinct
  sessions/sources, not raw repetitions. On conflict keep the current belief;
  keep an older choice only when a short reason prevents a likely future
  mistake. Keep genealogy in evidence. Never drop explicit negation.
entity_labels: <canonical user/agent actor vocabulary>
entities_allow_free_form: false  # user bank only
observation_scope_limits:
  - scope: [profile_eligible]
    limit: 15  # user bank; 25 for project bank
```

`capture-check` verifies with `dry-run-extract` that the `candidate_verbatim` strategy would return exactly the input claim, once, with no additional fact. A separate config diff shows intended changes. Applying the PATCH, enabling the worker, changing mental-model filters, refreshing models or touching production banks is outside this plan's authorized rollout.

## File map

- `src/memory/contracts.py`: reusable constrained workspace/session/checkpoint aliases shared by REST and MCP.
- `src/memory/brief.py`, `src/memory/working_state.py`, `src/memory/api/brief.py`: Phase 2 mandatory delivery and delegated-subject corrections.
- `src/memory/api/working_state.py`, `src/memory/mcp/tools.py`, `src/memory/mcp/proxy.py`: Phase 2 schema, resolved-slug and locator corrections.
- `migrations/versions/c3d4e5f6a7b8_working_state.py`: safe Phase 2 downgrade collapse.
- `src/memory/capture/contracts.py`: checkpoint request, extractor envelopes, normalized candidates and status types.
- `src/memory/capture/local.py`: transcript slicing, cursor locking, sanitization and HTTP submission.
- `src/memory/capture/repository.py`: idempotent acceptance, leases, retries and stage persistence.
- `src/memory/capture/extractor.py`: one semantic extraction call plus strict envelope parsing.
- `src/memory/capture/classifier.py`: structural origin/kind/provenance normalization, eligibility and routing.
- `src/memory/capture/filer.py`: deterministic Hindsight items/operations and completion polling.
- `src/memory/capture/worker.py`: durable queue state machine and Working State application.
- `src/memory/capture/configuration.py`: desired Hindsight strategies, missions, entity labels, scope budgets and read-only verification.
- `src/memory/api/capture.py`, `src/memory/api/app.py`: authenticated checkpoint acceptance.
- `src/memory/models.py`, `migrations/versions/d4e5f6a7b8c9_capture_slices.py`: capture queue persistence.
- `src/memory/hindsight/paths.py`, `src/memory/hindsight/client.py`: dry-run extraction, config reads/diffs and multi-item retain fields.
- `src/memory/api/memory.py`, `src/memory/mcp/tools.py`: explicit retain as fixed evidence-only, verbatim candidate capture.
- `src/memory/cli.py`: checkpoint client, worker and read-only capture check commands.
- `plugins/claude-code/hooks/hooks.json`, `plugins/claude-code/scripts/capture-checkpoint.sh`: silent Stop/PreCompact integration.
- `docker-compose.yml`, `deploy/helm/ach-memory/templates/capture-worker-deployment.yaml`, `deploy/helm/ach-memory/values.yaml`: disabled-by-default worker rollout.
- `src/memory/metrics.py`, `src/memory/activity.py`: content-free capture counters/status.
- `tests/fixtures/claude-transcripts/`: minimal redaction/slicing fixtures with synthetic canaries only.
- `tests/test_phase2_review_closure.py`, `tests/test_capture_local.py`, `tests/test_capture_api.py`, `tests/test_capture_repository.py`, `tests/test_capture_extractor.py`, `tests/test_capture_worker.py`, `tests/test_capture_configuration.py`, `tests/test_capture_e2e.py`: focused regressions and delivery gate.

---

### Task 0: Close the Phase 2 review findings

**Files:**

- Create: `src/memory/contracts.py`
- Modify: `src/memory/brief.py`
- Modify: `src/memory/working_state.py`
- Modify: `src/memory/api/working_state.py`
- Modify: `src/memory/api/brief.py`
- Modify: `src/memory/mcp/tools.py`
- Modify: `src/memory/mcp/proxy.py`
- Modify: `migrations/versions/c3d4e5f6a7b8_working_state.py`
- Create: `tests/test_phase2_review_closure.py`
- Modify: `tests/test_brief.py`
- Modify: `tests/test_mcp_surface_honesty.py`
- Modify: `tests/test_mcp_proxy.py`
- Modify: `tests/test_working_state.py`
- Modify: `tests/test_working_state_api.py`

- [ ] **Step 1: Pin all seven gaps with failing tests**

Add one focused regression per numbered finding in the review prerequisite. In particular:

- the maximum legal Working State payload in Full must include objective, `age:` and inert-rendered `source session:` within `FULL_MAX_TOKENS`;
- MCP input schemas must expose workspace `pattern`/length, `session_id` bounds, scalar `minimum`, item `maxLength` and list `maxItems`;
- workspace uses exact `fullmatch`, and blank/control/over-128 session IDs fail on REST and MCP without reaching PostgreSQL;
- an upgrade, two workspace revision writes, downgrade and re-upgrade succeeds;
- a master brief with `On-Behalf-Of` includes and fingerprints the effective user's Working State;
- a request through a retired slug returns the current slug plus rename metadata on REST and MCP;
- an explicit alternate project receives no auto-injected locator from the current repository.

- [ ] **Step 2: Verify the regressions fail for the reviewed reasons**

Run:

```bash
uv run pytest tests/test_phase2_review_closure.py tests/test_mcp_surface_honesty.py -v
```

Expected: FAIL on the seven contract assertions, not on setup or integration services.

- [ ] **Step 3: Make the surgical corrections**

Create shared constrained aliases and use them in the Pydantic request models and the public FastMCP signatures. Reserve an atomic Working State floor of objective + age + source before optional lines. Render the session through `inert()`. Resolve the effective user before `get_current()`. Return the domain's resolved project result rather than echoing request input. Inject proxy locator only in the same branch that injects an implicit project.

Before restoring the old three-column revision primary key on downgrade, delete non-empty workspace partitions and preserve the `workspace_id=""` row; if no legacy row exists, deterministically keep the newest row for the tuple. Use database time for Working State updates, include state time in `generated_at`, and add normal activity/metrics descriptions for the no-bank Working State paths while touching this seam.

- [ ] **Step 4: Run the repaired Phase 2 gate**

Run:

```bash
uv run pytest \
  tests/test_phase2_review_closure.py \
  tests/test_working_state.py \
  tests/test_working_state_api.py \
  tests/test_brief.py \
  tests/test_mcp_tools.py \
  tests/test_mcp_surface_honesty.py \
  tests/test_mcp_proxy.py \
  tests/test_agent_bundle.py \
  tests/test_cli.py -q
uv run ruff check src tests
uv run alembic heads
```

Expected: PASS and one Alembic head.

- [ ] **Step 5: Commit**

```bash
git add src/memory migrations/versions/c3d4e5f6a7b8_working_state.py tests
git commit -m "fix(working-state): close phase 2 contract gaps"
```

---

### Task 1: Build local slice identity, sanitization and cursor semantics

**Files:**

- Create: `src/memory/capture/__init__.py`
- Create: `src/memory/capture/contracts.py`
- Create: `src/memory/capture/local.py`
- Create: `tests/fixtures/claude-transcripts/basic.jsonl`
- Create: `tests/fixtures/claude-transcripts/redaction.jsonl`
- Create: `tests/test_capture_local.py`

- [ ] **Step 1: Write failing pure tests**

Cover complete-line offset slicing, an unterminated final record, empty slices, file truncation, concurrent Stop/PreCompact calls, lost-ack retry, and atomic cursor persistence. Assert identity uses raw complete-record bytes while only sanitized text is submitted.

Use synthetic fixtures to prove:

- common API keys, bearer tokens, PEM blocks, credential URLs and assignment-style secrets are replaced locally;
- tool results from file-reading tools become a bounded artifact marker, not file content;
- other tool outputs are capped per call and the whole slice is capped;
- user/assistant text and minimal tool name/status evidence survive;
- canaries, greetings and raw JSON dumps are omitted;
- no raw path, secret or file-body sentinel appears in the mocked HTTP request, cursor or diagnostic output.

- [ ] **Step 2: Verify the local tests fail**

Run:

```bash
uv run pytest tests/test_capture_local.py -v
```

Expected: FAIL because capture contracts and local pipeline do not exist.

- [ ] **Step 3: Implement a fail-closed local client**

Parse Claude transcript JSONL defensively and whitelist supported record/content types. Process only newline-terminated records. Hold an advisory lock around cursor read, slice build, submission and cursor update so overlapping hook events cannot emit divergent slices.

Store cursors below `ACH_MEMORY_CACHE_DIR/capture/` under an owner-fingerprint/service/project/workspace/session digest; never put credentials, user IDs, repository paths or transcript names in filenames. Write with temp + `fsync` + atomic replace and mode `0600`.

The client must return without a network call when the API key, workspace, project locator, transcript path or complete new records are unavailable. HTTP timeout is bounded. Only a `202` response matching the submitted identity advances the cursor.

- [ ] **Step 4: Run the local gate**

Run:

```bash
uv run pytest tests/test_capture_local.py -q
uv run ruff check src/memory/capture tests/test_capture_local.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/memory/capture tests/fixtures/claude-transcripts tests/test_capture_local.py
git commit -m "feat(capture): sanitize and identify transcript slices locally"
```

---

### Task 2: Persist idempotent checkpoint acceptance and durable leases

**Files:**

- Modify: `src/memory/models.py`
- Create: `migrations/versions/d4e5f6a7b8c9_capture_slices.py`
- Create: `src/memory/capture/repository.py`
- Create: `src/memory/api/capture.py`
- Modify: `src/memory/api/app.py`
- Modify: `src/memory/errors.py`
- Modify: `tests/test_models.py`
- Create: `tests/test_capture_repository.py`
- Create: `tests/test_capture_api.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Write failing model, repository and API tests**

Pin the full unique identity, tenant/user/project/workspace isolation, a duplicate returning the original row, same offsets with a different hash rejecting as a conflict, no-create project resolution, server session allocation/reuse, `checkpoint_seq == end_offset`, request size/hash validation, and master credential rejection.

For leasing, test two workers cannot own one row, an expired lease is recoverable, stage payloads survive a process boundary, retries increment attempts/backoff, and completion clears sanitized/extractor content.

- [ ] **Step 2: Verify failures**

Run:

```bash
uv run pytest tests/test_capture_repository.py tests/test_capture_api.py tests/test_models.py -v
```

Expected: FAIL because the table, repository and route do not exist.

- [ ] **Step 3: Add the queue schema and repository**

Use UUID primary keys, database timestamps, bounded text, JSON for stage state, non-negative offset/attempt constraints and the full identity unique constraint. Do not store the raw transcript or raw path. Add indexes for `(status, available_at)` and `lease_until`.

Acceptance resolves the project with `create=False`, validates the opaque workspace, reuses/allocates the Phase 2 `WorkingSession`, and performs insert-or-fetch under a savepoint. A duplicate with the exact identity/payload is `202 duplicate=true`; an overlapping identity or sanitized-hash mismatch is a typed `409`.

- [ ] **Step 4: Add authenticated API wiring**

Return the current project slug and optional rename metadata. Record only content-free activity fields: host, status, byte bucket and duplicate flag. Freeze the route in `tests/test_app.py`.

- [ ] **Step 5: Exercise migration and queue gates**

Run:

```bash
uv run alembic upgrade head
uv run alembic downgrade c3d4e5f6a7b8
uv run alembic upgrade head
uv run pytest tests/test_capture_repository.py tests/test_capture_api.py tests/test_models.py tests/test_app.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/memory/models.py src/memory/capture/repository.py src/memory/api/capture.py src/memory/api/app.py src/memory/errors.py migrations/versions/d4e5f6a7b8c9_capture_slices.py tests
git commit -m "feat(capture): persist replay-safe checkpoint jobs"
```

---

### Task 3: Add strict semantic envelopes and structural classification

**Files:**

- Modify: `src/memory/capture/contracts.py`
- Create: `src/memory/capture/classifier.py`
- Create: `tests/test_capture_classifier.py`

- [ ] **Step 1: Encode the spec matrix as failing table tests**

Cover every `kind × origin` cell, observed artifact downgrade, gotcha prerequisites, Working State precedence, user/project routing, impersonal project requirements, explicit negative constraints, corrections, and rejection of model-supplied bank/eligibility/tags.

Required semantic cases include:

- stated user preference -> user/profile eligible;
- observed project convention with artifact -> project/profile eligible;
- inferred convention -> project/evidence only;
- any technical claim -> evidence only;
- plan or next step -> Working State, not decision;
- permission to test -> neither confirmed decision nor durable profile input;
- “do not squash commits” remains negative and user- or project-scoped as stated;
- observed gotcha without failure/cause/repro -> technical claim;
- personal preference routed to project -> rejected.

- [ ] **Step 2: Verify failures**

Run:

```bash
uv run pytest tests/test_capture_classifier.py -v
```

Expected: FAIL because classifier rules do not exist.

- [ ] **Step 3: Implement pure validation and normalization**

Use discriminated Pydantic models with `extra="forbid"`, bounded single-line candidate text, bounded provenance and existing Working State field aliases. Keep semantic origin/subject selection separate from structural eligibility/routing.

Return an immutable normalized candidate containing only resolved `bank_kind`, derived eligibility, exact tags/scopes, validated provenance and correction scope. Never accept a bank ID from the extractor.

- [ ] **Step 4: Run the classifier gate**

Run:

```bash
uv run pytest tests/test_capture_classifier.py -q
uv run ruff check src/memory/capture tests/test_capture_classifier.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/memory/capture/contracts.py src/memory/capture/classifier.py tests/test_capture_classifier.py
git commit -m "feat(capture): classify candidates structurally"
```

---

### Task 4: Extend the Hindsight boundary and verify store-as-given behavior

**Files:**

- Modify: `src/memory/hindsight/paths.py`
- Modify: `src/memory/hindsight/client.py`
- Create: `src/memory/capture/configuration.py`
- Modify: `tests/test_hindsight_paths.py`
- Modify: `tests/test_hindsight_client.py`
- Create: `tests/test_capture_configuration.py`

- [ ] **Step 1: Write failing client-shape tests**

Pin the Hindsight 0.9.1 routes and bodies for:

- `POST /memories/dry-run-extract` with per-call custom extraction overrides;
- `GET /config` and a read-only desired/current diff;
- multi-item async retain with item-level `metadata`, `tags`, `observation_scopes`, `strategy` and one top-level caller UUID;
- no bank ID or upstream payload leakage on error.

Keep production config PATCH support private and uncalled in this phase; test only its request shape if needed by a later approved rollout.

- [ ] **Step 2: Verify failures**

Run:

```bash
uv run pytest tests/test_hindsight_paths.py tests/test_hindsight_client.py tests/test_capture_configuration.py -v
```

Expected: FAIL on missing paths/client methods/configuration.

- [ ] **Step 3: Implement the expanded boundary**

Add `dry_run_extract()`, `get_bank_config()` and `retain_items()`. Keep the existing single-item `retain()` as a compatibility wrapper. Validate deterministic operation IDs before the network call. Allow tags/scopes/strategy only through trusted server-owned item models, not arbitrary MCP metadata.

Define user/project desired overrides, canonical `user`/`agent` actor labels, the exact observation mission and the 15/25 `profile_eligible` scope limits. Do not change mental-model source filters.

- [ ] **Step 4: Add the read-only safety check**

`capture-check` must run a fixed one-claim fixture through `dry-run-extract` using `retain_extraction_mode="verbatim"` and assert one byte-identical fact. It prints the desired/current config diff with bank IDs redacted and exits non-zero on drift or changed extraction. It never PATCHes config or retains memory.

- [ ] **Step 5: Run the Hindsight boundary gate**

Run:

```bash
uv run pytest tests/test_hindsight_paths.py tests/test_hindsight_client.py tests/test_capture_configuration.py -q
uv run ruff check src/memory/hindsight src/memory/capture tests
```

Expected: PASS with no outbound config mutation in the request assertions.

- [ ] **Step 6: Commit**

```bash
git add src/memory/hindsight src/memory/capture/configuration.py tests/test_hindsight_paths.py tests/test_hindsight_client.py tests/test_capture_configuration.py
git commit -m "feat(capture): verify the hindsight candidate contract"
```

---

### Task 5: Implement the sole extractor and idempotent filer

**Files:**

- Create: `src/memory/capture/extractor.py`
- Create: `src/memory/capture/filer.py`
- Create: `tests/test_capture_extractor.py`
- Create: `tests/test_capture_filer.py`

- [ ] **Step 1: Write failing extractor tests**

Mock one `dry-run-extract` call per slice and feed valid, malformed, duplicated and adversarial envelopes. Assert malformed JSON, unknown fields, model-supplied eligibility, missing provenance and more than one Working State fail the whole extraction without partial filing.

Use curated fixtures for negative constraints, corrections, observed artifacts, inferred claims, project phrasing, plans and permission. Assert the custom prompt excludes greetings/logistics/canaries/cheap repo facts and emits one semantic claim per envelope.

- [ ] **Step 2: Write failing filer replay tests**

Assert:

- one async operation per resolved bank with deterministic UUIDv5;
- every item has the same slice document ID and fixed host context;
- metadata contains origin/kind/provenance/host/session/session_epoch/checkpoint/slice hash;
- tags are exactly eligibility + kind and scopes exactly the matching eligibility scope;
- user/session/origin never become tags;
- a lost retain acknowledgement retries the same operation ID;
- a pending/failed operation does not report filing complete;
- replay never uses document replacement as a casual retry.

- [ ] **Step 3: Verify failures**

Run:

```bash
uv run pytest tests/test_capture_extractor.py tests/test_capture_filer.py -v
```

Expected: FAIL because extractor/filer do not exist.

- [ ] **Step 4: Implement extraction and filing**

Use `dry-run-extract` with `retain_extraction_mode="custom"`, the strict JSON-envelope instructions and sanitized slice context. Parse only `facts[*].text`; discard Hindsight entity suggestions because canonical routing belongs to the harness.

Group normalized candidates by resolved bank and call `retain_items(..., strategy="candidate_verbatim", update_mode="append")` once per bank. The document ID is the content-addressed slice key. Append is used only for the first idempotent operation associated with that immutable key; retries rely on the operation ID and never resubmit with a new ID.

- [ ] **Step 5: Run the extraction/filing gate**

Run:

```bash
uv run pytest tests/test_capture_classifier.py tests/test_capture_extractor.py tests/test_capture_filer.py -q
uv run ruff check src/memory/capture tests
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/memory/capture/extractor.py src/memory/capture/filer.py tests/test_capture_extractor.py tests/test_capture_filer.py
git commit -m "feat(capture): extract and file one-claim candidates"
```

---

### Task 6: Drive the durable worker and automatic Working State

**Files:**

- Create: `src/memory/capture/worker.py`
- Modify: `src/memory/capture/repository.py`
- Modify: `src/memory/working_state.py`
- Modify: `src/memory/config.py`
- Create: `tests/test_capture_worker.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_brief.py`

- [ ] **Step 1: Write failing state-machine tests**

Exercise every crash boundary: after lease, after extraction persistence, after operation-ID persistence, after retain acknowledgement, after Hindsight completion, after Working State write and before completion mark. Restart the worker and assert one evidence set and one effective Working State advancement.

Also prove:

- a slice with no durable candidates can still update Working State;
- no extracted Working State leaves the prior row unchanged;
- a stale earlier slice cannot overwrite a later offset;
- exact replay does not change `updated_at` or revision;
- the subsequent Full/Index brief contains the automatic state, age and source;
- correction refresh remains gated off while profile-input activation is false.

- [ ] **Step 2: Verify failures**

Run:

```bash
uv run pytest tests/test_capture_worker.py tests/test_brief.py -k 'capture or automatic' -v
```

Expected: FAIL because the worker state machine does not exist.

- [ ] **Step 3: Implement one-stage-at-a-time processing**

Each lease invocation performs at most one externally visible stage, persists its result and releases the lease. Reuse the Phase 2 verified session tuple and call `working_state.replace()` with `checkpoint_seq=end_offset`. Do not invent a second ordering implementation.

Expose `capture-worker --once` for deterministic tests/operations and a loop form with interruptible polling. Configuration includes enabled flag, poll interval, lease length, attempt cap and batch size. Disabled means no lease acquisition, not “lease then skip.”

- [ ] **Step 4: Add correction fencing without activating profile inputs**

Persist correction scope on the normalized candidate and make the worker able to request refresh only for the affected resolved bank after its retain operation completes. Guard that call behind the separate profile-input activation flag, default false. Tests assert no refresh request under the Phase 3 default and the correct single-bank request when explicitly enabled in an isolated fixture.

- [ ] **Step 5: Run the worker gate**

Run:

```bash
uv run pytest tests/test_capture_repository.py tests/test_capture_worker.py tests/test_brief.py -q
uv run ruff check src/memory/capture src/memory/working_state.py tests
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/memory/capture src/memory/working_state.py src/memory/config.py tests/test_capture_worker.py tests/test_config.py tests/test_brief.py
git commit -m "feat(capture): advance working state from durable jobs"
```

---

### Task 7: Wire silent Claude checkpoints and preserve explicit retain safely

**Files:**

- Modify: `src/memory/cli.py`
- Create: `plugins/claude-code/scripts/capture-checkpoint.sh`
- Modify: `plugins/claude-code/hooks/hooks.json`
- Modify: `src/memory/api/memory.py`
- Modify: `src/memory/mcp/tools.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_agent_bundle.py`
- Modify: `tests/test_memory_api.py`
- Modify: `tests/test_mcp_tools.py`
- Modify: `tests/test_mcp_surface_honesty.py`

- [ ] **Step 1: Write failing hook and CLI tests**

Feed real-shaped synthetic Stop and PreCompact JSON. Assert both call the same checkpoint command, provide transcript/session/cwd fields, produce zero stdout/stderr, return zero on every network/service failure and never block Stop. Assert timeout is bounded and duplicate hook events share the same cursor behavior.

- [ ] **Step 2: Write failing explicit-retain contract tests**

Assert `retain` and `retain_sync` remain available but always use:

- `strategy="candidate_verbatim"`;
- exactly `evidence_only` and `kind:technical_claim` tags;
- `[["evidence_only"]]` observation scope;
- server-owned explicit-request provenance;
- no caller override of eligibility, kind, origin, tags or scopes.

Update descriptions to say the tool is for an explicit human “remember this” request and captures evidence, not guaranteed profile truth. Existing document/update-mode semantics and authorization remain intact.

- [ ] **Step 3: Verify failures**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_agent_bundle.py tests/test_memory_api.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py -k 'capture or checkpoint or retain' -v
```

Expected: FAIL on missing commands/hooks and old unclassified retain bodies.

- [ ] **Step 4: Implement CLI and plugin integration**

Add `capture-checkpoint`, `capture-worker` and `capture-check` dispatch before `init`-only arguments are accessed. The shell wrapper invokes the packaged version through the same release-pinned `uvx` source as `.mcp.json`, redirects all output, uses a short timeout and exits zero. Register both events with no blocking decision output.

Stop/PreCompact capture must be silent: Claude interprets Stop hook output as feedback and can re-enter the loop. Only SessionStart may emit context.

- [ ] **Step 5: Route explicit retain through trusted evidence fields**

Construct eligibility metadata/tags/scopes at the API boundary after caller validation. Do not accept those fields from MCP or generic metadata. Keep one submitted content item as one stored verbatim candidate.

- [ ] **Step 6: Run the surface gate**

Run:

```bash
uv run pytest tests/test_cli.py tests/test_agent_bundle.py tests/test_memory_api.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py -q
uv run ruff check src tests
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/memory/cli.py src/memory/api/memory.py src/memory/mcp/tools.py plugins/claude-code tests
git commit -m "feat(capture): checkpoint claude sessions without retain drip"
```

---

### Task 8: Add disabled-by-default worker deployment and observability

**Files:**

- Modify: `docker-compose.yml`
- Create: `deploy/helm/ach-memory/templates/capture-worker-deployment.yaml`
- Modify: `deploy/helm/ach-memory/values.yaml`
- Modify: `deploy/helm/README.md`
- Modify: `src/memory/metrics.py`
- Modify: `src/memory/activity.py`
- Create: `tests/test_capture_deployment.py`
- Modify: `tests/test_metrics.py`

- [ ] **Step 1: Write failing deployment and telemetry tests**

Assert Helm renders no worker by default, renders one worker only with explicit enablement, shares required database/Hindsight configuration, has no public Service/Ingress, and supports safe termination longer than the lease handoff. Compose exposes the worker only under an explicit `capture` profile.

Assert metrics/activity contain counts, stage, outcome, retry and latency only—never user/project/session IDs, transcript content, hashes, bank IDs or upstream error bodies.

- [ ] **Step 2: Verify failures**

Run:

```bash
uv run pytest tests/test_capture_deployment.py tests/test_metrics.py -v
```

Expected: FAIL because worker deployment and metrics do not exist.

- [ ] **Step 3: Add operational wiring**

Run the worker as a separate process using the same image and database migration gate. Default Helm/Compose values remain off. Document the required order for a later approved environment: migration, read-only `capture-check`, reviewed config diff/PATCH, isolated canary worker, replay test, then hook rollout.

Do not document a production enable command as part of this phase; Phase 0 supplies the separate approval and exact mutation procedure.

- [ ] **Step 4: Run deployment checks**

Run:

```bash
uv run pytest tests/test_capture_deployment.py tests/test_metrics.py -q
helm lint deploy/helm/ach-memory
docker compose config --quiet
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml deploy/helm src/memory/metrics.py src/memory/activity.py tests/test_capture_deployment.py tests/test_metrics.py
git commit -m "ops(capture): stage the disabled checkpoint worker"
```

---

### Task 9: Prove the Phase 3 delivery gate

**Files:**

- Create: `tests/test_capture_e2e.py`
- Modify: `docs/plans/2026-08-29-memory-quality-program.md`
- Modify: `SPEC-v1.md`

- [ ] **Step 1: Add an end-to-end replay scenario**

Use a synthetic Claude transcript containing:

- a stated user preference;
- an observed project convention with artifact provenance;
- an inferred technical claim;
- an explicit negative constraint;
- a project correction plus an unrelated user claim;
- a plan/next step for Working State;
- secret and repository-file canaries that must never reach the server.

Run local preprocessing, submit the same slice twice, drain the worker through every stage, submit it again after completion, and fetch the next brief.

Assert:

- one capture identity and no duplicate evidence/operations;
- one effective Working State advancement and automatic state in the subsequent brief;
- exact subject banks, provenance, kind, eligibility tags/scopes and negative wording;
- inferred evidence is recallable but not profile eligible;
- the correction is confined to the project bank/scope;
- no avoidable secret/file content reaches HTTP, PostgreSQL, Hindsight mocks, logs or metrics;
- mental-model input config remains unchanged and capture remains production-disabled.

- [ ] **Step 2: Run Phase 3 focused verification**

Run:

```bash
uv run pytest \
  tests/test_phase2_review_closure.py \
  tests/test_capture_local.py \
  tests/test_capture_api.py \
  tests/test_capture_repository.py \
  tests/test_capture_classifier.py \
  tests/test_capture_configuration.py \
  tests/test_capture_extractor.py \
  tests/test_capture_filer.py \
  tests/test_capture_worker.py \
  tests/test_capture_e2e.py \
  tests/test_agent_bundle.py \
  tests/test_mcp_surface_honesty.py -q
```

Expected: PASS.

- [ ] **Step 3: Run repository verification**

Run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run alembic heads
git diff --check
```

Expected: all non-live tests pass, live integration tests pass when their documented Hindsight service and credentials are present, Ruff is clean, Alembic reports only `d4e5f6a7b8c9`, and the diff is clean.

- [ ] **Step 4: Update only factual status documentation**

Mark the Phase 2 review corrections and Phase 3 implementation/gate complete. Record that production configuration mutation, capture enablement, observation-only mental-model inputs and correction-triggered production refresh remain deferred to Phase 0. Do not mark the production path active based only on mocked/local tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_capture_e2e.py docs/plans/2026-08-29-memory-quality-program.md SPEC-v1.md
git commit -m "test(capture): prove the phase 3 delivery gate"
```

## Completion boundary

Phase 3 is complete when the focused gate proves local sanitization, durable replay safety, one semantic extraction, exact candidate mapping and automatic Working State delivery; the repository verification is clean apart from explicitly unavailable live prerequisites; and all production mutation/activation flags remain off.

It is not complete merely because hooks submit slices, because Hindsight accepts candidates, or because a single happy-path brief contains Working State. It is also not authorization to PATCH production bank configuration, enable capture workers/hooks broadly, change mental-model inputs, refresh production profiles or start Phase 0 probes.
