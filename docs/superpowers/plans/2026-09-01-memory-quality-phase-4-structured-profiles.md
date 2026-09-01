# Memory Quality Phase 4 Structured Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace delivered user/project prose blobs with one bounded, typed, current-truth profile per durable bank kind, while preserving legacy delivery as the default until structured quality and nightly refresh cost are measured.

**Architecture:** Hindsight remains the durable synthesis engine. Each user bank or project bank gets one target `ach-memory-profile-v1` mental model with a scope-specific JSON response schema; the temporary coexistence with `ach-memory-brief-v1` is migration shadowing, not a split into specialized models. ach-memory reads `reflect_response.structured_output`, validates every item against the Phase 3 durability matrix, derives ordering and displacement without accepting an agent-supplied importance score, and renders only normalized structured fields. Project orientation continues to come from PostgreSQL Project Metadata and Working State remains a separate PostgreSQL section. A `legacy|structured` delivery mode defaults to `legacy`; an internal CLI dry-run evaluator measures quality, tokens and duration without exposing a new HTTP dry-run surface or mutating memory. Production provisioning, structured cutover, observation-only inputs, correction refresh and automatic capture all remain separately disabled pending explicit rollout approval and the final Phase 0 lifecycle probe.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Hindsight API 0.9.1 structured mental models, FastMCP, pytest/respx, Docker Compose and Helm.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (§4.2, §5, §6.4–§6.6, §8.4–§8.5, §10–§13, §17 Phase 4); sequencing, deferrals and gate: `docs/plans/2026-08-29-memory-quality-program.md` Phase 4.

## Review prerequisite from Phase 3

Phase 3 is integrated through `5a4da0b`. The focused delivery gate passes (`253 passed`), Ruff is clean, Alembic has the single head `d4e5f6a7b8c9`, and the repository suite reaches `1420 passed, 2 skipped`; its remaining failure/error are the known live-integration prerequisites (no reachable Hindsight and no `MEMORY_MASTER_KEY`).

That green gate bypasses one real hook/API seam and does not prove several privacy and crash boundaries. Task 0 closes these findings before Phase 4 consumes captured evidence:

1. the shipped hook omits required `project_slug`, while the E2E test adds it manually;
2. `observed preference` and `observed decision` are incorrectly marked profile-eligible;
3. transcript rotation submits a stale start offset, and whole-slice capping advances beyond records that were never sent;
4. shell-based file reads and credential-bearing git locators can cross the network;
5. terminal failed/not-found Hindsight operations are treated as pending forever, and one row failure can terminate the worker loop;
6. leases are not owner/generation fenced and can expire during external calls, allowing a stale worker to overwrite a newer transition;
7. explicit retain accepts caller-owned classification metadata instead of stamping explicit-request provenance;
8. the extractor mission and validators do not pin proposal/authorization, correction scope, project phrasing, negation and provenance-span rules;
9. the shell hook and Compose worker do not both remain cheap and off by default, and retry telemetry does not match the planned contract.

No structured-profile implementation starts until the Task 0 hook-client-to-FastAPI regression passes.

## Non-negotiable contracts

