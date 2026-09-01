# Memory Quality Program — General Plan

> **For agentic workers:** This is a sequencing and approval roadmap, not an execution checklist. Before changing code or production data, write and obtain approval for the detailed plan of the selected phase.

**Goal:** Bring ach-memory into conformance with `SPEC-memory-quality-v1.4` by completing the delivery path, adding Working State and disciplined capture, then improving profiles and recall without mutating live memory until the final hygiene phase.

**Architecture:** The current system already delivers a compact index through MCP instructions and a full brief through Claude Code SessionStart. The remaining program adds an ephemeral Working State layer, an idempotent transcript-to-candidate pipeline, structured durable profiles, and a truly read-only long-tail recall surface. Production hygiene is intentionally sequenced last; Phase 3 must therefore install its mechanisms without switching profile synthesis to observation-only input until the hygiene probe has run.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, Hindsight 0.9.x, FastMCP, Bash host hooks, pytest/respx.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (the versioned repository copy; do not confuse it with the historical untracked root draft).

---

## Current Baseline

The delivery portion of Phase 1 is implemented and reviewed:

- `GET /v1/session-brief` resolves master-key user reads correctly and has no provisioning side effect.
- Project metadata supplies deterministic orientation.
- The compiler emits index and full tiers with `brief_revision` and `memory_protocol`.
- The stdio MCP proxy serves a cached index immediately and refreshes it asynchronously.
- Claude Code SessionStart fetches the full tier with a bounded request and a credential-isolated last-good cache.
- Static consumer policy lives in the host activation files rather than in synthesized memory.

The following delivery gaps remain and are the first detailed-plan scope:

1. Bound the full tier to the SPEC's provisional context budget rather than composing every available profile line.
2. Make cache age visible for every cached tier, not only the hook's offline fallback.
3. State unambiguously in delivered context when the full tier is unavailable and the session has only the MCP index.
4. Preserve the existing design decision that host policy is outside dynamic memory: the SessionStart-delivered Full payload includes the static consumer contract from host activation plus the dynamic brief from `/v1/session-brief`; the compiler does not synthesize or duplicate that policy.

## Program Order

```text
Phase 1 closure → Phase 2 Working State → Phase 3 capture quality
        → Phase 4 structured profiles → Phase 5 read-only recall
        → Phase 0 production hygiene and final profile-input gate
```

Phase 0 is last by explicit product decision. This overrides only the execution order shown in SPEC §17; its operations, lifecycle probe and gate remain unchanged. Nothing in this program authorizes a mutation of production memory before that final phase receives a separate, explicit approval.

## Phase 1 Closure — Delivery Conformance

**Purpose:** Turn the delivered tiers already in production code into a fully measurable, bounded consumer contract.

**Detailed plan must cover:**

- full-tier selection and a testable provisional budget;
- cache metadata format and visible age for index and full cache hits;
- no-full-tier fallback copy that does not claim memory is absent;
- delivered-payload tests exercising live, cached and unavailable paths;
- compatibility/migration behavior for existing cache files.

**Gate:** A host-facing test proves the index remains under its host budget, the full tier respects its configured budget, each cached payload exposes its revision and age, and a failed full fetch leaves startup successful and clearly limited to the index.

**Out of scope:** Working State storage, transcript extraction, profile schema, production bank cleanup.

## Phase 2 — Working State

**Status:** Implemented and integrated at `8a081ba`. The follow-up implementation review's seven bounded contract gaps were closed in Phase 3 Task 0 (`99c391f`) before any automatic writer depended on this boundary.

**Purpose:** Add explicit, ephemeral continuity without letting current task state become durable memory.

**Detailed plan must cover:**

- a record keyed by `(user, project, workspace)`, with workspace derived from the git worktree root;
- migration, repository/API boundary, authorization and an explicit human-handoff write path;
- server-assigned `session_epoch` and lexicographic `(session_epoch, checkpoint_seq)` conflict prevention;
- age and source-session presentation in both tiers;
- revision fingerprint expansion so Working State changes update `brief_revision`;
- independent-worktree and stale-state tests.

