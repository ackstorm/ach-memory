# Memory Quality Phase 5 Truly Read-Only Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make deep evidence, history and rationale safely retrievable without any read path minting or enriching a user, project, bank, model or other domain identity.

**Architecture:** Add one side-effect-free bank resolver for read surfaces and route semantic recall, exact memory history and compatible legacy recall through it. The resolver accepts only an existing user or existing project slug, authorizes before touching Hindsight, never forwards repository metadata into mutating project resolution, and performs no domain commit. Recall returns a closed, bounded result rather than an arbitrary upstream payload; a returned memory ID can be used by a separate history lookup in the same authorized bank. The MCP `recall` name remains compatible but becomes genuinely read-only and may advertise `readOnlyHint`; a new `memory_history` tool exposes rationale/history. Reflect-style Q&A is not registered by default and lands only if curated behavior measurement proves it adds value over grounded recall despite its LLM cost.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Hindsight API 0.9.1 recall and observation-history APIs, FastMCP, pytest/respx.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (§5, §9, §11, §13, §14, §17 Phase 5); sequencing and gate: `docs/plans/2026-08-29-memory-quality-program.md` Phase 5.

## Review prerequisite from Phase 4

Phase 4 is integrated at `d13a395`. Its reported full suite reaches `1946 passed, 2 skipped`, with only the two documented live Hindsight/master-key prerequisites remaining, and production delivery still defaults to `legacy`.

A final independent review reproduced four Task 5 contract gaps against that exact HEAD. Task 0 closes them before Phase 5 adds another consumer path:

1. valid maximum structured lines can leave both optional profiles present while dropping mandatory Working State in Index and Full;
2. `is_stale=true` structured output is served for up to seven days, even though stale structured current truth must fail closed;
3. gotcha failure dedup uses substring containment, so `Install` can suppress `Stall` and a negated mention can suppress the actual failure;
4. revision fingerprints hash rendered text plus a render version, not canonical normalized typed truth plus schema version, so `stated -> confirmed` and evidence changes can keep a stale revision.

These are Important, not architectural reversals. No Phase 5 recall work starts until their focused regressions pass.

## Non-negotiable contracts

- A read never creates or enriches a `User`, `Project`, retired slug, bank mapping, mental model, Working State, capture row or Hindsight memory.
- The only tolerated writes around a read are content-free operational activity and mandatory delegated-master audit, isolated from the domain transaction. Gate tests compare all domain tables and every upstream mutating method before/after.
- Project read identity is `project_slug`. `git_locator` is not accepted by the new read contract, so a read cannot bind or repair repository metadata. The stdio proxy may derive/inject the slug locally before the call.
- Missing project, retired/forwarded slug, unauthorized project and locator-only context have distinct typed outcomes where authorization permits disclosure, but none creates state.
- All Hindsight calls occur only after local authorization and use the already stored bank ID. A caller never supplies or receives a bank ID.
- Semantic recall is bounded: query length, fact types, tag filters, result count and returned payload all have explicit limits. No raw chunks, embeddings, entities, tool traces or arbitrary upstream metadata are returned.
- Default recall returns grounded hits, not synthesized prose. Each hit carries a scoped memory ID so history can be requested explicitly.
- History lookup is separately scoped and authorizes the bank before dereferencing the memory ID. An ID valid in another bank is indistinguishable from not found.
- Current truth and history remain separate: active observations are preferred for current recall; invalidated/superseded evidence is available only when explicitly requested through evidence/history filters.
- `profile_eligible` / `evidence_only` and `kind:<kind>` filters are server-constructed from enums. Callers cannot submit arbitrary Hindsight tag expressions.
- The existing `recall` REST/MCP contract remains available during migration but delegates to the new read-only implementation and loses its legacy project-creation side effect.
- `recall` and `memory_history` may advertise `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false` only after no-state tests prove the annotation honest.
- Reflect-style Q&A keeps host confirmation and a write-class rate limit because it spends an LLM call, even though its resolver is domain-read-only. It is not registered or advertised unless behavior evaluation justifies it.
- No new `dry-run-refresh` route, production memory mutation, profile cutover, eligible-observation activation or lifecycle cleanup is authorized by Phase 5.

