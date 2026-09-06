# ach-memory v0.4.0 Release Review Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the verified post-merge review findings so v0.4.0 can be considered for release activation without weakening its retain, currentness, delivery, or migration contracts.

**Architecture:** Keep ach-memory as the control-plane harness around Hindsight: ACH authenticates, authorizes, sanitizes, records provenance, fences unsafe reads, and delivers bounded context; Hindsight owns semantic storage and synthesis. Corrections reuse the canonical retain sanitizer and acquire immutable ACH revisions, expired restores never reactivate upstream state, and every affected persisted model is withheld against a real refresh operation. Context remains a bounded concurrent read with one end-to-end deadline; release evidence must exercise the contracts it claims.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, Pydantic v2, SQLAlchemy 2, PostgreSQL, Alembic, pytest, respx, Ruff, Hindsight 0.9.2.

**Spec:** `docs/specs/2026-09-03-ach-memory-v0.4.0.md`

## Global Constraints

- Do not restore automatic transcript capture, a semantic extractor/router, profiles, session briefs, INDEX/FULL delivery, capture workers, or tests whose sole purpose is proving those removed surfaces are absent.
- No production cleanup, no production flag activation, and no mutation of non-disposable Hindsight banks.
- Every correction claim is mechanically normalized and secret-scanned by the same `normalize_claim` boundary as typed retain; the canonical limit is 4,096 UTF-8 bytes.
- Evidence never enters Hindsight. Physical bank IDs and upstream mental-model IDs never enter public responses or errors.
- Currentness correctness wins over availability: unsafe banks/models are withheld, but unrelated banks/models remain independently available.
- `load_context` has one two-second end-to-end deadline and a 4,608-token ceiling under `ach-delivery-o200k-v1`.
- One built-in plus five custom models per bank remains the maximum ACH-governed set.
- Use database time for authoritative retain/restore/curation boundary decisions.
- Write behavior tests, not absence/tombstone tests. Preserve all existing retained-product behavior.
- Prefix every shell command with `rtk`; use `apply_patch` for edits.

## Execution Order and Parallelism

```text
Task 1 (correction + expiry + revisions) ──> Task 2 (model currentness/idempotency) ──┐
Task 3 (context deadline + MCP honesty) ─────────────────────────────────────────────┼─> Task 4 (release evidence)
                                                                                     ┘
```

Tasks 1 and 3 may run in parallel in separate worktrees. Task 2 starts after Task 1 because both modify curation paths and migrations. Task 4 starts only after Tasks 1–3 land.

---

### Task 1: Make correction and expiry obey the canonical retain boundary

**Files:**
- Create: `migrations/versions/b8c9d0e1f2a3_v040_retained_record_revisions.py`
- Modify: `src/memory/models.py`
- Modify: `src/memory/retained_records.py`
- Modify: `src/memory/curation_service.py`
- Modify: `src/memory/api/curation.py`
- Modify: `src/memory/mcp/memory_tools.py`
- Test: `tests/test_retained_records.py`
- Test: `tests/test_curation_service.py`
- Test: `tests/test_curation_api.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `memory.sanitization.normalize_claim(content: str) -> str`, `LogicalBankRef`, database clock via `SELECT now()`.
- Produces: `RetainedRecordRevision`, `append_correction_revision(db: Session, retained: RetainedRecord, *, curation_operation_id: str) -> RetainedRecordRevision`, database-time validation in `accept_retain`, canonical correction content, and an expired-restore path that performs no upstream validation.

- [ ] **Step 1: Add failing correction-boundary tests**

Add REST and MCP tests showing that correction rejects secrets and a 4,097-byte canonical claim, accepts exactly 4,096 bytes, and persists/sends normalized canonical content rather than caller text. Exercise both a tracked ACH source and an authorized untracked legacy Hindsight memory.

Construct the token-shaped secret only inside the test process so repository text and agent prompts never contain a credential-shaped literal:

```python
token_shaped_secret = "".join(("g", "hp", "_", "A" * 36))
```

Use these exact expectations for both surfaces: `token_shaped_secret` returns `content_rejected_by_sanitizer` with zero upstream calls; `"x" * 4097` returns `CONTENT_TOO_LARGE` with zero upstream calls; `"x" * 4096` completes and sends 4,096 bytes; and `"Stable\t\tclaim.  \r\n"` sends `"Stable claim.\n"`. Name the REST tests `test_correct_rejects_secret`, `test_correct_rejects_canonical_oversize`, `test_correct_accepts_exact_canonical_limit`, and `test_correct_uses_normalized_claim`. Give the MCP twins the same suffix under `test_mcp_correct_*`.

- [ ] **Step 2: Run the correction tests and confirm RED**

Run:

```bash
rtk uv run pytest tests/test_curation_api.py tests/test_mcp_tools.py -q -k 'correct and (secret or 4096 or 4097 or canonical or normalize)'
```

Expected: failures show `correct` still uses `_check_content_size` and raw `content`, or Hindsight receives a secret/oversized/non-canonical value.

- [ ] **Step 3: Route every correction through `normalize_claim`**

Make `correct_record` canonicalize internally so no tracked caller can bypass the boundary. In the REST/MCP legacy fallback, authorize and resolve the bank first, then call `normalize_claim` before `client.curate`. Remove `_check_content_size` as the correction security contract; it remains appropriate for document/query transports but not canonical claims.

```python
def correct_record(db, retained, content, *, client, bank_id):
    canonical_content = normalize_claim(content)
    return _mutate(
        db,
        retained,
        action="correct",
        client=client,
        bank_id=bank_id,
        desired_content=canonical_content,
    )