- Phase 3 remains the only semantic extraction path. Phase 4 synthesizes already classified durable evidence; it never reclassifies raw transcripts.
- One target mental model exists per durable bank kind: one user schema for a user bank and one project schema for a project bank. Temporary legacy/structured coexistence is removed after cutover evidence, not expanded into category-specific models.
- The response schema contains no `importance`, `priority`, `bank`, `profile_eligible`, score or free-form metadata field. Ordering is code-derived.
- One JSON item represents one semantic claim. Cause and provenance may explain that claim, but may not carry additional prescriptions or facts.
- The exact Phase 3 durability matrix is enforced again at the profile boundary. `inferred`, `technical_claim`, observed preference and observed decision never reach a durable profile.
- A gotcha is deliverable only with a failure claim, cause or reproducible condition, and bounded provenance.
- User Profile has at most 15 delivered items. Project Profile has at most 25. Oversubscribed synthesis is deterministically ranked and truncated; a higher-ranked new item displaces a lower-ranked item.
- Ordering is derived from semantic kind/risk, distinct evidence references, and a stable claim fingerprint. Repetition inside one source does not increase support.
- Project orientation (`name`, `locator`, `canonical_spec`, `purpose`) is always compiled from Project Metadata, never copied from learned profile output.
- Working State is never accepted inside a profile schema and remains separately compiled with visible age/source.
- Profiles represent current truth. Superseded material stays in Hindsight evidence/history and is not rendered as narrative history unless a concise negative warning prevents a likely mistake.
- Reads remain side-effect free. A GET does not create/reconcile/refresh a model, persist a snapshot or alter project identity.
- Invalid, missing, placeholder or stale structured output fails closed for that section. Structured mode does not silently serve a legacy prose item that may have been superseded.
- `MEMORY_PROFILE_DELIVERY_MODE=legacy` is the default everywhere. No Phase 4 route, deployment or test changes that default.
- Phase 4 does not apply bank configuration, provision production models, enable capture/worker/correction refresh, or switch final model inputs to `fact_types=[observation]`, `tags=[profile_eligible]`, `tags_match=exact`.
- The final observation-only trigger is represented and tested as desired configuration only. It is not applied until Phase 0 verifies invalidation and delta refresh end-to-end.
- No ach-memory HTTP `dry-run-refresh` endpoint is added. Cost/quality evaluation is an explicit local/admin CLI operation against an already provisioned model and Hindsight's non-persisting upstream preview.
- Metrics, evaluator JSON and logs contain counts, timings, modes and error codes only: no claim text, source IDs, bank IDs, user IDs or project slugs.

## Structured contracts

Each category is an array of the same bounded item type. Category membership is structural; the model cannot invent categories.

```json
{
  "claim": "Run the focused tests before the full suite.",
  "kind": "convention",
  "origin": "confirmed",
  "negative": false,
  "failure": null,
  "cause": null,
  "reproduction": null,
  "provenance": "Accepted workflow convention.",
  "evidence_ids": ["<hindsight-memory-id>"]
}
```

Fields are closed (`additionalProperties=false`) and bounded:

- `claim`: one non-empty line, maximum 320 characters;
- `kind`: `preference|decision|convention|gotcha` only;
- `origin`: `stated|confirmed|observed` only;
- `negative`: boolean, used only to preserve an explicit negative constraint;
- `failure`, `cause`, `reproduction`: nullable single lines, maximum 240 characters each; `gotcha` requires `failure` and at least one of `cause|reproduction`, non-gotchas require all three null;
- `provenance`: nullable single line, maximum 240 characters; required for a gotcha and omitted from normal rendering unless it changes behavior;
- `evidence_ids`: 1–8 unique IDs, each required to occur in `reflect_response.based_on.memories`; the compiler uses only the distinct validated references as support, never a model-supplied count.

User response schema:

```yaml
user_profile:
  interaction: []
  engineering: []
  preferences: []
  constraints: []
```

Project response schema:

```yaml
project_profile:
  architecture: []
  decisions: []
  workflow: []
  testing: []
  conventions: []
  gotchas: []
```

Every category has `maxItems` equal to the profile's total budget so the upstream response is bounded. A post-schema normalization pass enforces the total 15/25 budget across categories.

## Eligibility, ordering and displacement

Validation repeats the exact durability table instead of trusting synthesis:

```text
preference  stated|confirmed       -> eligible
decision    stated|confirmed       -> eligible
convention  stated|confirmed|observed -> eligible
gotcha      stated|confirmed|observed -> eligible when structurally valid
everything else                    -> reject from active profile
```

Each accepted item receives deterministic compiler metadata, not serialized back into Hindsight:

```text
category
kind_rank
support_count = count(unique validated evidence_ids)
claim_key     = sha256(normalized category + claim + negative flag)
```