## Read-only public contracts

### Semantic recall

```http
POST /v1/read/recall
Authorization: Bearer <credential>

{
  "scope": "user|project",
  "user_id": null,
  "project_slug": "ach-memory",
  "query": "Why is capture disabled by default?",
  "view": "current|evidence|all",
  "kinds": ["decision", "gotcha"],
  "max_results": 10
}
```

`user_id` is accepted only for master-key delegated reads. Project scope requires `project_slug` and rejects `git_locator` as an unknown field.

```json
{
  "project_slug": "ach-memory",
  "resolved_from": null,
  "hits": [
    {
      "memory_id": "<opaque Hindsight memory id>",
      "text": "Production capture remains disabled until the lifecycle gate passes.",
      "fact_type": "observation",
      "state": "valid",
      "kind": "decision",
      "origin": "confirmed",
      "eligibility": "profile_eligible",
      "occurred_at": "2026-09-01T10:00:00Z",
      "document_id": null
    }
  ],
  "truncated": false
}
```

The response is closed. Optional fields may be null; unknown upstream fields are discarded recursively.

`view` maps to server-owned upstream filters:

```text
current   -> types=[observation, world, experience], prefer_observations=true,
             valid/current results only
evidence  -> types=[world, experience], evidence_only eligible by default
all       -> types=[observation, world, experience], no eligibility default,
             still valid/current unless exact history is requested separately
```

Explicit `kinds` become a bounded OR over `kind:<kind>` tags. Eligibility is selected by `view`, not by a raw tag field.

### Exact history/rationale

```http
POST /v1/read/history

{
  "scope": "project",
  "project_slug": "ach-memory",
  "memory_id": "<id returned by recall>"
}
```

```json
{
  "project_slug": "ach-memory",
  "resolved_from": null,
  "memory_id": "<same id>",
  "current": {"text": "...", "state": "valid", "kind": "decision"},
  "changes": [
    {
      "text": "...",
      "valid_from": "...",
      "valid_to": null,
      "source_facts": [
        {"memory_id": "...", "text": "...", "origin": "confirmed"}
      ]
    }
  ],
  "truncated": false
}
```

History is capped by change count, sources per change and total payload. Whole entries are dropped when the cap is reached; text is never cut mid-claim.

## Compatibility and host behavior

- Existing `POST /v1/memory/recall` becomes a compatibility adapter over the new service. Its response keeps the legacy envelope for one release and includes standard deprecation/sunset/link headers.
- Existing MCP `recall(scope, query, project_slug, ...)` keeps its name and core arguments so agents/configs do not break. It calls the new read service and returns the existing compact shape where possible.
- `verbose` is deprecated. It no longer exposes arbitrary upstream fields; it may add only the closed provenance fields already allowed by the new response schema.
- Existing `reflect` also switches to the read-only resolver immediately, so it cannot mint projects while awaiting the Q&A measurement decision. It remains confirmation-requiring and rate-limited.
- The SessionStart affordance text changes from “host will ask to confirm recall” to an honest read-only callable statement only after MCP annotations and no-state tests land together.
- No duplicate `search_memory` alias is added: two names for the same retrieval behavior increase tool-selection noise without compatibility benefit.

## File map