```

Public responses must echo only the canonical corrected text when they include text at all.

- [ ] **Step 4: Add failing immutable-revision tests**

Test that a proven correction appends exactly one immutable prior-value revision, an exact retry does not append another revision, an unknown outcome leaves the prior canonical claim current, and hard delete purges revisions.

The tests use the existing `session`, `bank`, `hindsight`, and `_retained` fixtures. For `test_correction_preserves_prior_canonical_revision`, seed `canonical_content="old"`, correct to `"new"`, and assert the sole revision contains the retained ID, `"old"`, and the curation operation ID while the retained row contains `"new"`. For `test_exact_correction_retry_has_one_revision`, invoke the same correction twice, configure both upstream calls as proven success, and assert the revision count remains one. For an `HindsightOutcomeUnknown`, assert the revision exists for audit but `retained.canonical_content == "old"`.

- [ ] **Step 5: Add the revision table and append-once repository function**

Create `retained_record_revisions` with:

```text
id UUID primary key
retained_record_id UUID NOT NULL REFERENCES retained_records(id) ON DELETE CASCADE
curation_operation_id VARCHAR(128) NOT NULL UNIQUE
revision INTEGER NOT NULL
canonical_content TEXT NOT NULL
payload_hash VARCHAR(64) NOT NULL
memory_type VARCHAR(16) NOT NULL
basis VARCHAR(32) NOT NULL
trigger VARCHAR(32) NOT NULL
sanitized_evidence JSON NOT NULL
valid_until TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE(retained_record_id, revision)
```

Add the ORM model and:

```python
def append_correction_revision(
    db: Session,
    retained: RetainedRecord,
    *,
    curation_operation_id: str,
) -> RetainedRecordRevision:
    """Append the prior canonical state once for this correction operation."""