Ranking is ascending by `kind_rank`, descending by `support_count`, then ascending by `claim_key`:

```text
0  valid gotcha or explicit negative constraint
1  decision
2  convention
3  stated/confirmed preference
```

Exact duplicate `claim_key` values collapse to one item with the union of valid evidence references. At 15/25 items, the sorted prefix is the active profile; any new item enters only by displacing the current last item. Array order from the model, timestamps, LLM wording order and raw `proof_count` never decide delivery.

Semantic packing cannot be proven by JSON Schema alone. The harness therefore combines closed one-line item structure, the Phase 3 one-claim evidence invariant, rejection of list/multiline payloads, and curated adversarial behavior cases where two independent prescriptions in one item must be split or rejected. Do not add a brittle blanket ban on words such as `and`, because a valid gotcha cause can contain them.

## Hindsight target and safe rollout

`ach-memory-profile-v1` is provisioned explicitly with:

```yaml
name: ach-memory-profile-v1
max_tokens: <scope-specific bounded value>
tags: []
trigger:
  mode: full
  refresh_after_consolidation: false
  response_schema: <user or project JSON Schema>
  keep_trace: true
```

During Phase 4, no cron and no final observation/tag filter are installed. The source query requires current durable truth, one claim per item, exact eligibility, correction supersession, bounded evidence IDs and the relevant scope schema. Post-validation is still authoritative.

The desired Phase 0 trigger is represented only in a pure configuration builder/test:

```yaml
mode: delta
fact_types: [observation]
tags: [profile_eligible]
tags_match: exact
refresh_after_consolidation: false
response_schema: <scope schema>
keep_trace: true
```

Rollout order:

1. land code with `legacy` delivery and all Phase 3 production flags false;
2. provision only local/staging fixture banks through the existing master-only explicit admin boundary;
3. run the non-persisting profile evaluator nightly for at least seven representative runs per scope;
4. require schema validity, budget conformance, curated behavior improvement and recorded token/duration ceilings before enabling `structured` in non-production;
5. run the complete delivery gate in structured mode;
6. leave production on `legacy` until explicit approval; final eligible-observation input and correction refresh still wait for Phase 0;
7. after approved cutover evidence, retire `ach-memory-brief-v1` in a separately authorized cleanup instead of maintaining two permanent models.

## File map

- `src/memory/capture/contracts.py`, `src/memory/capture/local.py`, `src/memory/api/capture.py`: canonical hook submission and lossless bounded slicing.
- `src/memory/capture/classifier.py`, `src/memory/capture/extractor.py`, `src/memory/provenance.py`, `src/memory/api/memory.py`: Phase 3 semantic/provenance corrections.
- `src/memory/capture/repository.py`, `src/memory/capture/worker.py`, `src/memory/capture/filer.py`: owner-fenced leases and terminal-operation handling.
- `plugins/claude-code/scripts/capture-checkpoint.sh`, `docker-compose.yml`, `src/memory/metrics.py`, `src/memory/activity.py`: Phase 3 activation and telemetry corrections.
- Create `src/memory/profiles.py`: scope schemas, source queries, structured parsing, eligibility, deterministic ranking/displacement and rendering inputs.
- `src/memory/hindsight/paths.py`, `src/memory/hindsight/client.py`: trusted mental-model tags/response schema and internal dry-run-refresh client support.
- `src/memory/brief.py`, `src/memory/api/brief.py`: structured section loading, revisions and Index/Full compilation.
- `src/memory/api/admin.py`: explicit structured profile provisioning/reconciliation; no read-path mutation.
- `src/memory/config.py`, `src/memory/cli.py`: default-legacy delivery mode and content-free profile evaluator.
- `deploy/helm/ach-memory/values.yaml`, `deploy/helm/ach-memory/templates/deployment.yaml`, `docker-compose.yml`, `.env.example`: disabled/default-legacy deployment wiring.
- Create `tests/test_phase3_review_closure.py`: every Phase 3 review regression, including the actual hook-client-to-FastAPI seam.
- Create `tests/test_profiles.py`: response schemas, validators, ordering and displacement.
- Create `tests/test_profile_provisioning.py`: trusted Hindsight payloads, explicit provisioning and desired-but-unapplied Phase 0 trigger.
- Create `tests/test_profile_evaluation.py`: dry-run cost/quality output and no public route.
- Modify `tests/test_brief.py`, `tests/test_session_brief_api.py`: typed compiler, revisions, budgets and read-only behavior.
- Create `tests/test_profile_e2e.py`: curated structured-profile delivery gate.