- `src/memory/brief.py`, `src/memory/api/brief.py`, `src/memory/profiles.py`: Phase 4 closure for reservation, stale handling, gotcha rendering and revisions.
- Create `src/memory/read_context.py`: existing-only user/project bank resolution with no locator enrichment or commit.
- Create `src/memory/read_models.py`: closed recall/history request and response contracts, filters and payload caps.
- Create `src/memory/read_service.py`: authorized semantic recall, history normalization and optional Q&A evaluation boundary.
- Create `src/memory/api/read.py`; modify `src/memory/api/app.py`: new read-only routes.
- `src/memory/hindsight/paths.py`, `src/memory/hindsight/client.py`: typed recall filters and observation-history GET.
- `src/memory/api/memory.py`, `src/memory/api/curation.py`: compatibility adapters and non-enriching existing reads.
- `src/memory/mcp/tools.py`, `src/memory/mcp/compact.py`, `src/memory/mcp/proxy.py`: honest read annotations, closed compact results and compatibility.
- `src/memory/config.py`, `src/memory/metrics.py`, `src/memory/activity.py`: bounds, optional Q&A gate and content-free measurements.
- `plugins/claude-code/scripts/session-start.sh`, `plugins/claude-code/activation.txt`, `plugins/codex/scripts/session-start.sh`, `plugins/codex/activation.txt`, `README.md`: honest host affordance text after the safety gate.
- Create `tests/test_phase4_review_closure.py`.
- Create `tests/test_read_context.py`, `tests/test_read_api.py`, `tests/test_read_service.py`, `tests/test_read_mcp.py`, `tests/test_read_e2e.py`.
- Modify `tests/test_brief.py`, `tests/test_hindsight_client.py`, `tests/test_memory_api.py`, `tests/test_mcp_surface_honesty.py`, `tests/test_mcp_tools.py`, `tests/test_mcp_proxy.py`.

---

### Task 0: Close the Phase 4 final-review findings

**Files:**

- Modify: `src/memory/brief.py`
- Modify: `src/memory/api/brief.py`
- Modify: `src/memory/profiles.py`
- Create: `tests/test_phase4_review_closure.py`
- Modify: `tests/test_brief.py`

- [x] **Step 1: Pin all four failures before changing code**

  Add schema-valid maximum-line regressions proving Working State disappears from current Index and Full; a five-minute-old `is_stale=true` model that is currently served; divergent gotcha claim/failure pairs including `Install`/`Stall` and negated mentions; and two canonical profiles differing only in origin/evidence that currently share a fingerprint/revision.

  Done via `tests/test_phase4_review_closure.py`. Confirmed all seven finding-specific tests fail against pre-fix source (via `git stash` of only `src/memory/brief.py`/`src/memory/profiles.py`), then pass after Steps 2-5.

- [x] **Step 2: Reserve Working State before optional profile material**

  Keep emission order unchanged, but allocate in authority order: header/tail and whole Project Metadata orientation first, then an atomic Working State minimum, then profile floors and round-robin optional lines, then Working State optional detail. Index minimum is its one headline; Full minimum is objective plus age and source session. A profile may disappear to honor the host budget; Working State metadata may not.

  Added `_FLOOR_ORDER = ("working_state", "user", "project")` in `brief.py`; both `compose_index` and `compose_full` reserve floors in that order while `_FILL_ORDER`/emission order (`sections`) is unchanged. Empirically confirmed the pre-fix defect at `budget=1300` with near-maximum (320-char) user+project claims: both profiles present, Working State absent pre-fix; a profile yields to Working State post-fix.

- [x] **Step 3: Fail closed on any stale structured profile**

  `get_structured_section` returns `None` whenever `is_stale` is true, regardless of refresh age. Preserve the legacy prose grace-period policy only in `get_section`. Add a recent-correction regression showing stale prior current truth is not served.

- [x] **Step 4: Deduplicate gotcha failure only by normalized equality**

  Replace unrestricted containment with equality after casefolding, whitespace collapse and bounded terminal-punctuation normalization. Divergent, extended, negated and substring-only failures render; only the same sentence filed twice is suppressed. Bump `PROFILE_RENDER_VERSION` in the same commit.

  `PROFILE_RENDER_VERSION` bumped to `profile-v1-render-3`.

- [x] **Step 5: Fingerprint canonical typed truth and schema version**

  Serialize scope plus a stable profile-schema version/hash, render version, and every canonical compiled item field (`category`, claim, kind, origin, negative, gotcha detail, provenance, sorted evidence IDs, support count and stable key). Hash canonical JSON with sorted keys. Upstream array permutations remain stable; origin, category, evidence, displacement and rendering changes bump the revision.

  Added `profiles.PROFILE_SCHEMA_VERSION = "profile-schema-v1"`. `brief._content_fingerprint` now takes `(scope, items)` and hashes canonical sorted-key JSON instead of rendered text.