```

Call it after accepting/locking the curation operation and before the upstream correction. A retry finds the row by `curation_operation_id` and returns it without creating another revision.

- [ ] **Step 6: Add failing database-clock and expired-restore tests**

Add `test_accept_retain_rejects_valid_until_not_after_database_recorded_at` with `valid_until` equal to the database timestamp returned under the bank lock; assert `InvalidValidityWindow` and no row. Add `test_restore_expired_claim_keeps_upstream_invalidated` with `lifecycle="forgotten"` and `valid_until` one second before the database timestamp; assert `lifecycle == "expired"` and `hindsight.curate.call_count == 0`.

- [ ] **Step 7: Enforce the database-time validity boundary**

Inside the existing bank lock in `accept_retain`, read database time once, reject `valid_until <= now`, and assign that same value to `recorded_at` and `valid_from`. Keep Pydantic's early application-time check for fast feedback, but treat the database check as authoritative.

In `restore_record`, lock/read database time before issuing anything upstream. If the record is already expired, transition `forgotten -> expired`, record a completed local restoration outcome, and do not send `state="valid"`; the preceding proven forget already established invalidation. A non-expired restore continues through normal upstream curation.

- [ ] **Step 8: Run Task 1 tests**

Run:

```bash
rtk uv run pytest tests/test_retained_records.py tests/test_curation_service.py tests/test_curation_api.py tests/test_mcp_tools.py -q
rtk uv run ruff check src/memory/retained_records.py src/memory/curation_service.py src/memory/api/curation.py src/memory/mcp/memory_tools.py tests/test_retained_records.py tests/test_curation_service.py tests/test_curation_api.py tests/test_mcp_tools.py
rtk git diff --check
```

Expected: all selected tests pass; no correction path can send non-canonical caller text; restore of an expired record makes zero upstream validation calls.

- [ ] **Step 9: Commit Task 1**

```bash
rtk git add migrations/versions/b8c9d0e1f2a3_v040_retained_record_revisions.py src/memory/models.py src/memory/retained_records.py src/memory/curation_service.py src/memory/api/curation.py src/memory/mcp/memory_tools.py tests/test_retained_records.py tests/test_curation_service.py tests/test_curation_api.py tests/test_mcp_tools.py
rtk git commit -m "fix(v040): enforce canonical correction and expiry boundaries"
```

---

### Task 2: Make model currentness and mutation recovery use real operation identities

**Files:**
- Create: `migrations/versions/c9d0e1f2a3b4_v040_model_mutation_ledger.py`
- Modify: `src/memory/models.py`
- Modify: `src/memory/model_registry.py`
- Modify: `src/memory/curation_service.py`
- Modify: `src/memory/mental_model_service.py`
- Modify: `src/memory/api/mental_models.py`
- Modify: `src/memory/mcp/model_tools.py`
- Test: `tests/test_curation_service.py`
- Test: `tests/test_mental_model_service.py`
- Test: `tests/test_model_refresh.py`
- Test: `tests/test_mental_models_api.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: canonical curation operation/revision behavior from Task 1 and Hindsight's returned `operation_id`.
- Produces: `MentalModelMutation`, `require_model_refresh(db, bank, model_key, *, repair_not_before)`, `record_model_refresh_operation(db, bank, model_key, operation_id)`, idempotent create/update/refresh/delete retries, and exact per-model currentness barriers.

- [ ] **Step 1: Add failing safety-refresh tests**

Cover correction, forget, restore, hard delete, and source/prompt update for indefinite claims/models. Each affected model must be withheld before it can be delivered, `refresh_mental_model` must be called, and `refresh_operation_id` must equal the ID returned by Hindsight—never a locally generated UUID. Expiring claims must not trigger persisted-model refresh.

Use `hindsight.curate.return_value={"id": retained.source_memory_id}` and `hindsight.refresh_mental_model.return_value={"operation_id": "refresh-42"}`. After correcting an indefinite `_retained` row, assert the admitting registration has `delivery_state="withheld"`, `refresh_status="pending"`, and `refresh_operation_id="refresh-42"`. After hard delete, assert the retained row is gone, the previously selected registration is withheld against `"refresh-42"`, and `refresh_mental_model` was called once with the resolved physical bank and upstream model ID.

- [ ] **Step 2: Run safety-refresh tests and confirm RED**

Run:

```bash
rtk uv run pytest tests/test_curation_service.py tests/test_model_refresh.py -q -k 'refresh or delete or correction or restore'
```

Expected: current code records random UUIDs, skips hard-delete refresh, or fails to issue refresh calls.

- [ ] **Step 3: Replace fake withholding with a recoverable refresh-required state**

Add repository functions with these contracts:

```python
def require_model_refresh(
    db: Session,
    bank: LogicalBankRef,
    model_key: str,
    *,
    repair_not_before: datetime,
) -> MentalModelRegistration:
    """Withhold now with refresh_status='required' and no invented operation id."""

def record_model_refresh_operation(
    db: Session,
    bank: LogicalBankRef,
    model_key: str,
    operation_id: str,
) -> MentalModelRegistration:
    """Replace required state with the exact upstream operation identity."""
```