---

### Task 0: Close the Phase 3 review findings

**Files:**

- Modify: `src/memory/capture/contracts.py`
- Modify: `src/memory/capture/local.py`
- Modify: `src/memory/api/capture.py`
- Modify: `src/memory/capture/classifier.py`
- Modify: `src/memory/capture/extractor.py`
- Modify: `src/memory/capture/filer.py`
- Modify: `src/memory/capture/repository.py`
- Modify: `src/memory/capture/worker.py`
- Modify: `src/memory/provenance.py`
- Modify: `src/memory/api/memory.py`
- Modify: `plugins/claude-code/scripts/capture-checkpoint.sh`
- Modify: `docker-compose.yml`
- Modify: `src/memory/metrics.py`
- Modify: `src/memory/activity.py`
- Create: `tests/test_phase3_review_closure.py`
- Modify: `tests/test_capture_local.py`
- Modify: `tests/test_capture_classifier.py`
- Modify: `tests/test_capture_worker.py`
- Modify: `tests/test_capture_extractor.py`
- Modify: `tests/test_capture_e2e.py`

- [ ] **Step 1: Pin the real hook/API seam before changing code**

  Add an integration test that invokes the same local `_checkpoint()` entry used by the Claude hook, captures its serialized `CheckpointSubmission`, sends that exact body through FastAPI with a real project/workspace/session boundary, and observes `202`. Remove the E2E's manual `project_slug` repair. Also assert that `CheckpointSubmission` and `CheckpointRequest` share the same required constrained fields.

- [ ] **Step 2: Resolve one canonical local project context**

  Canonicalize the raw git origin with `slugs.canonical_locator()` before it is used in a cursor key or wire body; derive the slug with `slugs.slug_from_locator()` through the same project-context helper used by SessionStart/MCP; allow an explicit validated hook/project override only when supplied by the host contract. Make `project_slug` required in the local wire model. Add credentials-in-origin and spelling-variant tests proving no userinfo crosses the network and the same repository maps to the same slug/cursor.

- [ ] **Step 3: Make transcript rotation and caps lossless**

  Return `effective_start_offset` from `read_new_slice()` after truncation/rotation. Batch only complete records whose sanitized representation fits the slice cap, preserve code-owned `[raw_start, raw_end)` markers for every included sanitized record, and return the raw end offset of the last included record. Hash, submit, acknowledge and advance exactly that bounded raw batch; leave remaining complete records for the next checkpoint. Empty sanitized batches may advance only across records structurally proven to contain no retained text, with a regression that later meaningful records remain reachable. Test rotation followed by successful submission and at least two batches preserving a decision in the tail.

- [ ] **Step 4: Close local privacy gaps**

  Classify tool uses before retaining tool results. Drop output for direct file-read tools and shell commands whose parsed executable/arguments are file-body readers (`cat`, `sed`, `awk`, `grep`/`rg` with file operands, `head`, `tail`, equivalent wrappers); fail closed when classification is ambiguous. Keep exit/error summaries, never command input or file bodies. Add synthetic canaries for Bash/file reads, URL userinfo, token patterns and multi-block outputs; assert no canary occurs in the submitted body, cursor key or diagnostic log.

