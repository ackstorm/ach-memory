# ach-memory v0.4.0 Context, Skill and Product-Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate typed memory and governed mental models into bounded standing context, finish explicit Working State, teach supported agents the retain contract, and remove the automatic-capture, structured-profile and INDEX/FULL product paths before releasing v0.4.0.

**Architecture:** This is the integration plan and starts only after the typed-retain/lifecycle and mental-model-governance branches are merged. `load_context` deterministically aggregates already-synthesized authorized model outputs, exact active expiring claims, Project Metadata and Working State; it performs no relevance reflection or cross-bank synthesis. One canonical skill teaches agents what to retain and when to abstain. The old pipeline is removed by forward commits and a forward migration after its replacement paths pass their gates.

**Tech Stack:** Python 3.12, FastAPI/Pydantic 2, SQLAlchemy/Alembic, Hindsight 0.9.2, FastMCP, tiktoken `o200k_base`, shell host adapters, pytest.

**Spec:** `docs/specs/2026-09-03-ach-memory-v0.4.0.md`

**Execution branch:** Integrate both reviewed parallel branches into `feat/ach-memory-v0.4.0` and execute this plan there; do not create a third feature branch from only one middle track.

## Global Constraints

- Requires both parallel plans merged onto the same foundation HEAD and reviewed independently.
- Resolve merge conflicts by preserving foundation type/table names; do not create a second migration head or restore old taxonomy.
- `load_context(project_slug?, workspace_id?)` reauthorizes every request, fetches ready model outputs concurrently and has a two-second end-to-end deadline.
- Deterministic output order is User models by key, Project Metadata, Project models by key, Active Time-Bounded Claims, then Working State.
- Budgets are 1,024 User-model tokens, 2,048 Project-model tokens, 256 Project Metadata, 256 active time-bounded claims, 512 Working State and 512 framing; total maximum 4,608 tokens under `ach-delivery-o200k-v1`.
- Whole model outputs and whole expiring claims are omitted, never truncated or used to displace unrelated entries.
- `load_context` performs no reflect, expiry sweep, bank/model creation or persistent last-good caching. It may enqueue at most one already-authorized safety repair per affected bank without waiting.
- Working State is exactly one record per tenant/user/project/workspace, at most 2 KiB and 512 delivered tokens, fenced by `(session_epoch, checkpoint_seq)`.
- The pre-compact hook reads no transcript and makes no memory write.
- Automatic transcript capture, semantic extractor/router, structured profiles, ranking/displacement, session brief, INDEX/FULL and their flags/deployments are removed, not left disabled.
- Frozen research remains reachable from `archive/memory-quality-v1.4-final`; it need not remain executable on the product branch.
- Production data cleanup is not part of this plan and still requires separate operation-by-operation approval.
- Test count is not a release KPI. Task 8 deletes tests whose product surface is removed and does not add
  absence or tombstone tests for that surface. Tests for retained governance and v0.4.0 behavior remain
  until the post-implementation portfolio report identifies a concrete duplicate or orphan and the user
  approves a separate cut.

---

### Task 1: Merge the parallel tracks and audit their shared contracts

**Files:**
- Inspect: `src/memory/memory_types.py`
- Inspect: `src/memory/v040_contracts.py`
- Inspect: `src/memory/models.py`
- Inspect: `src/memory/currentness.py`
- Inspect: `src/memory/mcp/tools.py`
- Create: `tests/test_v040_integration_contracts.py`

**Interfaces:**
- Consumes: reviewed tips of the typed-retain/lifecycle and mental-model-governance branches.
- Produces: one integration HEAD with no conflict markers, one Alembic head and compatible shared repository signatures.

- [ ] **Step 1: Merge both reviewed branches without squashing their task commits**

Run from the integration worktree:

```bash
rtk git merge --no-ff feat/ach-memory-v0.4.0-retain-lifecycle
rtk git merge --no-ff feat/ach-memory-v0.4.0-mental-models
```

Expected: either clean merges or conflicts limited to the MCP aggregator/surface tests and the boundary where curation marks model registrations withheld. The mental-model branch does not edit the Hindsight client or `currentness.py`.

- [ ] **Step 2: Add a failing cross-track contract test before resolving semantic conflicts**

```python
def test_curation_and_models_share_one_delivery_state(db, indefinite_record, registered_model, hindsight):
    forget_record(db, indefinite_record, reason="superseded", client=hindsight)
    row = get_registration(db, registered_model.bank, registered_model.model_key)
    assert row.delivery_state == "withheld"
    assert row.refresh_operation_id == hindsight.refresh_operation_id


def test_parallel_tracks_added_no_migration_heads():
    heads = subprocess.check_output(["uv", "run", "alembic", "heads"], text=True)
    assert len([line for line in heads.splitlines() if line.strip()]) == 1
```

- [ ] **Step 3: Resolve conflicts using the foundation ownership rules**