**Gate:** An explicit handoff appears in delivered context with age/source; a lower checkpoint cannot overwrite a higher one; independent worktrees remain isolated.

**Out of scope:** automatic transcript capture and concurrent merge semantics for a shared workspace.

## Phase 3 — Capture and Write Quality

**Status:** Implementation and its focused delivery gate are complete (Tasks 0–9; `test_capture_e2e.py`'s replay scenario is the gate's proof). All production mutation/activation flags remain off: `MEMORY_CAPTURE_ENABLED` (local hook), `MEMORY_CAPTURE_WORKER_ENABLED` and `MEMORY_CAPTURE_CORRECTION_REFRESH_ENABLED` (server) all default `false`, no code path PATCHes Hindsight bank config, and mental-model input configuration is unchanged. This is implementation-complete, not production-enabled -- the deferred switch below and the rollout order in `deploy/helm/README.md` are still gated on Phase 0.

**Purpose:** Replace unstructured mid-task fact drip with a single, idempotent pipeline that emits classified, one-claim candidates and Working State.

**Detailed plan must cover:**

- Stop/PreCompact (or host-equivalent) transcript checkpoint inputs and local redaction/capping before network transmission;
- slice identity, offsets, hashes, caller operation IDs and at-least-once retry behavior;
- one background semantic extractor with `origin`, `kind`, provenance, bank routing and structural `profile_eligible` classification;
- store-as-given retain configuration, observation mission, entity labels and eligibility tags/scopes;
- explicit `retain` retained as deliberate evidence capture, not a bypass around classification;
- negative constraints, scope-fenced supersession and inferred-claim tests.

**Deferred switch:** Do not feed profiles from observation-only eligible input until final Phase 0 validates invalidation plus delta refresh. The plumbing and tests may land here; activation waits for that probe.

**Gate:** A replayed transcript slice produces no duplicate evidence or Working State advancement; automatic Working State arrives in a subsequent brief; candidates preserve subject scope, provenance and negative meaning.

## Phase 4 — Structured Profiles