- [ ] **Step 5: Enforce the exact durability/provenance contract**

  Replace `_eligibility()` with the literal §6.4 matrix and correct the existing observed-preference/decision tests. Reserve `origin`, `kind`, `negative`, `correction`, `provenance`, session/checkpoint/slice fields and eligibility names from caller metadata. Explicit retain stamps server-owned `origin=stated`, `kind=technical_claim` (evidence candidate), `explicit_request=true` and bounded request provenance; it never lets caller metadata promote itself. Expand the extractor mission for proposals/authorization, scope-fenced corrections, impersonal project claims and explicit negation. Validate transcript provenance spans against the submitted slice and require `negative=true` to match an explicit negative claim.

- [ ] **Step 6: Fence leases and terminal operation states**

  Add compare-and-set transitions on `(capture_id, lease_owner)` or a monotonically increasing lease generation for `advance_stage`, `release_lease`, `record_failure` and `complete`. Lease one row at a time or renew before/after any external call so a 180-second extraction cannot outlive an unprotected 60-second lease. Distinguish `pending|running` from terminal `failed|not_found`; terminal states record bounded retry/backoff and eventually remain failed. Catch typed domain/not-found errors as row failures, and isolate each row in `run_forever` so one malformed/failed row cannot stop the process. Add two-worker stale-owner, lease-expiry, terminal-operation and poison-row-followed-by-good-row tests.

- [ ] **Step 7: Align default activation and telemetry**

  Gate `capture-checkpoint.sh` on `MEMORY_CAPTURE_ENABLED` before invoking `uvx`. Keep Compose's worker flag false unless the operator explicitly sets it true, even when the capture profile is selected. Add the promised content-free retry counter and reduce acceptance activity to host/status/duplicate/byte bucket; remove exact slug/bank fingerprints. Make display redaction replace embedded bank IDs, not only exact whole-field matches.

- [ ] **Step 8: Run the repaired Phase 3 gate**

  Run:

  ```bash
  uv run pytest tests/test_phase3_review_closure.py tests/test_capture_local.py tests/test_capture_api.py tests/test_capture_repository.py tests/test_capture_classifier.py tests/test_capture_extractor.py tests/test_capture_filer.py tests/test_capture_worker.py tests/test_capture_e2e.py tests/test_agent_bundle.py tests/test_mcp_surface_honesty.py -q
  uv run ruff check src tests
  alembic heads
  ```

  Stop if the exact local hook body cannot traverse FastAPI without test-only repair.

---

### Task 1: Define the closed user and project response schemas

**Files:**

- Create: `src/memory/profiles.py`
- Create: `tests/test_profiles.py`

- [ ] **Step 1: Write failing schema-shape tests**

  Assert the exact user/project category keys, `additionalProperties=false` at every object level, bounded one-line strings, allowed enums, unique bounded `evidence_ids`, per-category limits and absence of importance/bank/eligibility/current-work fields.

- [ ] **Step 2: Implement Pydantic item and document models**

  Define frozen `ProfileItem`, `UserProfileDocument` and `ProjectProfileDocument` models plus `user_response_schema()` / `project_response_schema()`. Keep the root wrapper (`user_profile` or `project_profile`) in the generated JSON Schema sent to Hindsight.

- [ ] **Step 3: Add cross-field validation**

  Enforce the durability matrix, gotcha requirements, non-gotcha null gotcha fields, unique evidence references, explicit-negative preservation, category/kind compatibility, no Working State/project metadata fields and no exact duplicates inside a response.

- [ ] **Step 4: Verify schema serialization is stable**

  Snapshot canonical JSON schema hashes in tests so an accidental Pydantic/config change cannot silently alter the model contract or revisions.

---

### Task 2: Extend the trusted Hindsight boundary without widening public control

**Files:**

- Modify: `src/memory/hindsight/paths.py`
- Modify: `src/memory/hindsight/client.py`
- Modify: `tests/test_hindsight_client.py`
- Modify: `tests/test_mental_models_api.py`