Keep `memory_tools.py` from retain/lifecycle, `model_tools.py` from model governance and both calls in the small `tools.register`. Keep the single `currentness.py` API; curation owns bank barriers and affected-model withholding, while mental-model service owns refresh observation and retry. No branch may reintroduce `ProfileKind`, `profile_eligible`, raw Hindsight IDs or a migration.

- [ ] **Step 4: Run both focused suites together**

```bash
rtk uv run pytest tests/test_retention.py tests/test_curation_service.py tests/test_expiry.py tests/test_mental_model_service.py tests/test_model_refresh.py tests/test_bootstrap.py tests/test_v040_integration_contracts.py -q
rtk uv run alembic heads
```

Expected: zero test failures and one migration head.

- [ ] **Step 5: Commit conflict resolution only when necessary**

If the merge commits already produce a clean passing tree, do not create an empty commit. Otherwise stage each explicitly resolved path shown by `rtk git status --short` and commit:

```bash
rtk git commit -m "fix(memory): integrate v0.4.0 control planes"
```

### Task 2: Implement the versioned delivery tokenizer and deterministic assembler

**Files:**
- Create: `src/memory/delivery.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_delivery.py`

**Interfaces:**
- Consumes: labelled section strings and declared per-section budgets.
- Produces: `count_tokens(text) -> int`, `DeliverySection`, `assemble_context(sections) -> ContextPayload`; tokenizer metadata `ach-delivery-o200k-v1`.

- [ ] **Step 1: Write failing tokenizer and whole-entry budget tests**

```python
def test_delivery_tokenizer_is_o200k_and_rejects_special_token_execution():
    assert TOKENIZER_VERSION == "ach-delivery-o200k-v1"
    assert count_tokens("ordinary text") > 0
    assert count_tokens("<|endoftext|>") > 0


def test_over_budget_section_is_omitted_whole_without_displacement():
    payload = assemble_context([
        DeliverySection(key="a", heading="User · a", text="x " * 600, max_tokens=10),
        DeliverySection(key="b", heading="User · b", text="kept", max_tokens=10),
    ], global_max_tokens=100)
    assert "User · a" not in payload.text
    assert "User · b\nkept" in payload.text
    assert payload.omissions[0].reason == "model_output_over_budget"
```

- [ ] **Step 2: Run tests and verify delivery module is absent**

Run: `rtk uv run pytest tests/test_delivery.py -q`

Expected: collection fails because `memory.delivery` does not exist.

- [ ] **Step 3: Add tiktoken and implement exact counting**

Add `tiktoken>=0.8,<1` to production dependencies and regenerate `uv.lock` with `rtk uv lock`.

```python
TOKENIZER_VERSION = "ach-delivery-o200k-v1"
_ENCODING = tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text, disallowed_special=()))
```

`assemble_context` escapes untrusted heading delimiters in content, sorts by the caller-supplied deterministic key, checks each section independently, adds fixed omission markers, and verifies the final framed response is at most the declared global ceiling. It never clips a string.

- [ ] **Step 4: Run deterministic and adversarial rendering tests**

Run: `rtk uv run pytest tests/test_delivery.py tests/test_content_caps.py -q`

Expected: whole-entry omission, forged heading content and exact 4,608-token boundary cases pass.

- [ ] **Step 5: Commit the delivery primitive**

```bash
rtk git add src/memory/delivery.py tests/test_delivery.py pyproject.toml uv.lock
rtk git commit -m "feat(context): add versioned bounded delivery"
```

### Task 3: Finish bounded Working State and fenced completion

**Files:**
- Modify: `src/memory/contracts.py`
- Modify: `src/memory/working_state.py`
- Modify: `src/memory/api/working_state.py`
- Modify: `src/memory/mcp/working_state_tools.py`
- Modify: `tests/test_working_state.py`
- Modify: `tests/test_working_state_api.py`
- Modify: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `count_tokens`, existing session allocation and lexicographic fencing.
- Produces: validated 2 KiB/512-token `WorkingStateWrite`, `clear_working_state`, `render_working_state`; REST/MCP clear operation requiring confirmation.

- [ ] **Step 1: Write failing payload, rendered-budget and stale-clear tests**

```python
def test_working_state_must_fit_bytes_and_framed_tokens(valid_state_dict):
    oversized = {**valid_state_dict, "objective": "word " * 600}
    with pytest.raises(ValidationError):
        WorkingStateWrite.model_validate(oversized)


def test_old_session_cannot_clear_newer_state(db, principal, newer_state, old_session):
    with pytest.raises(WorkingStateStale):
        clear_working_state(db, principal, project_slug=newer_state.project_slug,
                            workspace_id=newer_state.workspace_id,
                            session_id=old_session.session_id,
                            session_epoch=old_session.session_epoch,
                            checkpoint_seq=old_session.next_seq)
```