- [x] **Step 6: Run the closure gate**

  ```bash
  uv run pytest tests/test_phase4_review_closure.py tests/test_brief.py tests/test_profiles.py tests/test_profile_e2e.py -q
  uv run ruff check src tests
  git diff --check
  ```

  Result: `381 passed`; ruff clean; `git diff --check` clean. Grepped for other callers of `_content_fingerprint`: none outside `brief.py` itself.

---

### Task 1: Build one existing-only, non-enriching read resolver

**Files:**

- Create: `src/memory/read_context.py`
- Create: `tests/test_read_context.py`

- [x] **Step 1: Pin domain-state snapshots**

  For user, existing project, absent project, retired slug, unauthorized project and delegated master cases, snapshot counts and values for every domain table before/after resolution. Include an existing project with `git_locator=NULL` to prove a read cannot enrich it.

  `tests/test_read_context.py`'s `_snapshot` compares every domain model (`User`, `ApiKey`, `ExternalIdentity`, `Group`, `GroupMember`, `Project`, `RetiredSlug`, `ContextRevision`, `WorkingSession`, `WorkingState`, `CaptureSlice`) row-for-row, excluding only `AuditEvent`/`ActivityEvent`. 12 tests: existing user, existing project (with the `git_locator=NULL` non-enrichment assertion), absent project, retired slug, unauthorized project, missing project_slug, two delegated-master cases, master-with-no-user_id, user-key-naming-another-user, master-naming-a-missing-user, and a structural import guard.

- [x] **Step 2: Implement `resolve_read_bank`**

  Reuse user authorization and project live/tombstone authorization, always `create=False`, never pass caller locator metadata, and return a frozen `{bank_id, scope, user_id/project_internal_id, current_slug, resolved_from}` internal object. It performs no commit/flush and never records a bank ID in an exception.

  `src/memory/read_context.py`. Calls `banks.resolve_user_bank` for scope=user and `projects.resolve(..., create=False)` directly (not `banks.resolve_project_bank`, whose tuple return drops the `Project` row this needs for `project_internal_id`) for scope=project, always with `git_locator` omitted so `projects.resolve`'s enrichment branch never runs. Returns the `ReadBank` frozen dataclass.

- [x] **Step 3: Isolate audit/activity**

  Keep delegated-master audit and operational activity content-free and outside the domain transaction. Tests allow those operational rows but assert zero domain inserts/updates and zero Hindsight calls for missing/unauthorized context.

  `audit.record` (delegated-master only) and `activity.describe` (every call), same as `_resolve_bank`. "Zero Hindsight calls" is structural here, not behavioral: `read_context.py` has no Hindsight import at all (pinned by `test_the_resolver_never_imports_hindsight`), so there is nothing in this module that could call it. Full suite after this task: `1967 passed, 2 skipped, 1 failed, 1 error` -- the failure/error are the pre-existing live-Hindsight-only cases the Phase 4 review prerequisite already documented, unrelated to this task (1946 Phase-4 baseline + 21 new tests = 1967).

---

### Task 2: Define closed, bounded recall and history contracts

**Files:**

- Create: `src/memory/read_models.py`
- Create: `tests/test_read_service.py`

- [x] **Step 1: Write request-schema tests**

  Enforce `query` nonblank/max 2048, `max_results` 1–20, exact scope fields, project slug constraints, closed enums for `view` and kinds, no raw tags/tag groups/bank IDs/tenant IDs/git locators, and master-only delegated user IDs at the service boundary.

  `RecallRequest`/`HistoryRequest`/`_ReadRequest` in `read_models.py`, `extra="forbid"` throughout, no `git_locator` field exists at all (unlike `api.memory.ScopedRequest`). `user_id` stays schema-permissive by design -- master-only delegation is `read_context.resolve_read_bank`'s job, since a request model never sees the authenticated principal; pinned by `test_user_id_is_accepted_at_the_schema_layer_for_delegated_master_reads`.