**Status:** Implementation and its delivery gate are complete (Tasks 0–8). Phase 3's review closure landed first, at `ee25a56` (the Task 0 fix loop `e64843e..ee25a56`, pinned as regressions by `test_phase3_review_closure.py`); structured-profile work starts at `f44efd5`. The gate is `tests/test_profile_e2e.py`: 22 tests that drive the real `GET /v1/session-brief` (JSON and the hook's `format=text` tier) over a mocked Hindsight and assert on the delivered payload, covering the 15/25 budgets, deterministic displacement, every ineligible durability cell plus bare technical and inferred claims, gotcha cause/reproduction/provenance, Project Metadata and Working State surviving allocation pressure, correction supersession with no delete call, workspace and credential-owner isolation, and a legacy-versus-structured comparison. Focused suite (`test_phase3_review_closure`, `test_profiles`, `test_profile_provisioning`, `test_profile_evaluation`, `test_profile_e2e`, `test_brief`, `test_capture_worker`, `test_mcp_surface_honesty`): 607 passed. Full suite: 1946 passed, 2 skipped, with only the two pre-existing live-Hindsight/`MEMORY_MASTER_KEY` integration prerequisites unmet (`test_integration_hindsight.py::test_retain_then_recall_round_trip` and `test_append_integration.py`'s setup error). Ruff clean, `git diff --check` clean, single Alembic head (`d4e5f6a7b8c9`). The two response schemas are pinned by hash in `tests/test_profiles.py` — user `645f19c0…4cd2735d`, project `bef57794…53995dbc` — so an accidental change to the contract Hindsight is asked to fill in fails a test rather than reaching a bank silently.

Nightly evaluation has **not** been run. Task 7 built and tested the instrument (`ach-memory profile-check`, a non-persisting dry-run evaluator with content-free output), but no live Hindsight instance and no staging bank exist in this development environment, so zero of the seven-plus representative runs per scope the rollout order requires have been executed. Recorded token/duration/quality ceilings are still outstanding, and they gate enabling `structured` anywhere.

Production memory and configuration were not mutated by this phase: nothing PATCHes bank config or provisions a production model, no cron or observation-only trigger was installed, and reads stay side-effect free. `MEMORY_PROFILE_DELIVERY_MODE` remains `legacy` everywhere — it is the default in `Settings`, in `.env.example` and in every deployment — and a bank holding both a legacy prose model and a structured profile model still serves the prose one until an operator flips the flag per deployment. As in Phase 3, this is implementation-complete, not production-enabled: Phase 0 still owns the eligible-observation input switch (`fact_types=[observation]`, `tags=[profile_eligible]`, `tags_match=exact`, represented as desired configuration only) and the production correction-refresh decision.

**Purpose:** Convert durable user and project synthesis from prose blobs into bounded, typed profile items.

**Detailed plan must cover:**

- one response schema per user/project profile, with one semantic claim per item;
- item budgets, ordering and displacement rules;
- compiler input from structured profile fields plus deterministic metadata;
- refresh-cost measurement and rollout strategy;
- profile/current-history separation and correction-driven refresh.

**Gate:** Delivered profiles satisfy their item budgets, omit inferred and bare technical claims, retain high-impact gotchas with cause/provenance, and improve curated behavior cases without increasing noise.

**Out of scope:** Multiple specialized models unless measured cadence, budget or retrieval needs justify them.

## Phase 5 — Truly Read-Only Recall

**Purpose:** Make deep history and rationale safely available without the current possibility that a read resolves or creates project state.

**Detailed plan must cover:**

- a distinct read-only API/tool contract and routing that cannot mint banks, models or project identities;
- evidence/history lookup filters and cautious reflect-style Q&A only where it adds value;
- tool annotations, host confirmation behavior and authorization regression tests;
- migration/compatibility for the existing recall surface.

**Gate:** A read against absent or unauthorized project context creates no state, while eligible history and rationale remain retrievable.

## Phase 0 — Production Hygiene (Executed Last)

**Purpose:** Clean the known live-data defects only after the mechanisms that make their effects measurable are in place.

**Requires separate explicit approval:** This phase mutates live Hindsight data. Execute one numbered operation at a time and record its evidence in the SPEC/results document.

**Operations:**

1. Diff and retire the slug-only duplicate project bank, re-retaining only reviewed unique material.
2. Soft-invalidate the known colour/canary user-bank rows.
3. Delete the accidental `squall` brief model and prove a subsequent GET does not recreate it.
4. Invalidate the stale disk-cache aspiration, refresh the model and record whether delta refresh removes it.
5. Audit high `proof_count` rows by distinct sessions and human utterances.
6. Use the invalidation/refresh result to decide whether to enable Phase 3's observation-only profile-input switch.

**Gate:** Every action is reversible where the engine permits it, counts and sources are recorded, the lifecycle probe has an explicit result, and no profile-input switch is enabled without that result.

## Cross-Phase Rules

- Treat the versioned v1.4 SPEC as authoritative; the root `SPEC-memory-quality.md` is historical until separately adopted.
- Each detailed phase plan must name exact files, interfaces, migrations, tests, rollout conditions and rollback behavior.
- No phase starts on a red targeted suite. Run the phase-specific tests first; integration tests requiring unavailable secrets/services are reported separately, never silently waived.
- Keep production-data operations out of code commits and out of automated test fixtures.
- Preserve the separation between host-commanded policy, deterministic project metadata, durable learned profiles and ephemeral Working State.
- Reassess the next phase only after the prior gate has evidence, not merely code coverage.