The repair selector must include `refresh_status='required'` even when `refresh_operation_id IS NULL`. `repair_one_model` submits one refresh, records the returned ID, and stops. Pending/completed/failed observation continues to compare the exact recorded ID.

For curation, compute affected models before deleting any retained row. After the source mutation is proven, mark every affected model refresh-required and commit that safe state before submitting refresh requests. Submit each request independently; a failure leaves that model withheld and repairable without blocking unrelated models. Hard delete purges the claim/revisions only after the affected set has been recorded.

Close the crash window around the source mutation itself: commit the curation operation and physical-bank barrier before calling Hindsight. Reconciliation must claim both `pending` and `unknown` operations. Once the source outcome is proven, atomically persist the ACH lifecycle change plus every affected model's refresh-required state, then release the bank barrier; individual model barriers remain until their exact refresh operations succeed. A process death after the upstream commit therefore leaves either the bank barrier or model barriers in place, never a falsely current bank/model.

- [ ] **Step 4: Add failing crash/retry tests for every public model mutation**

Use the fixed operation ID `11111111-1111-4111-8111-111111111111` in every retry test. Seed a `creating` registration with the matching payload digest and an exact upstream `ach:{model_key}` candidate; `create_custom_model` must return `lifecycle_state="active"` through recovery. Parameterize update, refresh and delete exact retries and assert one upstream mutation. Reuse the update operation ID with `source_query="first"` and then `source_query="different"`; assert `IdempotencyConflict` before a second upstream call.

- [ ] **Step 5: Add a durable model-mutation ledger**

Create `mental_model_mutations` with logical bank identity, `model_key`, caller `operation_id`, action, canonical payload hash, state, nullable upstream operation ID, timestamps, and the same user/project scope check used by the registry. Uniqueness is `(tenant_id, scope, user_id, project_internal_id, operation_id)` with PostgreSQL nulls-not-distinct semantics.

All four public mutations must accept and consume the supplied operation ID:

```python
create_custom_model(db, bank, request, *, client)
update_model(db, bank, model_key, request, *, client)
refresh_model(db, bank, model_key, *, operation_id: str, client)
delete_model(db, bank, model_key, *, operation_id: str, client)
```

The MCP `refresh_mental_model` and `delete_mental_model` body factories must use `MutationScopedRequest`, not discard their `operation_id`. Matching completed retries return the recorded result; matching pending create retries call `resume_model_mutation`; mismatched payload hashes return `IdempotencyConflict` before any upstream call.

- [ ] **Step 6: Make source-definition updates withhold and refresh**

Display-name-only and `always_in_context`-only updates do not refresh. Any change to `source_query`, `max_tokens`, or `trigger` must:

1. durably withhold the model as refresh-required;
2. apply the upstream definition update;
3. request refresh;
4. record the exact refresh operation ID;
5. remain withheld until `observe_model_refresh` proves success.

Add tests for a lost update response, a refresh submission failure, pending observation, failed observation/backoff, repair, and successful release.

- [ ] **Step 7: Run Task 2 tests**

Run:

```bash
rtk uv run pytest tests/test_curation_service.py tests/test_mental_model_service.py tests/test_model_refresh.py tests/test_mental_models_api.py tests/test_mcp_tools.py -q
rtk uv run alembic heads
rtk uv run ruff check src/memory/models.py src/memory/model_registry.py src/memory/curation_service.py src/memory/mental_model_service.py src/memory/api/mental_models.py src/memory/mcp/model_tools.py tests/test_curation_service.py tests/test_mental_model_service.py tests/test_model_refresh.py tests/test_mental_models_api.py tests/test_mcp_tools.py
rtk git diff --check
```

Expected: all selected tests pass and Alembic reports exactly `c9d0e1f2a3b4 (head)`.

- [ ] **Step 8: Commit Task 2**