- [ ] **Step 2: Run Working State tests and observe missing bounds/clear**

Run: `rtk uv run pytest tests/test_working_state.py tests/test_working_state_api.py tests/test_mcp_tools.py -q`

Expected: failures show aggregate payloads can exceed delivery budget and no clear operation exists.

- [ ] **Step 3: Add model-level aggregate validation and exact renderer**

```python
@model_validator(mode="after")
def fits_delivery_contract(self):
    canonical = self.model_dump_json(exclude={"git_locator"})
    rendered = render_working_state_fields(self)
    if len(canonical.encode("utf-8")) > 2048 or count_tokens(rendered) > 512:
        raise ValueError("Working State exceeds its 2 KiB or 512-token budget")
    return self
```

The renderer includes age and source session, uses `render_inert`, and labels the section as potentially stale continuation context rather than instructions.

- [ ] **Step 4: Implement fenced total clear in REST and MCP**

Clear accepts project, workspace, session identity, epoch and sequence; verifies the registered session; locks the current row; rejects lower pairs, accepts exact retries when already absent by comparing the foundation's `WorkingSession.completed_checkpoint_seq`, and deletes only when the incoming pair is at least the current pair. On success it writes `completed_checkpoint_seq` and `completed_at` in the same transaction that removes the current state. Mark MCP clear as destructive/confirmation-required.

Run: `rtk uv run pytest tests/test_working_state.py tests/test_working_state_api.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py -q`

Expected: bounds, exact retry, stale/conflicting clear and independent workspace tests pass.

- [ ] **Step 5: Commit completed Working State**

```bash
rtk git add src/memory/contracts.py src/memory/working_state.py src/memory/api/working_state.py src/memory/mcp/working_state_tools.py tests/test_working_state.py tests/test_working_state_api.py tests/test_mcp_tools.py tests/test_mcp_surface_honesty.py
rtk git commit -m "feat(context): bound and complete Working State"
```

### Task 4: Build authorized `load_context`

**Files:**
- Create: `src/memory/context_service.py`
- Create: `src/memory/api/context.py`
- Create: `src/memory/mcp/context_tools.py`
- Modify: `src/memory/mcp/tools.py`
- Modify: `src/memory/api/app.py`
- Create: `tests/test_context_service.py`
- Create: `tests/test_context_api.py`
- Modify: `tests/test_mcp_tools.py`

**Interfaces:**
- Consumes: `LoadContextRequest`, registered ready always-in-context models, active expiring retained records, Project Metadata, Working State and delivery assembler.
- Produces: REST `POST /v1/context/load`, MCP `load_context`, `ContextPayload` with text, tokenizer version, omissions, section metadata and total tokens.

- [ ] **Step 1: Write failing order, authorization, concurrency and fail-open tests**

```python
def test_context_order_and_scope_labels(service, user_models, project_models, project, state):
    result = service.load(LoadContextRequest(project_slug=project.slug,
                                              workspace_id=state.workspace_id))
    headings = result.headings
    assert headings == ["User · alpha", "User · user-context", "Project Metadata",
                        "Project · beta", "Project · project-context",
                        "Active Time-Bounded Claims", "Working State"]


def test_model_fetches_settle_concurrently_and_one_failure_does_not_cancel_peers(service, slow_models):
    result = service.load(LoadContextRequest(project_slug="ach-memory"))
    assert result.elapsed_seconds < sum(model.delay for model in slow_models)
    assert "successful-peer" in result.text
    assert any(item.reason == "model_unavailable" for item in result.omissions)


def test_context_does_not_create_or_reflect(service, empty_database, hindsight):
    result = service.load(LoadContextRequest())
    assert result.total_tokens <= 4608
    hindsight.reflect.assert_not_called()
    assert empty_database.project_count() == 0
```

- [ ] **Step 2: Run context tests and verify service is absent**

Run: `rtk uv run pytest tests/test_context_service.py tests/test_context_api.py tests/test_mcp_tools.py -q`

Expected: collection fails because context service/routes do not exist.

- [ ] **Step 3: Implement concurrent model selection and fetching**

After authorization, select only ready registered models with `always_in_context=true`. Reject enabling a flag earlier if declared totals exceed 1,024 User or 2,048 Project tokens. Submit all selected Hindsight gets concurrently, wait only until the shared two-second monotonic deadline, cancel pending futures and retain successful peers. Sort only after all settled results are collected.

```python
done, pending = concurrent.futures.wait(futures, timeout=remaining)
for future in pending:
    future.cancel()
for future in done:
    collect_or_omit(future)
```

Reauthorize every call. If a bank currentness barrier is active, omit that bank's models and expiring claims with machine-readable status. An eligible access may enqueue one safety repair without waiting.

- [ ] **Step 4: Add deterministic non-model sections**