- [ ] **Step 1: Pin exact upstream payloads with respx**

  Add failing tests for trusted `tags`, trigger `response_schema`/`keep_trace`, full-detail `reflect_response.structured_output`, and `POST .../dry-run-refresh`. Assert `dry-run-refresh` has no request body and returns usage/duration/diff metadata.

- [ ] **Step 2: Add trusted client fields**

  Extend `create_mental_model` and `update_mental_model` to accept explicit server-owned tags and pass the full trigger verbatim. Add a path/client method for upstream dry-run refresh with traversal rejection and the LLM timeout.

- [ ] **Step 3: Preserve the public boundary**

  Do not add tags to caller-controlled mental-model request models and do not add a FastAPI/MCP dry-run-refresh route. Keep and strengthen `test_no_dry_run_refresh_surface_exists` by inspecting OpenAPI and MCP tools.

---

### Task 3: Provision exactly one structured target model per bank kind

**Files:**

- Modify: `src/memory/profiles.py`
- Modify: `src/memory/api/admin.py`
- Create: `tests/test_profile_provisioning.py`

- [ ] **Step 1: Write explicit-provisioning and read-safety tests**

  Test create, idempotent reconcile, wrong-scope schema correction, preservation of fields this module does not own, no refresh, and no model creation from any GET. Require master auth and `create=False` bank resolution.

- [ ] **Step 2: Implement scope-specific source queries and safe Phase 4 triggers**

  Build fixed user/project synthesis missions covering current truth, exact durability, correction supersession, atomic claims, gotcha detail and evidence references. Build the response schema into a non-automatic full trigger. No final tags/fact types are applied.

- [ ] **Step 3: Add explicit admin provisioning**

  Add `POST /v1/admin/profile/{scope}/provision` (or a backwards-compatible explicit format selector on the existing provision route) that creates/reconciles only `ach-memory-profile-v1`. Return only `created|reconciled`, scope and public project forwarding metadata.

- [ ] **Step 4: Represent, but do not apply, the Phase 0 target**

  Add a pure `desired_phase0_trigger(scope)` builder and tests for observation-only exact eligible input. No application call, deployment default or admin action in this phase may select it.

---

### Task 4: Normalize, rank and displace structured items deterministically

**Files:**

- Modify: `src/memory/profiles.py`
- Modify: `tests/test_profiles.py`

- [ ] **Step 1: Write failing normalization cases**

  Cover all matrix combinations, invalid evidence IDs, duplicate references, duplicate claims, 16th/26th items, stable ordering under permuted upstream arrays, support-count ties, a high-risk gotcha displacing a weak preference/convention, negative constraint preservation and superseded-history exclusion.

- [ ] **Step 2: Validate grounding against `based_on`**

  Extract the set of Hindsight memory IDs from `reflect_response.based_on.memories`; reject item references outside it. Do not accept `proof_count` or a supplied source count. Deduplicate references before support calculation.

- [ ] **Step 3: Implement canonical keys and ordering**

  Normalize whitespace without rephrasing, derive `claim_key`, `kind_rank` and `support_count`, merge exact duplicates, sort with the declared tuple and take the 15/25 prefix. Preserve category so later rendering is deterministic.

- [ ] **Step 4: Add curated atomicity cases**

  Exercise packed independent claims, valid cause clauses, list injection, multiline content, cheap technical facts, inferred preferences and current-task material. Structural violations fail closed; semantic model failures make the evaluator gate fail rather than adding a second LLM/classifier.

---

### Task 5: Compile Index and Full from typed profile fields

**Files:**

- Modify: `src/memory/brief.py`
- Modify: `src/memory/api/brief.py`
- Modify: `tests/test_brief.py`
- Modify: `tests/test_session_brief_api.py`

- [ ] **Step 1: Pin structured delivery and failure behavior**

  Add tests for user-only/project-only/both scopes, absent/invalid/stale output, no legacy fallback in structured mode, inert rendering, stable revisions, Project Metadata separation, Working State separation, renamed project forwarding and delegated master reads.