```bash
rtk git add migrations/versions/c9d0e1f2a3b4_v040_model_mutation_ledger.py src/memory/models.py src/memory/model_registry.py src/memory/curation_service.py src/memory/mental_model_service.py src/memory/api/mental_models.py src/memory/mcp/model_tools.py tests/test_curation_service.py tests/test_mental_model_service.py tests/test_model_refresh.py tests/test_mental_models_api.py tests/test_mcp_tools.py
rtk git commit -m "fix(v040): fence model mutations with real refresh operations"
```

---

### Task 3: Enforce end-to-end context bounds and honest MCP annotations

**Files:**
- Modify: `src/memory/context_service.py`
- Modify: `src/memory/mcp/memory_tools.py`
- Modify: `tests/test_context_service.py`
- Modify: `tests/test_mcp_surface_honesty.py`
- Modify: `tests/test_v040_context_live.py`

**Interfaces:**
- Consumes: `ContextPayload`, `DeliveryOmission`, `DEADLINE_SECONDS=2.0`, `ACTIVE_CLAIMS_TOKENS=256`.
- Produces: a single monotonic deadline propagated through context assembly, bounded active-claim selection, and truthful non-read-only annotations for maintenance-capable tools.

- [ ] **Step 1: Add deterministic deadline and large-ledger tests**

Use a fake client that makes one model exceed the shared deadline while peers return immediately. Instrument database/model/claims/Working-State phases with a controllable monotonic clock. Assert wall time is bounded, peers remain present, the slow model is omitted, and no phase starts after the deadline.

Create a local `_StepClock` test double whose `__call__` returns a controlled float and whose `advance(seconds)` increments it. Inject it into `ContextService`. The deadline test advances past `2.0` after one fast model and asserts the fast heading remains, `slow-model` has `model_unavailable`, no Working-State read occurs after expiry, and the observed deadline never resets. The ledger test inserts exactly 1,000 active expiring rows, asserts the repository query fetches no more than the named bounded prefix, `total_tokens <= 4608`, and the `active-claims` omission count equals the total rows not rendered.

- [ ] **Step 2: Run context tests and confirm RED**

Run:

```bash
rtk uv run pytest tests/test_context_service.py -q -k 'deadline or bounded or slow or claim'
```

Expected: the current implementation performs unbounded claim loading and continues database/render work after the model-wait deadline.

- [ ] **Step 3: Apply one remaining-time budget across the complete request**

Inject `clock: Callable[[], float] = monotonic` into `ContextService` for deterministic tests. Compute one deadline at method entry and use:

```python
def remaining() -> float:
    return max(0.0, deadline - self.clock())
```

Before every database/upstream phase, stop adding sections when `remaining() == 0` and emit a machine-readable omission. Pass the remaining timeout to every Hindsight read. Keep `ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)` so an over-deadline peer never delays the response.

For PostgreSQL reads, set a transaction-local statement timeout from the remaining milliseconds before potentially variable registry/claim/Working-State queries. Fetch active claims as an ordered bounded prefix plus total count—do not materialize the whole ledger. The response uses the existing whole-entry 256-token packing and reports both query-tail and token-budget omissions without exposing content.

- [ ] **Step 4: Correct MCP read-only metadata**

Change `recall` and `reflect` to `readOnlyHint=False` because `run_access_maintenance` can claim expiry work and commit database changes. Keep truly non-mutating list/get/history/context tools read-only.

```python
EXPECTED_READ_ONLY = {
    "recall": False,
    "reflect": False,
    "memory_history": True,
    "load_context": True,
}
```

Update `tests/test_mcp_surface_honesty.py` to assert this exact truth table and the user-facing descriptions of maintenance side effects.

- [ ] **Step 5: Strengthen the live context gate**

Extend `tests/test_v040_context_live.py` with two disposable-bank configurations:

1. maximum: four 256-token User custom models plus five 256-token Project custom models;
2. default-like: both built-ins plus two User and four Project custom models.

Retain a unique harmless marker for each model and assert every expected ready heading/output is delivered. Wrap one model GET in a controlled client that sleeps beyond two seconds while delegating all other IDs to the real Hindsight client; assert peers, Project Metadata, active claims and Working State return before the deadline with exactly one `model_unavailable` omission. Cleanup failure remains a test failure.

- [ ] **Step 6: Run Task 3 tests**

Run:

```bash
rtk uv run pytest tests/test_context_service.py tests/test_mcp_surface_honesty.py -q
rtk uv run ruff check src/memory/context_service.py src/memory/mcp/memory_tools.py tests/test_context_service.py tests/test_mcp_surface_honesty.py tests/test_v040_context_live.py
rtk git diff --check
```

Expected: unit tests pass; live test remains opt-in and unexecuted in this task.

- [ ] **Step 7: Commit Task 3**

```bash
rtk git add src/memory/context_service.py src/memory/mcp/memory_tools.py tests/test_context_service.py tests/test_mcp_surface_honesty.py tests/test_v040_context_live.py
rtk git commit -m "fix(v040): enforce bounded context and honest MCP effects"
```

---

### Task 4: Align migration, evaluator, documentation, and release evidence

**Files:**
- Modify: `tests/test_v040_upgrade.py`
- Modify: `scripts/evaluate-retain-skill.py`
- Modify: `tests/test_retain_skill_evaluation.py`
- Modify: `README.md`
- Modify: `docs/releases/0.4.0.md`
- Modify: `docs/results/2026-09-04-ach-memory-v0-4-0-release.md`
- Modify: `docs/results/2026-09-04-ach-memory-v0-4-0-test-portfolio.md`

**Interfaces:**
- Consumes: completed Tasks 1–3 and their two new migrations.
- Produces: exact v0.3.5 upgrade/downgrade evidence, majority-scored evaluator output, current public documentation, final non-live/live release evidence, and one Alembic head.

- [ ] **Step 1: Add failing migration-evidence tests**

Expand the v0.3.5 fixture to seed:

- canonical and retired project slugs;
- one Working Session and Working State row;
- one complete `capture_slices` row and one `context_revisions` row using the exact predecessor schemas;
- user/project identity and bank IDs.

After upgrading to head, assert identity/slugs/Working State survive, removed tables are gone, new revision/model-mutation tables exist and are empty, registries/ledgers are empty unless explicitly seeded, and no Hindsight client method was called. The test is a forward upgrade gate; do not edit the already-landed `a7b8c9d0e1f2` migration merely to improve its downgrade path.

Implement two explicit upgrade paths. `test_released_v035_data_survives_v040_upgrade` starts at `b7d99d980665`, seeds released identity/project/slug data, upgrades to head and asserts exact preservation. `test_pre_retirement_state_survives_forward_removal` upgrades a disposable database to `f6a7b8c9d0e1`, seeds Working Session, Working State, capture and context-revision rows, upgrades to head, asserts Working State preservation and removal of only the two obsolete tables, and runs under an HTTP deny-all/mock transport proving zero Hindsight requests.

- [ ] **Step 2: Record the downgrade limitation without rewriting migration history**

Leave `a7b8c9d0e1f2` byte-for-byte unchanged. Add a release note that rollback from v0.4.0 is restore-from-snapshot/forward-fix only because retired capture data is not reconstructable. This is the YAGNI ruling on the reviewer's downgrade suggestion: the v0.4.0 SPEC requires safe forward migration and forbids editing an applied migration; it does not promise a usable predecessor capture pipeline after downgrade.

- [ ] **Step 3: Add failing evaluator-majority tests**

Construct rows where one of three repetitions differs. Assert ordinary recall/precision/abstention/type metrics use one 2-of-3 decision per `(family, case_id)`, while any single raw wrong-scope or secret-retention action still fails the hard gate.

```python
def test_ordinary_metrics_use_per_family_case_majority():
    rows = three_runs(one_outlier=True)
    score = score_rows(rows, corpus, policy)
    assert score["aggregate_recall"] == 1.0

def test_single_raw_wrong_scope_is_never_hidden_by_majority():
    rows = three_runs(one_wrong_scope=True)
    score = score_rows(rows, corpus, policy)
    assert score["wrong_scope"] == 1
    assert "WRONG_SCOPE" in score["reason_codes"]
```

- [ ] **Step 4: Implement the frozen 2-of-3 scoring contract**