Project Metadata contains stored name, purpose and canonical-spec pointer and fits 256 tokens. Active Time-Bounded Claims selects only `recorded_at <= now < valid_until`, proven-current upstream records from the authorized User and active Project ledgers; order by expiry, recorded time and document ID; append whole claims until 256 tokens and report omitted count. It performs no expiry invalidation. Working State is included only when both project and workspace resolve and fits 512 tokens.

- [ ] **Step 5: Expose REST/MCP with honest side-effect annotations**

```python
# src/memory/mcp/tools.py
def register(mcp):
    memory_tools.register(mcp)
    model_tools.register(mcp)
    context_tools.register(mcp)
    working_state_tools.register(mcp)
```

`load_context` is not marked purely read-only because it may claim one existing safety repair. Its description states that it creates no project, bank, model or claim. Sanitize untrusted model output so it cannot forge section headings.

Run: `rtk uv run pytest tests/test_context_service.py tests/test_context_api.py tests/test_mcp_tools.py tests/test_bank_id_redaction.py -q`

Expected: all selected tests pass, exact order is stable, expired claims are absent and failures remain fail-open.

- [ ] **Step 6: Commit standing context delivery**

```bash
rtk git add src/memory/context_service.py src/memory/api/context.py src/memory/mcp/context_tools.py src/memory/mcp/tools.py src/memory/api/app.py tests/test_context_service.py tests/test_context_api.py tests/test_mcp_tools.py
rtk git commit -m "feat(context): load governed standing context"
```

### Task 5: Replace host startup/capture hooks with context and a pre-compact nudge

**Files:**
- Modify: `src/memory/cli.py`
- Modify: `src/memory/mcp/proxy.py`
- Modify: `plugins/claude-code/hooks/hooks.json`
- Modify: `plugins/codex/hooks/hooks.json`
- Modify: `plugins/claude-code/scripts/session-start.sh`
- Modify: `plugins/codex/scripts/session-start.sh`
- Create: `plugins/claude-code/scripts/pre-compact.sh`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_mcp_proxy.py`
- Modify: `tests/test_agent_bundle.py`

**Interfaces:**
- Consumes: `/v1/context/load`, client-derived project/workspace identity and MCP bootstrap.
- Produces: `uvx ach-memory context load`, `uvx ach-memory hook pre-compact`; no transcript read or memory write.

- [ ] **Step 1: Write failing CLI and hook-side-effect tests**

```python
def test_context_load_prints_only_context_to_stdout(cli, api):
    api.context("standing context")
    result = cli.run("context", "load")
    assert result.stdout == "standing context\n"
    assert "operation" not in result.stdout


def test_pre_compact_is_fixed_and_does_not_touch_network_or_stdin(cli, api):
    result = cli.run("hook", "pre-compact", stdin="SECRET transcript")
    assert result.stdout == PRE_COMPACT_NUDGE + "\n"
    assert api.requests == []
    assert "SECRET" not in result.stdout
```

- [ ] **Step 2: Run host tests and confirm old brief/capture behavior**

Run: `rtk uv run pytest tests/test_cli.py tests/test_mcp_proxy.py tests/test_agent_bundle.py -q`

Expected: failures show missing commands and the old session-brief/capture hook calls.

- [ ] **Step 3: Add host-neutral commands**

`context load` derives project and workspace through the existing local resolver, sends them in the request body and prints context text alone to stdout; diagnostics and omissions go to stderr without raw model/evidence content. `hook pre-compact` emits exactly:

```text
Before context is compacted, retain any durable decision, constraint, convention, fact or verified gotcha that is not yet in ach-memory. If project work is incomplete, update Working State. Do not retain the transcript or a generic session summary.
```

- [ ] **Step 4: Rewire supported host lifecycle files**

Claude SessionStart invokes `uvx ach-memory context load`; PreCompact invokes the fixed nudge. Codex uses its supported startup/instruction mechanism to call `load_context` once and carries the same fixed pre-compact guidance where a lifecycle event exists. Remove every Stop capture registration. Hosts without a lifecycle event receive no polling or fallback capture.

Run: `rtk uv run pytest tests/test_cli.py tests/test_mcp_proxy.py tests/test_agent_bundle.py tests/test_leakscan.py -q`

Expected: no hook reads transcript input, no hook contains capture commands, startup failure remains non-fatal and credentials never appear in argv/output.

- [ ] **Step 5: Commit host context plumbing**

```bash
rtk git add src/memory/cli.py src/memory/mcp/proxy.py plugins/claude-code/hooks/hooks.json plugins/codex/hooks/hooks.json plugins/claude-code/scripts/session-start.sh plugins/codex/scripts/session-start.sh plugins/claude-code/scripts/pre-compact.sh tests/test_cli.py tests/test_mcp_proxy.py tests/test_agent_bundle.py
rtk git commit -m "feat(hosts): load context without transcript capture"
```

### Task 6: Replace duplicated host skills with one canonical retain skill

**Files:**
- Create: `plugins/shared/ach-memory/SKILL.md`
- Create: `plugins/shared/ach-memory/references/curation.md`
- Create: `scripts/sync-agent-skill.py`
- Modify: `plugins/claude-code/skills/ach-memory/SKILL.md`
- Modify: `plugins/codex/skills/ach-memory/SKILL.md`
- Modify: `plugins/opencode/skills/ach-memory/SKILL.md`
- Modify: `plugins/pi/skills/ach-memory/SKILL.md`
- Modify: `plugins/claude-code/activation.txt`
- Modify: `plugins/codex/activation.txt`
- Modify: `plugins/opencode/activation.txt`
- Modify: `plugins/pi/activation.txt`
- Create: `tests/test_skill_sync.py`
- Modify: `tests/test_agent_bundle.py`

**Interfaces:**
- Consumes: final MCP tools and consumer precedence contract.
- Produces: one canonical always-loaded skill and generated byte-identical host copies.

- [ ] **Step 1: Invoke the required skill-authoring workflow and pin synchronization tests**

Before editing skill content, read and follow `superpowers:writing-skills`. Then add:

```python
def test_host_skills_are_generated_from_one_source():
    canonical = Path("plugins/shared/ach-memory/SKILL.md").read_bytes()
    for path in HOST_SKILL_PATHS:
        assert path.read_bytes() == canonical