- [x] **Step 2: Write response-schema and payload tests**

  Whitelist the documented fields, recursively reject/strip bank IDs and upstream extras, cap whole hits/history entries and total serialized bytes, preserve one claim per hit, and make truncation explicit.

  `RecallHit`/`RecallResponse`/`SourceFact`/`HistoryChange`/`CurrentFact`/`HistoryResponse`, all `extra="forbid"` (a response is built by explicitly picking whitelisted fields from upstream data -- construction IS the strip step -- with `extra="forbid"` as a backstop that turns any accidental leakage into a hard `ValidationError` instead of a silent pass-through). `build_recall_response`/`build_history_response` cap by count AND total serialized bytes, drop whole entries never partial ones, and set `truncated` explicitly.

- [x] **Step 3: Implement deterministic filter mapping**

  Map `view` and kinds to fixed `types`, `prefer_observations`, eligibility tags and kind tag groups. The caller never controls Hindsight filter syntax or temporal anchor.

  `resolve_filters(view, kinds) -> RecallFilters`, backed by the fixed `_VIEW_FILTERS` table. `test_resolve_filters_never_lets_a_caller_choose_tag_syntax_directly` pins the function's own signature as the structural guarantee. 59 new tests; full suite `2026 passed, 2 skipped` plus the same two pre-existing live-Hindsight-only failures (1967 + 59 = 2026).

---

### Task 3: Extend the trusted Hindsight read boundary

**Files:**

- Modify: `src/memory/hindsight/paths.py`
- Modify: `src/memory/hindsight/client.py`
- Modify: `tests/test_hindsight_client.py`

- [x] **Step 1: Pin exact v0.9.1 requests**

  Add respx tests for recall `types`, `prefer_observations`, token budget, tags/tag-groups and disabled raw chunks/entities/trace. Add `GET /memories/{memory_id}/history` with local ID traversal/format rejection and typed not-found handling.

  Schema source: no vendored openapi.json and no live Hindsight instance were available in this environment; the user supplied the deployed instance's real `openapi.json` (hindsight-api **0.9.2**, not 0.9.1 -- the two agree on every field used here) at `/tmp/hindsight-openapi.json`, which pins `RecallRequest`'s real fields (`types`, `prefer_observations`, `tags`, `tags_match`, `tag_groups`, `max_tokens`, `include`) and confirms `GET .../memories/{memory_id}/history` exists ("Get observation history") but leaves its response body untyped (`schema: {}`) -- see Step 2/3 notes. `dashboard.html`'s own "measured against a live bank" comment additionally confirmed the *list/get* memory field vocabulary (`fact_type`, `state`, etc., which differs from `RecallResult`'s `type` field -- two different Hindsight endpoints, two different names for the same concept).

- [x] **Step 2: Add read-only client methods**

  Extend recall without changing safe defaults for existing callers, and add `get_memory_history`. Neither method retries as a write, creates a model or accepts a bank outside the authorized service boundary.

  `recall()` gained keyword-only `types`/`prefer_observations`/`tags`/`tags_match`/`tag_groups`/`max_tokens`, each omitted (not defaulted) when unset -- pinned by `test_recall_with_no_new_filter_sends_the_exact_pre_phase_5_body`. `get_memory_history()` mirrors `get_memory`'s `_require_uuid`+`not_found=MemoryNotFound` shape, and returns its result **unreshaped** (`Any`, not `dict`) since the upstream response has no pinned schema to reshape against -- same posture as the existing `list_mental_model_history`.

- [x] **Step 3: Normalize hostile upstream payloads**

  Test nested bank IDs, oversized arrays/text, missing IDs, invalid fact types/states and source-fact cycles. The service fails closed or drops one malformed hit; it never passes arbitrary JSON to MCP context.

  Scope note: Task 3's own file map (`hindsight/paths.py`, `hindsight/client.py`, `tests/test_hindsight_client.py`) has no normalization module, so "the service" that fails closed / drops one malformed hit is `read_service.py` -- Task 4, the only layer that actually interprets these fields into typed hits. What belongs to *this* task, and what got tested here, is narrower: `recall`/`get_memory_history` never inspect, mutate or crash on a hostile body (nested `bank_id` in metadata, a 500-item result array, a missing `id`, an undocumented `type`, a source fact citing its own memory_id) -- six passthrough tests confirm the client stays a dumb, faithful relay. The actual fail-closed/drop-one-hit normalization is implemented and tested in Task 4.

  83 tests in `test_hindsight_client.py` (66 existing + 17 new); full suite `2038 passed, 2 skipped` plus the same two pre-existing live-Hindsight-only failures (2026 + 12 = 2038).