- [ ] **Step 2: Add a typed section loader**

  Find `ach-memory-profile-v1` with `detail=full`, parse only `reflect_response.structured_output`, validate/normalize it and return a typed section plus model refresh timestamp/schema/content fingerprint. Never use the markdown `content` field as structured fallback.

- [ ] **Step 3: Render deterministic category lines**

  Render claims from typed fields only. Add cause/reproduction to gotcha lines in a bounded form and keep provenance only where it prevents misuse. Pass every upstream string through inert rendering. Project orientation remains a separate mandatory compiler block.

- [ ] **Step 4: Integrate the delivery mode**

  Add `MEMORY_PROFILE_DELIVERY_MODE=legacy|structured`, default `legacy`, and branch only at section loading. Preserve the existing host budgets/allocator; structured mode must still reserve mandatory orientation and Working State metadata before optional profile items.

- [ ] **Step 5: Make revisions reflect normalized truth**

  Fingerprint normalized structured items and schema version, not upstream array order or markdown. A permutation with the same normalized profile keeps the revision; a displacement/correction changes it and invalidates the appropriate workspace cache only.

---

### Task 6: Target correction refresh at the active canonical profile only

**Files:**

- Modify: `src/memory/capture/worker.py`
- Modify: `src/memory/profiles.py`
- Modify: `tests/test_capture_worker.py`
- Modify: `tests/test_profiles.py`

- [ ] **Step 1: Replace refresh-all tests**

  Assert a user correction cannot refresh a project bank, a project correction cannot refresh a user bank, legacy mode targets only `ach-memory-brief-v1`, structured mode targets only `ach-memory-profile-v1`, missing target is a bounded no-op/error policy, and unrelated operator-created models are never refreshed.

- [ ] **Step 2: Select by fixed model name and resolved bank**

  Reuse the owner-fenced worker transition from Task 0. List full models once, choose the fixed active model name, request refresh, and complete the capture only after the upstream refresh call succeeds. Retries may repeat the same refresh but cannot widen scope or regress queue state.

- [ ] **Step 3: Keep both switches off**

  `MEMORY_CAPTURE_CORRECTION_REFRESH_ENABLED=false` and `MEMORY_PROFILE_DELIVERY_MODE=legacy` remain deployment defaults. Tests may enable combinations locally; Phase 4 does not authorize production activation.

---

### Task 7: Measure refresh cost and consuming-agent quality without mutation

**Files:**

- Modify: `src/memory/cli.py`
- Modify: `src/memory/profiles.py`
- Modify: `src/memory/metrics.py`
- Create: `tests/test_profile_evaluation.py`
- Modify: `deploy/helm/ach-memory/values.yaml`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

- [ ] **Step 1: Define a content-free evaluation result**

  Include scope, requested/effective mode, outcome, would-persist, retrieved/used fact counts, input/output/total tokens, duration, schema-valid flag, candidate/delivered item counts, displacement count, curated-case pass/fail counts and warnings codes. Exclude all content and identifiers.

- [ ] **Step 2: Add `ach-memory profile-check`**

  Resolve an existing bank/model without creation, call upstream dry-run refresh, parse the configured structured document from `preview_content` as JSON, validate it through the same normalizer used for persisted `reflect_response.structured_output`, and emit human or `--json` output. The command is explicit, returns nonzero on schema/quality/budget failure, and never calls provision/update/refresh/retain.

- [ ] **Step 3: Prove no mutation and no public surface**

  With respx, assert only list/get and upstream dry-run-preview calls occur; no PATCH/POST refresh or ach-memory API route exists. Assert logs/metrics/output contain no synthetic claim, source, bank or slug canaries.

- [ ] **Step 4: Wire an opt-in nightly evaluator, disabled by default**

  Provide a Helm/Compose command/profile template that an operator can point only at explicitly selected staging banks. Its enable flag defaults false and no target list ships. Document the seven-run minimum and required token/duration/quality report before any structured cutover.