Group by `(family, case_id)`. For ordinary metrics, choose the majority closed decision tuple `(action, scope, memory_type)` and score 40 family/case outcomes. Keep `wrong_scope` and `secret_retention` as raw-run scans across all 120 rows. Report both `raw_rows=120` and `majority_outcomes=40`, plus per-family metrics and failing case IDs. Do not rerun agent families unless the canonical skill itself changed.

- [ ] **Step 5: Rewrite README for the actual v0.4.0 product**

Update all install examples to `v0.4.0`, remove old `0.1.2` output, link `docs/specs/2026-09-03-ach-memory-v0.4.0.md`, and describe explicit proactive retain through the canonical skill. Replace the obsolete 15-tool claim with a generated/current tool count or a capability list that does not hard-code a count. Document:

- canonical 4 KiB sanitizer/secret boundary;
- `ach-exact-v1` and English-only retained claims;
- one built-in plus five custom models per bank;
- `always_in_context`, active time-bounded claims and Working State budgets;
- `recall`/`reflect` bounded expiry-maintenance side effects;
- Hindsight 0.9.2 compatibility and disposable live gate;
- separate approval for production cleanup.

Do not add a catalog of the removed capture/profile product.

- [ ] **Step 6: Run focused non-live gates**

Run:

```bash
rtk uv run pytest tests/test_v040_upgrade.py tests/test_retain_skill_evaluation.py tests/test_context_service.py tests/test_curation_service.py tests/test_mental_model_service.py tests/test_model_refresh.py tests/test_mcp_surface_honesty.py -q
rtk uv run python scripts/evaluate-retain-skill.py validate
rtk uv run python scripts/evaluate-retain-skill.py score
rtk uv lock --check
rtk uv run ruff check .
rtk git diff --check
rtk uv run alembic heads
```

Expected: all focused gates pass; evaluator reports 120 raw rows and 40 majority outcomes; Alembic reports one head.

- [ ] **Step 7: Run the full non-live suite**

Run:

```bash
rtk uv run pytest -q --ignore=tests/test_integration_hindsight.py --ignore=tests/test_append_integration.py --ignore=tests/test_v040_hindsight_live.py --ignore=tests/test_v040_mental_models_live.py --ignore=tests/test_v040_context_live.py
```

Expected: zero failures. Record exact passed/skipped counts and runtime; do not optimize for test count.

- [ ] **Step 8: Run disposable Hindsight 0.9.2 live gates**

Preflight `127.0.0.1:8888`. If unavailable, establish the repository's existing loopback-only Kubernetes port-forward and verify `/openapi.json` reports Hindsight 0.9.2 before mutation. Then run:

```bash
rtk env HINDSIGHT_V040_CONFIRM=disposable-banks-only uv run pytest tests/test_v040_hindsight_live.py tests/test_v040_mental_models_live.py tests/test_v040_context_live.py -q
```

Expected: all live gates pass, both context configurations remain under two-second p95, the controlled slow model fails open without hiding peers, and every disposable bank is removed. Never point the confirmation flag at staging or production.

- [ ] **Step 9: Update release evidence from measured outputs**

Update release/result documents with exact commands, commit SHA, counts, runtime, context p95 for both mixes, controlled-unavailable behavior, Hindsight version, migration head, evaluator metrics, and cleanup confirmation. Remove or correct claims broader than the executed evidence. Keep production activation and production cleanup explicitly unperformed.

- [ ] **Step 10: Commit Task 4**

```bash
rtk git add tests/test_v040_upgrade.py scripts/evaluate-retain-skill.py tests/test_retain_skill_evaluation.py README.md docs/releases/0.4.0.md docs/results/2026-09-04-ach-memory-v0-4-0-release.md docs/results/2026-09-04-ach-memory-v0-4-0-test-portfolio.md
rtk git commit -m "docs(v040): close measured release review gates"
```

---

## Final Review Gate

- [ ] Request a fresh independent code review over the complete closure range.
- [ ] Fix every verified Critical and Important issue before release activation.
- [ ] Re-run the exact focused, full non-live, and disposable-live commands after the final fix commit.
- [ ] Confirm `rtk git status --short` is empty.
- [ ] Confirm no production cleanup or production activation occurred.
- [ ] Record the final release verdict as `production_eligible=true|false` from evidence; do not infer it from a green unit suite alone.