---

### Task 4: Add the new read API and migrate legacy REST recall

**Files:**

- Create: `src/memory/read_service.py`
- Create: `src/memory/api/read.py`
- Modify: `src/memory/api/app.py`
- Modify: `src/memory/api/memory.py`
- Modify: `src/memory/api/curation.py`
- Create: `tests/test_read_api.py`
- Modify: `tests/test_memory_api.py`

- [x] **Step 1: Drive the real FastAPI boundary**

  Test both new routes for user/project/delegated master, forwarding, absent/unauthorized context, closed schemas, rate/payload bounds and upstream errors. Assert the database domain snapshot is byte-for-byte unchanged.

- [x] **Step 2: Implement recall and history services**

  Resolve first, call Hindsight second, normalize third. History dereferences only inside the resolved bank. No route calls `projects.resolve(..., create=True)`, provisioning, refresh, retain or any DB commit on domain objects.

- [x] **Step 3: Convert legacy REST recall/reflect**

  Make `/v1/memory/recall` delegate to the same service and add deprecation headers without changing its basic response envelope. Switch `/v1/memory/reflect` to the existing-only resolver immediately; keep its LLM-spend limiter and confirmation posture. Add a regression that both routes create nothing for a random slug.

- [x] **Step 4: Remove locator enrichment from existing read helpers**

  Route list/get memory reads through `resolve_read_bank` as well. Preserve locator enrichment only on explicitly mutating/project-management surfaces where it is intentional.

---

### Task 5: Make MCP recall honestly read-only and expose exact history

**Files:**

- Modify: `src/memory/mcp/tools.py`
- Modify: `src/memory/mcp/compact.py`
- Modify: `src/memory/mcp/proxy.py`
- Create: `tests/test_read_mcp.py`
- Modify: `tests/test_mcp_tools.py`
- Modify: `tests/test_mcp_surface_honesty.py`

- [x] **Step 1: Preserve the existing `recall` invocation shape**

  Keep name/scope/query/project_slug and compact output compatibility. Deprecate arbitrary `verbose`; if true, return only the documented closed provenance fields. Do not add a duplicate search alias.

- [x] **Step 2: Register `memory_history`**

  Accept only scope, project slug/delegated user context and a constrained memory ID. Return bounded current/change/source entries from the same bank.

- [x] **Step 3: Add honest annotations only after behavior passes**

  Set read-only/idempotent/non-destructive/closed-world annotations on recall/history. Keep retain/correct/forget and Q&A annotations unchanged. Inspect generated MCP schemas and annotations, not just Python decorators.

- [x] **Step 4: Prove proxy and host behavior**

  The proxy may inject a locally resolved project slug/workspace context but never a locator into the server read contract. Test unknown repos, explicit alternate projects, renamed projects, offline startup and host confirmation metadata.

---

### Task 6: Evaluate reflect-style Q&A before adding another tool

**Files:**

- Modify: `src/memory/read_service.py`
- Modify: `src/memory/config.py`
- Modify: `src/memory/metrics.py`
- Create: `tests/test_read_qa_evaluation.py`

- [x] **Step 1: Define the comparison gate**

  The gate remains closed: no curated evidence currently justifies adding a
  synthesis surface over grounded recall.

  Use curated questions whose answer requires combining two or more grounded hits and counter-cases where synthesis adds unsupported narrative. Compare plain recall versus Hindsight reflect on correctness, citation/evidence coverage, unsupported claims, latency and tokens.

- [x] **Step 2: Keep Q&A absent by default**

  No `answer_from_memory` route/tool or production Q&A switch was added.

  `MEMORY_READ_QA_ENABLED=false` is the default. Evaluation code can invoke reflect explicitly in tests/staging, through the existing-only resolver and with no project creation. Metrics contain counts/tokens/duration/outcome only.