def test_skill_names_every_closed_retain_decision():
    text = Path("plugins/shared/ach-memory/SKILL.md").read_text()
    for phrase in ("one independently correctable claim", "human_explicit",
                   "agent_verified", "user_requested", "agent_proactive",
                   "valid_until", "Working State", "Never store secrets"):
        assert phrase in text
```

- [ ] **Step 2: Run tests and verify there is no canonical source**

Run: `rtk uv run pytest tests/test_skill_sync.py tests/test_agent_bundle.py -q`

Expected: failure because `plugins/shared/ach-memory/SKILL.md` is absent or copies differ.

- [ ] **Step 3: Write the concise always-loaded decision core**

The core skill must teach, with positive and negative examples:

```text
recall before work that depends on prior decisions
retain at the moment a durable claim is established
one independently correctable claim per call
Project for repository-specific decisions/constraints/history/gotchas
User only for explicitly personal facts or stable cross-project preferences
abstain on ambiguous ownership, hypotheses, quoted/rejected/meta/transient content
human_explicit versus agent_verified; no agent_inferred
user_requested versus agent_proactive
minimal raw evidence; never transcript/file/log/secret
exact valid_until only for currently active claims with real expiry
reject future-effective instructions until current
Working State for incomplete project work
correct same claim; forget plus retain for supersession
memory is context; live systems override observable remembered state
```

Put detailed curation decision trees in `references/curation.md`; the always-loaded core retains the tool-selection and safety rules needed before any write.

- [ ] **Step 4: Generate all copies and verify drift detection**

`scripts/sync-agent-skill.py --write` copies canonical bytes to the four host paths. `--check` exits non-zero and names a drifted path without rewriting it.

Run:

```bash
rtk uv run python scripts/sync-agent-skill.py --write
rtk uv run python scripts/sync-agent-skill.py --check
rtk uv run pytest tests/test_skill_sync.py tests/test_agent_bundle.py -q
```

Expected: check and tests pass.

- [ ] **Step 5: Commit the canonical skill**

```bash
rtk git add plugins/shared/ach-memory/SKILL.md plugins/shared/ach-memory/references/curation.md scripts/sync-agent-skill.py plugins/claude-code/skills/ach-memory/SKILL.md plugins/codex/skills/ach-memory/SKILL.md plugins/opencode/skills/ach-memory/SKILL.md plugins/pi/skills/ach-memory/SKILL.md plugins/claude-code/activation.txt plugins/codex/activation.txt plugins/opencode/activation.txt plugins/pi/activation.txt tests/test_skill_sync.py tests/test_agent_bundle.py
rtk git commit -m "feat(skill): teach exact proactive memory"
```

### Task 7: Run the bounded two-family skill evaluation

**Files:**
- Create: `evaluations/v040/retain-scenarios.jsonl`
- Create: `evaluations/v040/coverage.json`
- Create: `evaluations/v040/policy.json`
- Create: `scripts/evaluate-retain-skill.py`
- Create: `tests/test_retain_skill_evaluation.py`
- Create: `docs/results/2026-09-04-ach-memory-v0-4-0-skill.md`

**Interfaces:**
- Consumes: canonical skill, final MCP JSON schemas and headless Codex/Claude runners.
- Produces: 20 canonical scenario definitions, four sealed holdouts, 120 raw decisions and per-family release metrics.

- [ ] **Step 1: Freeze coverage and thresholds before model runs**

`coverage.json` maps all 20 case IDs to every SPEC §14.1 behavior and positive/negative polarity. `policy.json` contains exactly:

```json
{
  "wrong_scope": 0,
  "secret_retention": 0,
  "critical_claim_recall": 1.0,
  "aggregate_recall_min": 0.85,
  "retention_precision_min": 0.95,
  "abstention_accuracy_min": 0.90,
  "repetitions": 3,
  "families": ["codex", "claude-code"],
  "holdout_case_count": 4
}
```

The four holdout definitions include at least one critical case and remain unavailable to the skill-tuning command until final evaluation.

- [ ] **Step 2: Add validation and mutation tests for the evaluator**

```python
def test_coverage_names_every_scenario_and_required_behavior(corpus, coverage):
    assert set(coverage["cases"]) == {case["case_id"] for case in corpus}
    assert REQUIRED_BEHAVIORS <= set(coverage["behaviors"])