---

### Task 8: Prove the Phase 4 delivery gate end to end

**Files:**

- Create: `tests/test_profile_e2e.py`
- Modify: `docs/plans/2026-08-29-memory-quality-program.md`
- Modify: `.env.example`

- [ ] **Step 1: Build curated current-truth fixtures**

  Include stated/confirmed preferences, observed preference/decision exclusions, observed convention, valid/invalid gotchas, technical claims, inferred claims, duplicate sessions, correction supersession, negative constraints, project metadata and Working State. Use only synthetic content.

- [ ] **Step 2: Exercise the real structured boundary**

  Feed full-detail Hindsight model responses containing `structured_output` and `based_on`, compile `/v1/session-brief` in structured mode, and assert the actual Index/Full payloads seen by the consumer—not just intermediate Python objects.

- [ ] **Step 3: Assert the complete gate**

  Require:

  - no more than 15 user and 25 project active items;
  - inferred, observed preference/decision and bare technical claims absent;
  - a high-impact gotcha retains failure plus cause/reproduction and bounded provenance;
  - a higher-ranked new item displaces a lower-ranked existing item;
  - Project Metadata and Working State are separate and mandatory fields survive host allocation;
  - correction removes old current truth without deleting evidence/history;
  - structured revisions/caches are workspace and credential-owner isolated;
  - curated behavior cases improve or remain equal to legacy with no added noise;
  - all production activation flags and profile delivery remain off/legacy by default.

- [ ] **Step 4: Run verification**

  Run:

  ```bash
  uv run pytest tests/test_phase3_review_closure.py tests/test_profiles.py tests/test_profile_provisioning.py tests/test_profile_evaluation.py tests/test_profile_e2e.py tests/test_brief.py tests/test_session_brief_api.py tests/test_capture_worker.py tests/test_mcp_surface_honesty.py -q
  uv run pytest -q
  uv run ruff check src tests
  git diff --check
  alembic heads
  ```

  The full suite may skip/fail only the already documented live Hindsight/master-key integration prerequisites when those services/secrets are absent. Record the exact counts and environment reason; do not relabel a product-test failure as an integration prerequisite.

- [ ] **Step 5: Record the rollout boundary**

  Update the HLD with the Phase 3 closure commit, Phase 4 gate counts, schema hashes and nightly evaluation summary. State explicitly that production memory/config was not mutated, `MEMORY_PROFILE_DELIVERY_MODE` remains `legacy`, and Phase 0 still owns the eligible-observation input and production correction-refresh decision.

## Completion criteria

Phase 4 is complete only when:

- Task 0 proves the real Claude hook body reaches FastAPI and no transcript tail/privacy/lease defect remains;
- one closed response schema exists for each profile scope and no specialized split is added;
- normalized delivered profiles obey 15/25 budgets and deterministic displacement;
- delivered context omits every ineligible matrix combination and bare technical claim;
- curated gotchas retain actionable cause/reproduction and provenance;
- Index/Full compile from structured fields while Project Metadata and Working State stay separate;
- dry-run evaluation reports content-free cost/quality and proves no mutation/new public surface;
- focused tests, Ruff, diff check and Alembic head pass;
- production remains on legacy delivery with capture, worker, correction refresh and final profile-input activation off.

## Explicit deferrals

- Production bank/model provisioning or configuration PATCH.
- Production structured-profile cutover.
- `fact_types=[observation]`, `tags=[profile_eligible]`, `tags_match=exact` activation.
- Production correction-triggered refresh enablement.
- Multiple category-specific mental models.
- Stable explainability IDs/UI beyond the bounded evidence references required for validation.
- Dynamic workspace-aware profile relevance or another storage layer.
- Automated lifecycle cleanup/invalidation (Phase 0).
- Truly read-only long-tail recall (Phase 5).