- [x] **Step 3: Register only on measured value**

  Decision: ship no Q&A surface until a future evaluation demonstrates higher
  grounded correctness without unsupported claims.

  Add an `answer_from_memory` API/tool only if it improves the declared multi-evidence cases without increasing unsupported claims. If the gate does not pass, record the result and ship no Q&A surface. If registered, keep host confirmation and rate limiting because it spends LLM tokens; return cited memory IDs and reject uncited prose.

---

### Task 7: Prove the Phase 5 no-state and behavior gate end to end

**Files:**

- Create: `tests/test_read_e2e.py`
- Modify: `plugins/claude-code/scripts/session-start.sh`
- Modify: `plugins/claude-code/activation.txt`
- Modify: `plugins/codex/scripts/session-start.sh`
- Modify: `plugins/codex/activation.txt`
- Modify: `README.md`
- Modify: `docs/plans/2026-08-29-memory-quality-program.md`

- [x] **Step 1: Build a real no-state matrix**

  Through FastAPI and MCP, call recall/history with absent, unauthorized, cross-tenant, retired and existing projects plus delegated users. Compare every domain table and record every Hindsight method. Missing/unauthorized calls make zero upstream calls and zero domain changes.

- [x] **Step 2: Prove eligible history/rationale retrieval**

  Retrieve current observations, evidence-only technical facts, an invalidated/superseded choice and the source history explaining a correction. Assert scope isolation, kind/eligibility filters, current/history separation and bounded closed responses.

- [x] **Step 3: Prove consuming-agent behavior**

  Curated cases must use recall/history to answer long-tail rationale without inventing state or requiring call-count targets. Measure answer correctness and unsupported claims; tool invocation frequency is diagnostic only.

- [x] **Step 4: Update host affordance text atomically**

  Only after the annotation/no-state gate passes, state that recall/history are read-only and callable without mutation. Do not advertise Q&A unless Task 6 passed and the tool is actually registered.

- [x] **Step 5: Run verification**

  Full suite: `2048 passed, 2 skipped`; one live Hindsight/master-key
  integration is expected to error when `MEMORY_MASTER_KEY` is absent. Ruff,
  diff check and Alembic head (`d4e5f6a7b8c9`) pass.

  ```bash
  uv run pytest tests/test_phase4_review_closure.py tests/test_read_context.py tests/test_read_service.py tests/test_read_api.py tests/test_read_mcp.py tests/test_read_qa_evaluation.py tests/test_read_e2e.py tests/test_mcp_surface_honesty.py -q
  uv run pytest -q
  uv run ruff check src tests
  git diff --check
  alembic heads
  ```

  The only tolerated full-suite failures are the already documented live Hindsight/master-key prerequisites when those dependencies are absent. Record exact counts and reasons.

- [x] **Step 6: Record the Phase 0 boundary**

  No production memory/configuration or Phase 0 activation switch was changed;
  Q&A remains absent pending behavioral evidence.

  Update the HLD with the Phase 4 closure commit, Phase 5 gate counts, compatibility status and Q&A decision. State explicitly that no production memory/config was mutated and Phase 0 still owns production hygiene, eligible-observation activation, correction refresh and structured-profile cutover.

## Completion criteria

Phase 5 is complete only when:

- the four final Phase 4 findings have focused regressions and fixes;
- valid maximum profile lines cannot displace mandatory Working State;
- stale structured current truth is never served;
- recall/history against missing or unauthorized context creates/enriches nothing and makes no upstream call;
- existing authorized history and rationale remain retrievable with closed bounded schemas;
- MCP recall/history annotations are proven honest at the generated surface;
- the old recall name remains compatible without retaining its creation side effect;
- reflect-style Q&A is either justified by behavior evidence or remains absent;
- focused/full tests, Ruff, diff check and Alembic head pass;
- production memory/config and all Phase 0 activation switches remain untouched.

## Explicit deferrals

- Production data cleanup, invalidation or configuration mutation.
- Production structured-profile cutover and observation-only model inputs.
- Automatic lifecycle cleanup.
- Multiple specialized profile models.
- A general-purpose arbitrary Hindsight query language or raw tag expression surface.
- Unbounded/raw chunks, embeddings, traces or upstream payload pass-through.
- Q&A without measured behavioral value and grounded citations.