def test_wrong_scope_mutation_fails_hard_gate(valid_result):
    mutated = valid_result.replace_one_scope("project", "user")
    assert score(mutated).passed is False
    assert "WRONG_SCOPE" in score(mutated).reason_codes
```

Run: `rtk uv run pytest tests/test_retain_skill_evaluation.py -q`

Expected before implementation: collection or assertions fail.

- [ ] **Step 3: Implement a content-safe headless evaluator**

For each host/model version, provide only the canonical skill, a synthetic scenario and fake MCP tool schemas. Capture closed decisions (`retain`, `working_state`, `abstain`, scope/type/basis/trigger/expiry/evidence-shape), never free-form chain of thought. Reject unknown fields, missing repetitions, duplicate tuples and executed=false. Store synthetic content only under the evaluation directory; scan artifacts for planted canaries and credentials.

- [ ] **Step 4: Tune only on the sixteen visible cases, then run holdout once**

Run visible evaluation until the skill meets the frozen gates without fixture-string rules. Once frozen, execute all 20 cases three times for each family exactly once as the release run:

```bash
rtk uv run python scripts/evaluate-retain-skill.py validate
rtk uv run python scripts/evaluate-retain-skill.py run --final
rtk uv run python scripts/evaluate-retain-skill.py score
```

Expected: 120 unique tuples. Any secret retention or wrong physical scope in one raw run fails the release, regardless of majority.

- [ ] **Step 5: Record and commit the result without adding runtime machinery**

The result document records host/model versions, corpus/policy hashes, per-family recall/precision/abstention metrics, hard-gate outcome and named failing case IDs. It does not change thresholds after observing output and does not compare unrelated model rankings.

```bash
rtk git add evaluations/v040/retain-scenarios.jsonl evaluations/v040/coverage.json evaluations/v040/policy.json scripts/evaluate-retain-skill.py tests/test_retain_skill_evaluation.py docs/results/2026-09-04-ach-memory-v0-4-0-skill.md
rtk git commit -m "test(skill): validate v0.4.0 retain behavior"
```

### Task 8: Remove the superseded product path by forward commits

**Files:**
- Delete: `src/memory/capture/`
- Delete: `src/memory/profiles.py`
- Delete: `src/memory/brief.py`
- Delete: `src/memory/revisions.py`
- Delete: `src/memory/api/capture.py`
- Delete: `src/memory/api/brief.py`
- Modify: `src/memory/api/admin.py`
- Modify: `src/memory/api/app.py`
- Modify: `src/memory/cli.py`
- Modify: `src/memory/config.py`
- Modify: `src/memory/metrics.py`
- Modify: `src/memory/models.py`
- Delete: `deploy/helm/ach-memory/templates/capture-worker-deployment.yaml`
- Delete: `deploy/helm/ach-memory/templates/profile-evaluator-cronjob.yaml`
- Modify: `deploy/helm/ach-memory/values.yaml`
- Modify: `deploy/helm/README.md`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Delete: `plugins/claude-code/scripts/capture-checkpoint.sh`
- Delete: `experiments/memory_quality/`
- Delete: `tests/test_brief.py`
- Delete: `tests/test_capture_api.py`
- Delete: `tests/test_capture_classifier.py`
- Delete: `tests/test_capture_configuration.py`
- Delete: `tests/test_capture_deployment.py`
- Delete: `tests/test_capture_e2e.py`
- Delete: `tests/test_capture_extractor.py`
- Delete: `tests/test_capture_filer.py`
- Delete: `tests/test_capture_local.py`
- Delete: `tests/test_capture_repository.py`
- Delete: `tests/test_capture_worker.py`
- Delete: `tests/test_memory_quality_baseline.py`
- Delete: `tests/test_memory_quality_consumer.py`
- Delete: `tests/test_memory_quality_corpus.py`
- Delete: `tests/test_memory_quality_delivery.py`
- Delete: `tests/test_memory_quality_delivery_runner.py`
- Delete: `tests/test_memory_quality_official_runtime.py`
- Delete: `tests/test_memory_quality_preprocessing.py`
- Delete: `tests/test_memory_quality_reliability.py`
- Delete: `tests/test_memory_quality_runner.py`
- Delete: `tests/test_memory_quality_scoring.py`
- Delete: `tests/test_memory_quality_semantic.py`
- Delete: `tests/test_memory_quality_semantic_repair.py`
- Delete: `tests/test_memory_quality_splitter.py`
- Delete: `tests/test_memory_quality_upstream.py`
- Delete: `tests/test_profile_e2e.py`
- Delete: `tests/test_profile_evaluation.py`
- Delete: `tests/test_profile_provisioning.py`
- Delete: `tests/test_profiles.py`
- Delete after migrating surviving Working-State/security assertions: `tests/test_phase2_review_closure.py`
- Delete after migrating surviving security assertions: `tests/test_phase3_review_closure.py`
- Delete: `tests/test_phase4_review_closure.py`
- Create: `migrations/versions/a7b8c9d0e1f2_remove_superseded_memory_pipeline.py`

**Interfaces:**
- Consumes: verified replacement retain, context, skill and host surfaces.
- Produces: no runtime import, command, route, hook, deployment or flag for automatic capture, structured profiles or INDEX/FULL; historical applied migrations remain intact.

- [ ] **Step 1: Remove code, host and deployment surfaces**

Delete only the explicitly superseded files. In `admin.py`, remove legacy brief/profile provisioning while preserving memory clear/delete and audit. In config/deployments remove `MEMORY_CAPTURE_ENABLED`, `MEMORY_CAPTURE_WORKER_ENABLED`, `MEMORY_CAPTURE_CORRECTION_REFRESH_ENABLED` and `MEMORY_PROFILE_DELIVERY_MODE`. Remove profile-only metrics but retain generic Hindsight, API, activity and error metrics.

- [ ] **Step 2: Add the forward database migration**

The new migration follows the existing v0.4 control-plane head and drops `capture_slices` and `context_revisions` only after replacement structures exist. It never edits `d4e5f6a7b8c9_capture_slices.py` or earlier applied revisions. Downgrade recreates empty legacy tables with their previous schema and documents that deleted queue/profile-revision data is not reconstructable.

- [ ] **Step 3: Preserve only positive retained-product coverage**

Delete the exact test files listed above. Before deleting `test_phase2_review_closure.py` and `test_phase3_review_closure.py`, move every still-applicable Working State, credential-redaction and authorization assertion into `test_working_state.py`, `test_sanitization.py`, `test_retention.py` or `test_agent_bundle.py`; do not discard a surviving invariant merely because its old mixed-purpose file is removed. Run:

```bash
rtk uv run pytest tests/test_sanitization.py tests/test_retention.py tests/test_context_service.py tests/test_agent_bundle.py tests/test_metrics.py -q
rtk uv run alembic heads
```

Expected: all selected tests pass and exactly one head is reported.

- [ ] **Step 4: Commit the smaller product**

Stage only the Task 8 paths enumerated in its **Files** list, including the named review-closure tests after their surviving assertions are migrated. Verify the staged paths with `rtk git diff --cached --name-only` and run `rtk git diff --check`, then commit:

```bash
rtk git commit -m "refactor(memory): remove automatic memory pipeline"
```

### Task 9: Close release, migration and live delivery gates

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `README.md`
- Create: `docs/releases/0.4.0.md`
- Create: `tests/test_v040_upgrade.py`
- Create: `tests/test_v040_context_live.py`
- Create: `docs/results/2026-09-04-ach-memory-v0-4-0-release.md`
- Create: `docs/results/2026-09-04-ach-memory-v0-4-0-test-portfolio.md`

**Interfaces:**
- Consumes: complete v0.4.0 implementation and all prior plan result documents.
- Produces: version `0.4.0`, tested clean/upgrade migrations, measured two-second context p95, an activation report and a bounded test-portfolio cut report; does not execute production cleanup or delete tests for retained product behavior. Public release material describes only the resulting v0.4.0 product and does not preserve a catalog of abandoned internal surfaces.

- [ ] **Step 1: Add clean-install and 0.3.5-upgrade tests**

The upgrade test creates a `0.3.5` schema fixture containing canonical/retired slugs, Working State, capture rows and legacy Hindsight-model inventory; upgrades to head; asserts slug preservation, Working State preservation, new registries/ledgers empty, obsolete local tables removed and no upstream request made during migration.

Run: `rtk uv run pytest tests/test_v040_upgrade.py -q`

Expected before fixture/implementation completion: failure identifying the first migration mismatch.

- [ ] **Step 2: Add the maximum-selection latency and fail-open live test**

Against disposable banks, exercise the maximum legal selected set under Hindsight's 256-token minimum: disable built-in delivery, enable four 256-token User custom models and five 256-token Project custom models, seed active expiring claims and Working State, warm the endpoint, then execute 30 calls. Also exercise the default built-in configuration: both built-ins plus two User and four Project custom models, eight selected total. Fetches must run concurrently; p95 must be at most two seconds in both configurations. Separately make one model slow/unavailable and assert peers, Project Metadata and Working State still return before the deadline with an omission reason.

- [ ] **Step 3: Run all non-live release gates**

```bash
rtk uv run ruff check src tests scripts
rtk git diff --check
rtk uv run pytest -q --ignore=tests/test_integration_hindsight.py --ignore=tests/test_append_integration.py --ignore=tests/test_v040_hindsight_live.py --ignore=tests/test_v040_mental_models_live.py --ignore=tests/test_v040_context_live.py
rtk uv run alembic heads
```

Expected: zero failures/errors, clean diff and one Alembic head.

- [ ] **Step 4: Run all explicit disposable live gates**

```bash
rtk env HINDSIGHT_V040_CONFIRM=disposable-banks-only uv run pytest tests/test_v040_hindsight_live.py tests/test_v040_mental_models_live.py tests/test_v040_context_live.py -q
```

Expected: exact retain, model governance and context latency gates pass; every registered bank/model is cleaned up. A failed gate blocks activation and is recorded without lowering thresholds.

- [ ] **Step 5: Update release metadata and documentation**

Set project version to `0.4.0`, regenerate the lock, and document:

```text
typed retain and evidence contract
English-only retained-content limitation
built-in model behavior and temporary withholding during definition refresh
always_in_context User exposure
context/Working State budgets
Hindsight 0.9.2 compatibility preflight
separate approval requirement for production cleanup
```

- [ ] **Step 6: Record the test-portfolio cutline without optimizing for a number**

Create `docs/results/2026-09-04-ach-memory-v0-4-0-test-portfolio.md`. Its baseline is the integrated
pre-Plan-4 `main` at `2cb1753`: 2,240 non-integration tests passed, 4 skipped and 6 live tests were
deselected in 124.66 seconds. Record the post-retirement counts and runtime from Step 3, plus:

```text
baseline and resulting totals, without preserving a tombstone catalog of abandoned tests
remaining tests grouped by retained responsibility:
  identity/auth/governance
  User/Project resolution and ownership
  typed retain/currentness/expiry
  mental-model governance
  context delivery
  Working State
  host integration, skill and packaging
orphan tests with no retained product surface (must be zero)
clusters where three or more tests exercise the same invariant through the same surface
recommended cut: now, a named 0.4.x follow-up, or keep — with one sentence of product risk
```

Use this read-only counter for consistent Python LOC/file totals before and after retirement:

```bash
rtk uv run python - <<'PY'
from pathlib import Path

for root in (Path("src/memory"), Path("tests")):
    files = sorted(root.rglob("*.py"))
    lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files)
    print(f"{root}: files={len(files)} lines={lines}")
PY
```

Task 8's explicitly listed deletions are the boundary for this execution. Do not retain absence tests,
tombstone assertions or public documentation whose only purpose is to remember the abandoned product,
and do not delete or merge additional retained-product tests merely to reduce the count. Do not add a new
eval framework. Finish the report with `READY_FOR_TEST_PORTFOLIO_CUT_REVIEW`; the user decides any
further consolidation after Plan 4 from this evidence.

- [ ] **Step 7: Verify release diff and commit**

```bash
rtk uv lock --check
rtk git diff --check
rtk git status --short
rtk git add pyproject.toml uv.lock README.md docs/releases/0.4.0.md tests/test_v040_upgrade.py tests/test_v040_context_live.py docs/results/2026-09-04-ach-memory-v0-4-0-release.md docs/results/2026-09-04-ach-memory-v0-4-0-test-portfolio.md
rtk git commit -m "chore(release): prepare ach-memory 0.4.0"
```

Do not tag, publish, deploy, enable production behavior or run Phase 0 in this task.

## Completion Gate

- Parallel branches are integrated under one schema and shared currentness contract.
- `load_context` is deterministic, concurrent, bounded, fail-open and performs no reflect or creation.
- Working State accepts only payloads that fit both storage and delivery contracts and supports fenced completion.
- Every host uses context loading and the no-write pre-compact nudge; no host captures transcripts.
- One canonical skill passes drift checks and the frozen two-family behavioral evaluation.
- Automatic capture, extractor/router, structured profiles, session brief, INDEX/FULL, experiments and their deployment/runtime surfaces are absent from the product branch.
- Clean install, 0.3.5 upgrade, non-live suite and disposable Hindsight gates pass.
- Version/release notes are `0.4.0`; production activation and one-off cleanup remain separate human-approved actions.
- The test-portfolio report proves that removed-product tests are gone, every remaining test maps to a
  retained responsibility and any further cut is a named human decision rather than a numeric target.

After this gate, use `superpowers:finishing-a-development-branch` to present merge/release options. Do not execute the SPEC §13 production cleanup until the user separately approves each numbered operation.
