# Memory Quality Phase 5.5 Hindsight Compatibility Bake-off Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine, with isolated component-level evidence, which ach-memory capture and delivery components can be replaced or simplified using Hindsight 0.9.2 and `hindsight-coding-agents` 0.5.1 before any production-memory mutation or activation.

**Architecture:** Build a non-production experiment harness under `experiments/memory_quality/`. It runs four independent comparisons—preprocessing/adapters, semantic extraction, delivered context, and failure recovery—against synthetic annotated cases and disposable Hindsight banks on the real local Hindsight 0.9.2 service. The harness records blinded per-case results and produces component rulings, but it never changes production code, ach-memory domain state, deployment defaults, or existing banks; any winning architecture requires a separate approved implementation plan.

**Tech Stack:** Python 3.12, Pydantic 2, httpx/respx, pytest, JSONL, Node.js 18+, `tsx` 4.20.6 for a read-only bridge into the pinned TypeScript source, Hindsight API 0.9.2, `@vectorize-io/hindsight-coding-agents` 0.5.1.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (§3.5, §4, §7, §10, §16, §19); program boundary: `docs/plans/2026-08-29-memory-quality-program.md`; approved Phase 5.5 design: the 2026-09-02 architecture decision immediately preceding this plan.

## Global Constraints

- Phase 5 must be committed, reviewed and integrated before Task 0. Do not absorb its existing dirty working tree into a Phase 5.5 commit.
- Use Hindsight server `0.9.2` and `hindsight-coding-agents` `0.5.1` for every compared run. A version or source-hash mismatch is a hard preflight failure, not a warning.
- `HINDSIGHT_BAKEOFF_URL` defaults to the available local service `http://127.0.0.1:8888`. Refuse live mutation unless it resolves to loopback or an explicitly allow-listed staging host and `HINDSIGHT_BAKEOFF_CONFIRM=disposable-banks-only` is set exactly. Equality with an existing `MEMORY_HINDSIGHT_URL` is allowed only on loopback; the bank-prefix and registry fences remain mandatory.
- Every mutated bank ID starts with `mq55-<run_id>-`. Cleanup may delete only bank IDs recorded in that run's local registry.
- Never call an ach-memory API, connect to the ach-memory database, or import `memory.db`, `memory.models`, `memory.api`, deployment code, or settings that can resolve a production bank.
- All production activation flags remain off: `MEMORY_CAPTURE_ENABLED=false`, `MEMORY_CAPTURE_WORKER_ENABLED=false`, `MEMORY_CAPTURE_CORRECTION_REFRESH_ENABLED=false`, and `MEMORY_PROFILE_DELIVERY_MODE=legacy`.
- Disable official integration side effects during experiments: `gitIngest="none"`, `codebaseSurvey=false`, `autoSeed=false`, `autoUpdate=false`, `optInOnly=true`; use only explicitly named disposable banks.
- The committed corpus is synthetic. Optional real-session cases stay outside git and must already be sanitized; the harness rejects unknown corpus fields and any case without a declared secret-canary list.
- `ACH_MEMORY_URL`/`ACH_MEMORY_API_KEY` identify an available legacy ach-memory service (currently advertising API `0.1.0`). They are permitted only for the explicit read-only calibration described in Task 4: `GET /openapi.json` and `GET /v1/session-brief`. Never list/export memories, call a mutating verb, forward legacy content to Hindsight or persist the response body.
- Semantic and delivery variants run three times per case, with the same Hindsight server, LLM model, temperature where configurable, input, prompt and token budget.
- Raw fixture canaries may exist only in the committed input corpus. An in-memory `official_preprocess` result may still contain one long enough to set a boolean failure flag, but that text is never serialized or submitted. Canaries must be absent from every Hindsight request/document, extracted fact, observation, page, delivered context, artifact, report and log.
- No A/B/C winner is integrated automatically. The only persistent product change authorized by this phase is documentation of the measured ruling.
- Phase 0 remains a separate explicit approval because it mutates live memory.

## Fixed Upstream Baseline

The evaluator pins these files from `../hindsight/hindsight-integrations/coding-agents`:

| Artifact | Required value |
|---|---|
| Package version | `0.5.1` |
| Release commit containing the package | `c61c4e7d7` |
| `src/core/missions.ts` SHA-256 | `556859a337e7bd5eb48b9ec3c16caf99ba9537f0c4a22b44745c4bd570c763fa` |
| `src/core/transcript.ts` SHA-256 | `b0dee61adedbcceba12b80acf37ecec456f06dff7481183867d14080c0488eda` |
| `src/core/transcript-codex.ts` SHA-256 | `afb8163db21b9e85ebd8c297f6cfd6da4607557c14c85c113608672278a3f4fe` |
| `src/core/chat.ts` SHA-256 | `e2e64fc6e22e0383643de55ebfb1d275a563798f2864273febb5f8220a6e361a` |
| `src/core/retain-hook.ts` SHA-256 | `b6590ef8582ae650b42db21004973912e9ac287d8f55ebbd4d49f604b5be496d` |
| `src/core/hindsight.ts` SHA-256 | `cd3d32d2586ad3d6ce86f0764fe2bd5db7bed1b48f25ecce6d6ce2987a5eb3ec` |
| `src/core/retain-cursor.ts` SHA-256 | `bd4883403b0b0425484ffdb00f8d42d65159612cd037cb95c808e066d1de320b` |
| `src/core/types.ts` SHA-256 | `b5f46397dc6d260ac256c527403c67bef797cb52409c461c040980c6c8940d17` |
| `package.json` SHA-256 | `c2304026211a71a6ac21d4caa9857fda6d1df7e7cb1060bd5b3053dca196d6b9` |
| `package-lock.json` SHA-256 | `3cb971b394933410511d11831bed485df457172a897311d914984f196fe1a621` |

`HINDSIGHT_CODING_AGENTS_DIR` may override the default sibling path. It changes the location, never the accepted hashes.

The local live lab was discovered before planning: `http://localhost:8888/openapi.json` reports Hindsight `0.9.2`, and the legacy ach-memory OpenAPI reports `0.1.0`. Preflight verifies these facts again; the plan does not treat discovery-time state as execution-time proof.

## Experiment Matrix

### A. Preprocessing and host adapter

Run the same Claude Code raw JSONL through:

1. `ach_preprocess`: `memory.capture.local.read_new_slice` + `build_batch`;
2. `official_preprocess`: official `readClaudeTranscript` + official JSONL renderer;
3. `hybrid_preprocess`: official normalized turns passed through ach-memory's redaction/drop policy.

This experiment measures retained user/assistant substance, tool noise, file/path leakage, secret leakage, normalized size and source-span traceability. It does not call an LLM.

### B. Semantic extraction

Run each canonical sanitized transcript through:

1. `ach_semantic`: current `capture.extractor.extract` + `capture.classifier.classify`;
2. `native_semantic`: official conversation mission and retain strategy in one disposable bank;
3. `hybrid_semantic`: a minimal scope splitter producing one-claim `{text, bank, provenance}` records with no `kind`, `origin` or `profile_eligible`; Hindsight's official mission extracts the durable facts from each bank projection. Working State remains a separate splitter output and is never retained.

`native_semantic` is eligible to win extraction quality but cannot pass user/project physical-isolation gates by itself. Only `hybrid_semantic` may be considered as a full dual-bank replacement.

### C. Delivered context

Freeze one memory/profile snapshot per case and compare:

1. `ach_index` and `ach_full` from the current compiler;
2. `official_reflect` from one low-budget first-prompt reflect;
3. `official_pages` from page search plus reading the top page;
4. `hybrid_delivery` containing deterministic User Core, Project Metadata and Working State plus the top relevant page result.

The first pass asserts the exact payload. When `MEMORY_BAKEOFF_CONSUMER_CMD` is configured, the same headless consumer receives each context and returns a closed JSON answer for behavioral scoring. Without complete consumer runs, Phase 5.5 may measure payload equivalence but must rule `profile_compiler` and `delivery_protocol` as `insufficient_evidence` for removal or replacement.

### D. Reliability

Exercise ach-memory and the actual official Stop runtime independently under: death before send, death while awaiting ACK, ACK lost after commit, `429`, Hindsight offline, worker death after each persisted stage, expired lease, out-of-order checkpoints, no later host event, and a later host event. The matrix distinguishes pre-ACK recovery from post-ACK eventual processing.

## Decision Rules

Hard gates are binary and must pass on every repetition to qualify a variant for the corresponding responsibility:

- no planted secret after local preprocessing;
- no personal content in any Project Bank document, fact, observation, page or delivered context;
- no project-specific content in any User Bank object;
- no unauthorized or implicit state creation;
- every critical final decision delivered in all three runs;
- no critical rejected/superseded proposal presented as current;
- exact latest-wins behavior in correction cases;
- no lower `(session_epoch, checkpoint_seq)` overwrites a higher pair;
- every server-accepted checkpoint eventually completes under the recoverable post-ACK faults;
- pre-ACK loss is reported honestly as `recovered_later`, `requires_future_event`, or `unrecoverable_without_outbox`.

For non-critical quality, compare named cases rather than percentage thresholds:

- A simpler variant wins a component if it passes every applicable hard gate and has no additional repeatable miss relative to the current implementation.
- One extra non-critical miss is a recorded trade-off requiring explicit user approval; two or more repeated misses make the variant non-inferior=false.
- A custom component survives if removing it causes any hard-gate failure or at least two repeatable named-case regressions.
- Median, p95 latency and token totals are reported. A repeated overhead above 25% is a decision factor, not a standalone failure; it must be weighed against named correctness or safety gains.
- Ties go to the smaller maintained surface. Count production Python/TypeScript lines and external runtime dependencies only as diagnostics, never as a quality score.

## File Map

- Create `experiments/__init__.py` and `experiments/memory_quality/__init__.py`: make the experiment code importable by tests without shipping it in the wheel.
- Create `experiments/memory_quality/contracts.py`: closed corpus, run, score, fault and decision models.
- Create `experiments/memory_quality/corpus/semantic.jsonl`: 16 annotated semantic cases.
- Create `experiments/memory_quality/corpus/delivery.jsonl`: 10 frozen delivered-context cases.
- Create `experiments/memory_quality/corpus/hosts/claude.jsonl`: one synthetic raw host transcript containing all preprocessing canaries.
- Create `experiments/memory_quality/upstream.py`: pinned source/version verification and the `tsx` subprocess bridge.
- Create `experiments/memory_quality/official_bridge.ts`: invoke official normalizer/writeback functions without copying them into ach-memory.
- Create `experiments/memory_quality/hindsight.py`: disposable-bank-only Hindsight client and registry.
- Create `experiments/memory_quality/preprocessing.py`: three preprocessing variants and measurements.
- Create `experiments/memory_quality/semantic.py`: three semantic variants and normalized outputs.
- Create `experiments/memory_quality/delivery.py`: four delivery variants and optional headless consumer.
- Create `experiments/memory_quality/reliability.py`: fault scenarios and recovery outcome recording.
- Create `experiments/memory_quality/scoring.py`: deterministic hard gates, blinded packet generation and component rulings.
- Create `experiments/memory_quality/runner.py`: CLI orchestration, artifact manifest and cleanup.
- Create `experiments/memory_quality/adjudication.example.json`: closed manual semantic-adjudication shape.
- Create `tests/test_memory_quality_corpus.py`, `tests/test_memory_quality_upstream.py`, `tests/test_memory_quality_preprocessing.py`, `tests/test_memory_quality_semantic.py`, `tests/test_memory_quality_delivery.py`, `tests/test_memory_quality_reliability.py`, `tests/test_memory_quality_scoring.py`, and `tests/test_memory_quality_runner.py`.
- Modify `.gitignore`: ignore only `/.artifacts/memory-quality/`.
- Create `docs/results/2026-09-02-memory-quality-phase-5-5.md` during the measured run.
- Modify `docs/plans/2026-08-29-memory-quality-program.md` only after the final decision packet is complete.

---

### Task 0: Freeze the corpus, contracts and upstream identity

**Files:**

- Create: `experiments/__init__.py`
- Create: `experiments/memory_quality/__init__.py`
- Create: `experiments/memory_quality/contracts.py`
- Create: `experiments/memory_quality/corpus/semantic.jsonl`
- Create: `experiments/memory_quality/corpus/delivery.jsonl`
- Create: `experiments/memory_quality/corpus/hosts/claude.jsonl`
- Create: `tests/test_memory_quality_corpus.py`
- Modify: `.gitignore`

**Interfaces:**

- Produces: `load_semantic_cases(path: Path) -> tuple[SemanticCase, ...]`
- Produces: `load_delivery_cases(path: Path) -> tuple[DeliveryCase, ...]`
- Produces: `corpus_digest(cases: Sequence[BaseModel]) -> str`
- Produces: closed models `SemanticCase`, `DeliveryCase`, `ExpectedUnit`, `RunObservation`, `GateResult`, `ComponentDecision`.

- [ ] **Step 1: Prove Phase 5 is an integrated clean prerequisite**

Run:

```bash
rtk git status --short
rtk git log -5 --oneline
rtk uv run pytest tests/test_read_context.py tests/test_read_service.py tests/test_read_api.py -q
```

Expected: status is empty or contains only this plan, commit `0675406 feat: add read-only recall and memory history` (or its integrated descendant) is present, and the focused Phase 5 suite passes. If this plan is the sole untracked change, commit it first with:

```bash
rtk git add docs/superpowers/plans/2026-09-02-memory-quality-phase-5-5-hindsight-bakeoff.md
rtk git commit -m "docs(quality): plan the hindsight component bakeoff"
rtk git status --short
```

The final status must be empty. Stop without changing files if any other path is dirty or the Phase 5 integration commit is absent.

- [ ] **Step 2: Write failing closed-contract tests**

Pin strict models and exact corpus sizes:

```python
def test_the_committed_corpus_is_closed_and_complete():
    semantic = load_semantic_cases(SEMANTIC)
    delivery = load_delivery_cases(DELIVERY)
    assert len(semantic) == 16
    assert len(delivery) == 10
    assert all(case.repetitions == 3 for case in (*semantic, *delivery))
    assert all(case.secret_canaries for case in (*semantic, *delivery))


def test_unknown_case_fields_fail_closed(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text('{"id":"S99","unknown":true}\n')
    with pytest.raises(ValidationError):
        load_semantic_cases(path)
```

- [ ] **Step 3: Implement the closed models and loaders**

Use these exact discriminators:

```python
Scope = Literal["user", "project", "working_state", "ignore"]
Variant = Literal[
    "ach_preprocess", "official_preprocess", "hybrid_preprocess",
    "ach_semantic", "native_semantic", "hybrid_semantic",
    "ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery",
    "ach_reliability", "official_reliability",
]

class ExpectedUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: str
    scope: Scope
    critical: bool
    current: bool
    required_literals: tuple[str, ...]
    forbidden_literals: tuple[str, ...] = ()

class SemanticCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    transcript: tuple[dict[str, str], ...]
    expected_units: tuple[ExpectedUnit, ...]
    secret_canaries: tuple[str, ...]
    repetitions: Literal[3] = 3

class DeliveryCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    task: str
    user_profile: dict[str, object]
    project_profile: dict[str, object]
    project_metadata: tuple[str, ...]
    working_state: dict[str, object] | None
    history_evidence: tuple[str, ...]
    expected_units: tuple[ExpectedUnit, ...]
    secret_canaries: tuple[str, ...]
    repetitions: Literal[3] = 3

class RunObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    variant: Variant
    repetition: int
    artifact_relpath: str
    hard_gate_flags: dict[str, bool]
    metric_values: dict[str, int | float | None]
    warning_codes: tuple[str, ...] = ()

class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate: str
    passed: bool
    failing_case_ids: tuple[str, ...] = ()

class ComponentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: Literal[
        "host_adapters", "preprocessing", "semantic_extractor", "scope_router",
        "profile_compiler", "delivery_protocol", "capture_reliability",
        "working_state_ordering",
    ]
    ruling: Literal[
        "keep", "replace_with_official", "simplify_to_hybrid",
        "add_follow_up_guard", "insufficient_evidence",
    ]
    approval_required: bool
    evidence_case_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
```

The delivery dictionaries are accepted only at the corpus boundary; `load_delivery_cases` immediately validates them with the production `UserProfile`, `ProjectProfile` and `WorkingState` models and rejects schema drift. Serialize with `model_dump(mode="json")`, sorted keys and compact separators before hashing. Artifact paths are run-relative, may not contain `..`, and point to redacted closed artifacts only.

- [ ] **Step 4: Add the 16 semantic cases**

The manifest contains these exact cases and failure purposes:

| ID | Required behavior |
|---|---|
| S01 | stated cross-project user preference reaches only User Bank |
| S02 | assistant proposal explicitly accepted by the human becomes confirmed project decision |
| S03 | permission to implement/test is Working State, never a confirmed durable decision |
| S04 | three changing proposals retain only the final settled value as current |
| S05 | one rejected alternative keeps its rationale but is never current |
| S06 | `do not auto-update dependencies` remains a negative constraint |
| S07 | observed non-obvious deployment gotcha keeps failure, cause and artifact reference |
| S08 | cheap repository fact remains evidence-only and out of active profile delivery |
| S09 | transient progress, test counts and greetings are ignored or Working State |
| S10 | explicit correction supersedes prior truth only in the same scope |
| S11 | personal review style mentioned during project work never enters Project Bank |
| S12 | one mixed message separates personal and project claims without copying either across banks |
| S13 | bearer token, private-key header, credential URL and assignment secret are all removed |
| S14 | replay of one slice does not duplicate evidence or advance Working State twice |
| S15 | repeated claim in two sessions remains one current claim with distinct-session support |
| S16 | agent inference without human confirmation never enters an active durable profile |

Use synthetic identifiers and canaries only. Every required exact value—such as `false`, `legacy`, `session_epoch`, a filename or a rejected option—appears in `required_literals` so deterministic scoring can verify it without semantic similarity.

- [ ] **Step 5: Add the 10 delivery cases**

Each case supplies a frozen User Profile, Project Profile, Project Metadata, Working State, relevant history, a user task and required/forbidden answer literals:

`D01-user-preference`, `D02-project-constraint`, `D03-working-state`, `D04-gotcha-cause`, `D05-superseded-choice`, `D06-negative-constraint`, `D07-budget-pressure`, `D08-irrelevant-anchoring`, `D09-long-tail-rationale`, and `D10-offline-last-good`.

The current-profile cases require no explicit recall. `D09` is the only case allowed to require history/recall. `D10` must expose cache age and may not claim freshness.

- [ ] **Step 6: Add the host preprocessing fixture**

The Claude JSONL contains normal user/assistant prose, thinking, meta, sidechain, compaction summary, tool use, tool result, a file read, a shell command, an injected-memory block and the four S13 canaries. Tests assert the raw fixture contains every canary before measuring their removal.

- [ ] **Step 7: Run the corpus gate**

Run:

```bash
rtk uv run pytest tests/test_memory_quality_corpus.py -q
rtk uv run ruff check experiments tests/test_memory_quality_corpus.py
rtk git diff --check
```

Expected: all pass; corpus digests are printed once and pinned in the test.

- [ ] **Step 8: Commit**

```bash
rtk git add .gitignore experiments tests/test_memory_quality_corpus.py
rtk git commit -m "test(quality): freeze the hindsight bakeoff corpus"
```

---

### Task 1: Build the disposable-bank-only Hindsight boundary

**Files:**

- Create: `experiments/memory_quality/hindsight.py`
- Create: `tests/test_memory_quality_upstream.py`

**Interfaces:**

- Consumes: `RunObservation` and the pinned corpus digest from Task 0.
- Produces: `BakeoffConfig.from_env(env: Mapping[str, str]) -> BakeoffConfig`
- Produces: `DisposableHindsight.create_bank(purpose: str) -> str`
- Produces: `DisposableHindsight.import_template(bank_id: str, template: dict) -> None`
- Produces: `DisposableHindsight.retain_and_wait(...) -> RetainReceipt`
- Produces: `DisposableHindsight.list_bank_objects(bank_id: str) -> BankSnapshot`
- Produces: `DisposableHindsight.cleanup() -> None`.

- [ ] **Step 1: Write refusal and registry tests first**

Pin these failures before any HTTP implementation:

```python
def test_live_config_requires_explicit_disposable_authority():
    env = bakeoff_env()
    env.pop("HINDSIGHT_BAKEOFF_CONFIRM")
    with pytest.raises(BakeoffRefused):
        BakeoffConfig.from_env(env)


def test_missing_url_defaults_to_the_local_service():
    env = bakeoff_env()
    env.pop("HINDSIGHT_BAKEOFF_URL")
    assert BakeoffConfig.from_env(env).base_url == "http://127.0.0.1:8888"


def test_non_loopback_production_url_is_rejected():
    env = bakeoff_env()
    env["HINDSIGHT_BAKEOFF_URL"] = "https://hindsight.example.invalid"
    env["MEMORY_HINDSIGHT_URL"] = env["HINDSIGHT_BAKEOFF_URL"]
    with pytest.raises(BakeoffRefused, match="production URL"):
        BakeoffConfig.from_env(env)


def test_confirmed_loopback_service_is_allowed():
    env = bakeoff_env(url="http://127.0.0.1:8888")
    assert BakeoffConfig.from_env(env).base_url == "http://127.0.0.1:8888"

```

Add a cleanup test whose registry contains one valid current-run ID and one foreign ID; `cleanup()` must raise before `respx_mock.calls` records any DELETE. Also assert no source file under `experiments/memory_quality/` imports `memory.db`, `memory.models` or `memory.api`, and that no experiment calls `memory.config.get_settings()` or `memory.hindsight.client.get_client()`. Pure production functions may be imported; clients and inputs are always constructed explicitly.

- [ ] **Step 2: Implement explicit configuration**

Use a frozen dataclass with exactly:

```python
@dataclass(frozen=True)
class BakeoffConfig:
    base_url: str
    api_token: str | None
    tenant: str
    run_id: uuid.UUID
    request_timeout_seconds: float = 30.0
    operation_timeout_seconds: float = 180.0
```

For its own values, `from_env` accepts only `HINDSIGHT_BAKEOFF_URL` (default `http://127.0.0.1:8888`), `HINDSIGHT_BAKEOFF_TOKEN`, `HINDSIGHT_BAKEOFF_TENANT`, `HINDSIGHT_BAKEOFF_CONFIRM` and an optional test-provided run ID. It reads `MEMORY_HINDSIGHT_URL` only for the loopback/staging refusal check. Normalize hostnames and resolved IPs before applying that check; `localhost`, `127.0.0.1` and `::1` are the only implicit allow-list.

- [ ] **Step 3: Pin Hindsight 0.9.2 before bank mutation**

Fetch `/openapi.json`, require `info.version == "0.9.2"`, and store its SHA-256 in the run manifest. No create/import/retain request may occur if the version check fails.

- [ ] **Step 4: Implement the bank registry and cleanup fence**

Generate IDs only through:

```python
def bank_id(run_id: uuid.UUID, purpose: str, ordinal: int) -> str:
    safe = re.sub(r"[^a-z0-9-]", "-", purpose.casefold()).strip("-")
    return f"mq55-{run_id.hex[:12]}-{safe}-{ordinal:03d}"
```

Append each successfully created ID atomically to `.artifacts/memory-quality/<run_id>/banks.json`. `cleanup()` reads that exact file, validates every prefix again, deletes each bank once and records the response. A malformed or foreign ID aborts cleanup before the first DELETE.

- [ ] **Step 5: Add bounded operation polling and snapshots**

`retain_and_wait` supplies deterministic `document_id` and UUIDv5 `operation_id`, polls terminal operation state until the configured deadline, and never retries a failed semantic operation under a different ID. `list_bank_objects` returns documents, memories, observations, mental models and Knowledge Pages in a closed model; unknown fields are discarded.

Use these result contracts from `contracts.py`:

```python
class RetainReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    operation_id: str
    terminal_state: Literal["completed", "failed"]
    duration_ms: int

class SnapshotObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer: Literal["document", "memory", "observation", "mental_model", "page"]
    object_id: str
    text: str
    source_ids: tuple[str, ...] = ()

class BankSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bank_id: str
    objects: tuple[SnapshotObject, ...]
```

`BankSnapshot` is an in-memory scoring input. Persisted observations replace `bank_id` with the closed variant/scope label and never serialize raw `text`; only the sealed blind packet may contain synthetic output text.

- [ ] **Step 6: Run and commit**

```bash
rtk uv run pytest tests/test_memory_quality_upstream.py -q
rtk uv run ruff check experiments tests/test_memory_quality_upstream.py
rtk git diff --check
rtk git add experiments/memory_quality/hindsight.py tests/test_memory_quality_upstream.py
rtk git commit -m "test(quality): fence disposable hindsight banks"
```

---

### Task 2: Invoke the pinned official runtime without adopting it

**Files:**

- Create: `experiments/memory_quality/upstream.py`
- Create: `experiments/memory_quality/official_bridge.ts`
- Create: `tests/test_memory_quality_official_runtime.py`

**Interfaces:**

- Consumes: `HINDSIGHT_CODING_AGENTS_DIR` and the fixed hashes in this plan.
- Produces: `verify_official_source(root: Path) -> OfficialSource`
- Produces: `OfficialRuntime.normalize_claude(path: Path) -> tuple[NormalizedTurn, ...]`
- Produces: `OfficialRuntime.retain_claude(event: dict, env: Mapping[str, str]) -> RuntimeReceipt`.

- [ ] **Step 1: Write source drift tests**

Copy no upstream code. In a temporary fake tree, assert missing file, wrong package version and one-byte hash drift all fail before Node starts. Assert the real sibling checkout passes when present.

- [ ] **Step 2: Implement the source verifier**

`verify_official_source` resolves the root, rejects symlinks escaping it, validates package version `0.5.1`, validates the ten hashes in **Fixed Upstream Baseline**, and requires `git diff --quiet c61c4e7d7 -- hindsight-integrations/coding-agents/src hindsight-integrations/coding-agents/package.json hindsight-integrations/coding-agents/package-lock.json`. Record the enclosing checkout commit, but do not require the whole repository HEAD to equal the release commit when that package subtree is byte-identical.

Return these closed bridge contracts:

```python
@dataclass(frozen=True)
class OfficialSource:
    root: Path
    package_version: Literal["0.5.1"]
    release_commit: Literal["c61c4e7d7"]
    checkout_commit: str
    sha256_by_relpath: Mapping[str, str]

class NormalizedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "tool", "meta"]
    text: str
    source_start: int | None = None
    source_end: int | None = None

class RuntimeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    turn_count: int
    error_code: str | None = None
```

- [ ] **Step 3: Write the narrow TypeScript bridge**

The bridge reads one closed JSON command from stdin:

```ts
type Command =
  | { op: "normalize-claude"; transcriptPath: string; sourceRoot: string }
  | {
      op: "retain-claude";
      transcriptPath: string;
      sourceRoot: string;
      bankId: string;
      apiUrl: string;
      apiToken?: string;
      sessionId: string;
    };
```

For normalization, dynamically import `src/core/transcript.ts`, call `readClaudeTranscript`, and emit `{turns}`. For retain, import `readClaudeTranscript` from `src/core/transcript.ts`, `retainLiveSession` from `src/core/chat.ts`, and `HindsightClient` from `src/core/hindsight.ts`. Supply a bridge-local `RetainCursorStore` backed by a `Map`, then emit only `{ok, turnCount}`. Reject any bank ID without the current `mq55-<run_id>-` prefix supplied separately by the Python parent.

- [ ] **Step 4: Implement the Python subprocess boundary**

Invoke without a shell:

```python
command = [
    "npm", "exec", "--yes", "--package=tsx@4.20.6", "--",
    "tsx", str(BRIDGE),
]
subprocess.run(
    command,
    input=json.dumps(payload),
    text=True,
    capture_output=True,
    timeout=90,
    check=False,
    env=minimal_environment(env),
)
```

The minimal environment contains `PATH`, temporary `HOME`, proxy/CA variables needed for the selected isolated endpoint, and no `MEMORY_*`, `ACH_MEMORY_*`, provider key or unrelated Hindsight variables. Pass the one bake-off API token only inside stdin; redact stderr before storing a failure.

- [ ] **Step 5: Prove the bridge cannot configure ambient hosts**

Tests use a temporary HOME and assert no `.claude`, `.codex`, `.hindsight` or repo config file appears. The bridge never invokes the official installer, SessionStart, auto-update, git ingest or survey code.

- [ ] **Step 6: Run and commit**

```bash
rtk uv run pytest tests/test_memory_quality_official_runtime.py -q
rtk uv run ruff check experiments tests/test_memory_quality_official_runtime.py
rtk git diff --check
rtk git add experiments/memory_quality/upstream.py experiments/memory_quality/official_bridge.ts tests/test_memory_quality_official_runtime.py
rtk git commit -m "test(quality): bind the pinned coding agents runtime"
```

---

### Task 3: Compare preprocessing and semantic extraction independently

**Files:**

- Create: `experiments/memory_quality/preprocessing.py`
- Create: `experiments/memory_quality/semantic.py`
- Create: `tests/test_memory_quality_preprocessing.py`
- Create: `tests/test_memory_quality_semantic.py`

**Interfaces:**

- Consumes: Task 0 corpus, Task 1 disposable client, Task 2 official runtime.
- Produces: `run_preprocessing(case_path: Path, runtime: OfficialRuntime) -> tuple[RunObservation, ...]`
- Produces: `run_semantic_case(case: SemanticCase, variant: SemanticVariant, repetition: int) -> RunObservation`
- Produces: `split_minimal(content: str, client: HindsightClient) -> MinimalProjection`.

- [ ] **Step 1: Pin preprocessing observations with fake adapters**

Assert all three variants report byte count, turn/record count, retained role count, tool-action count, source-span count, every canary presence flag and duration. The observation schema contains no raw transcript or free-form tool output.

- [ ] **Step 2: Implement the three preprocessing variants**

`ach_preprocess` calls the production pure functions without invoking `checkpoint`. `official_preprocess` calls Task 2's bridge. `hybrid_preprocess` converts official turns into the same text-block policy as `memory.capture.local`: redact prose, drop tool inputs/file bodies and preserve only bounded non-file error/status evidence. Do not change `src/memory/capture/local.py`.

- [ ] **Step 3: Add preprocessing hard gates**

Fail a variant on any planted canary after preprocessing. Separately report lost required utterance literals, tool noise and lack of raw-span provenance. A missing provenance capability is not a secret failure; it is a named trade-off for the later decision.

- [ ] **Step 4: Define the semantic adapter protocol**

```python
class SemanticVariant(Protocol):
    name: Literal["ach_semantic", "native_semantic", "hybrid_semantic"]

    def run(
        self, *, case: SemanticCase, repetition: int, banks: DisposableHindsight
    ) -> SemanticOutput: ...
```

`SemanticOutput` contains only normalized one-claim records, Working State, usage, duration and a snapshot of which scope holds each resulting object.

```python
class NormalizedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    scope: Literal["user", "project"]
    current: bool
    source_ids: tuple[str, ...]

class SemanticOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: tuple[NormalizedClaim, ...]
    working_state: WorkingStateEnvelope | None
    document_scopes: tuple[Literal["user", "project", "shared"], ...]
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
```

`shared` is observable only for `native_semantic`'s one-bank baseline and automatically fails the dual-bank isolation responsibility; hybrid and ach outputs may use only `user` or `project`.

- [ ] **Step 5: Implement `ach_semantic` without the worker**

Call `capture.extractor.extract` with the canonical sanitized content, then `capture.classifier.classify`. Serialize the exact candidates and Working State that `capture.worker` would persist, but do not call `file_candidates`, mutate Working State or write an ach-memory row.

- [ ] **Step 6: Implement `native_semantic` on one disposable bank**

Import the pinned official conversation retain strategy and mission into a new bank, retain the canonical transcript once with its case/repetition document ID, await completion/consolidation, then snapshot documents, raw facts and observations. Do not create Pages in the semantic experiment.

- [ ] **Step 7: Implement the minimal hybrid splitter**

Use one `dry-run-extract` call with a closed response schema:

```python
class ProjectedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    bank: Literal["user", "project"]
    provenance: str

class MinimalProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: tuple[ProjectedClaim, ...]
    working_state: WorkingStateCandidate | None
```

The prompt permits no `kind`, `origin`, eligibility, confidence, ranking or importance fields. It requires one claim per record, impersonal project phrasing, no cross-scope content and provenance copied from code-owned raw-span markers. Retain the user/project claim lists into separate disposable banks under the official conversation strategy; never retain `working_state`.

- [ ] **Step 8: Normalize all three outputs for blind comparison**

Map Hindsight `world`, `experience` and `observation` payloads into `{text, scope, current, source_ids}`. Preserve the document snapshot separately so physical-isolation gates inspect documents as well as extracted facts.

- [ ] **Step 9: Test repeat/run isolation**

Every `(case, variant, repetition)` gets fresh banks. Tests assert document IDs, bank IDs and operation IDs cannot collide across variants or runs, and failure in one run cannot be retried into another run's bank.

- [ ] **Step 10: Run and commit**

```bash
rtk uv run pytest tests/test_memory_quality_preprocessing.py tests/test_memory_quality_semantic.py -q
rtk uv run ruff check experiments tests/test_memory_quality_preprocessing.py tests/test_memory_quality_semantic.py
rtk git diff --check
rtk git add experiments/memory_quality/preprocessing.py experiments/memory_quality/semantic.py tests/test_memory_quality_preprocessing.py tests/test_memory_quality_semantic.py
rtk git commit -m "test(quality): compare extraction by component"
```

---

### Task 4: Compare the actual delivered contexts

**Files:**

- Create: `experiments/memory_quality/delivery.py`
- Create: `tests/test_memory_quality_delivery.py`

**Interfaces:**

- Consumes: `DeliveryCase`, disposable banks and current `memory.brief`/`memory.profiles` pure functions.
- Produces: `build_delivery(case: DeliveryCase, variant: DeliveryVariant) -> DeliveredArtifact`
- Produces: `run_consumer(artifact: DeliveredArtifact, command: Sequence[str]) -> ConsumerAnswer`.
- Produces: `measure_legacy_brief(scope: Literal["user", "project"], project_slug: str | None) -> LegacyCalibration`.

- [ ] **Step 1: Write delivered-artifact tests**

Pin this closed result:

```python
class DeliveredArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    variant: Literal[
        "ach_index", "ach_full", "official_reflect", "official_pages", "hybrid_delivery"
    ]
    context: str
    context_token_upper_bound: int
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    source_ids: tuple[str, ...]
    stale: bool

class LegacyCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_version: str
    scope: Literal["user", "project"]
    status_code: int
    latency_ms: int
    byte_count: int
    context_token_upper_bound: int
    recognized_sections: tuple[
        Literal["user_core", "project_metadata", "project_profile", "working_state", "memory_protocol"],
        ...,
    ]

class ConsumerAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    used_source_ids: tuple[str, ...]
    abstained: bool
```

The artifact may contain only synthetic corpus content. It may not contain a bank ID, API token, raw upstream metadata or secret canary.

- [ ] **Step 2: Implement current compiler variants**

Construct `Section`, `Orientation` and Working State inputs directly from each frozen fixture, pass them to `compose_index`/`compose_full`, and assert the artifact text is byte-for-byte the host-delivered tier—not a pre-budget intermediate profile.

- [ ] **Step 3: Implement official reflect and page variants**

Seed fresh disposable banks with the case's frozen evidence, configure the official mission, and create only the Pages named by the fixture. `official_reflect` makes one low-budget reflect using the user task. `official_pages` searches pages with the same task and reads only the top result. Record time/tokens and fail the run on timeout; do not silently substitute recall.

- [ ] **Step 4: Implement the hybrid delivery variant**

Reuse the current compiler only for its mandatory deterministic core: User Core, Project Metadata and Working State. Exclude optional project-profile lines, then append the top relevant Page result within the same Full-tier token budget. If the page result does not fit as a whole, omit it and set a closed `PAGE_DID_NOT_FIT` warning; never cut a claim mid-line.

- [ ] **Step 5: Add deterministic payload scoring**

For each artifact, check required/forbidden literals, negative wording, stale marker, budget and whether `D09` alone needs long-tail retrieval. These checks run even when no consumer command exists.

- [ ] **Step 6: Add an optional provider-neutral consumer boundary**

Parse `MEMORY_BAKEOFF_CONSUMER_CMD` as a JSON array, never a shell string. Send `{case_id, task, context}` on stdin and require:

```json
{
  "answer": "synthetic answer text",
  "used_source_ids": ["source-1"],
  "abstained": false
}
```

Run with a 120-second timeout, a temporary HOME and no ach-memory/Hindsight credentials. Tests use a tiny fake executable and prove timeout, malformed JSON and nonzero exit produce closed failure codes.

- [ ] **Step 7: Blind variant identity for consumer runs**

Use a run-local HMAC to map variant names to opaque labels. The consumer packet and adjudication file contain the opaque label only; the mapping remains in the private run manifest until scoring is complete.

- [ ] **Step 8: Add a narrow read-only legacy calibration**

If `ACH_MEMORY_URL` and `ACH_MEMORY_API_KEY` are present, expose a calibration function that may call only `GET /openapi.json` and `GET /v1/session-brief`. The operator supplies `scope` and, for project scope, `project_slug`; the code never enumerates projects or memories. Parse the brief in memory, derive `LegacyCalibration`, discard the response object and never write, log, adjudicate or forward its content. A transport spy test fails if the adapter emits a non-GET request or any path outside that two-item allow-list. This calibration is observational and cannot select a winning variant.

- [ ] **Step 9: Run and commit**

```bash
rtk uv run pytest tests/test_memory_quality_delivery.py -q
rtk uv run ruff check experiments tests/test_memory_quality_delivery.py
rtk git diff --check
rtk git add experiments/memory_quality/delivery.py tests/test_memory_quality_delivery.py
rtk git commit -m "test(quality): compare delivered memory context"
```

---

### Task 5: Make reliability claims exact with fault injection

**Files:**

- Create: `experiments/memory_quality/reliability.py`
- Create: `tests/test_memory_quality_reliability.py`

**Interfaces:**

- Consumes: current local checkpoint/worker functions and Task 2 official runtime.
- Produces: `run_fault_scenario(variant: ReliabilityVariant, fault: Fault) -> ReliabilityResult`
- Produces: `reliability_matrix() -> tuple[ReliabilityExpectation, ...]`.

- [ ] **Step 1: Encode the exact fault matrix**

Use these expected outcome enums:

```python
Fault = Literal[
    "death_before_send", "death_waiting_for_ack", "lost_ack_after_commit", "rate_limited",
    "hindsight_offline_after_ack", "worker_death_after_extract",
    "worker_death_after_retain", "expired_lease", "older_checkpoint",
    "no_future_host_event", "future_host_event",
]
Recovery = Literal[
    "completed", "recovered_later", "requires_future_event",
    "unrecoverable_without_outbox", "rejected_as_stale",
]

ReliabilityVariant = Literal["ach_reliability", "official_reliability"]

class ReliabilityExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: ReliabilityVariant
    fault: Fault
    expected: Recovery

class ReliabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variant: ReliabilityVariant
    fault: Fault
    observed: Recovery
    requests: int
    duplicate_objects: int
    final_cursor_state: str
```

The baseline expectations state explicitly:

- ach-memory post-202 worker failures recover autonomously;
- ach-memory pre-202 failures preserve the cursor but require a future hook event;
- official dirty cursor/lost ACK recovers on a future invocation;
- neither implementation creates a future invocation by itself;
- an older Working State pair is rejected.

- [ ] **Step 2: Reuse production boundaries without editing them**

Drive `memory.capture.local.checkpoint`, `capture.repository` and `capture.worker.run_once` through injected HTTP responses and crash callbacks. Never add experiment flags or branches to production modules.

- [ ] **Step 3: Drive the actual official writeback path**

Use Task 2's bridge with a temporary cursor directory and controlled HTTP endpoint. Kill only the spawned bridge process, never a shared Hindsight process. Verify deterministic operation IDs, dirty-to-replace behavior and dependence on a later invocation.

- [ ] **Step 4: Test lost ACK after server commit**

The fake endpoint records the operation, closes the response before the client receives it, then returns the original terminal result on the identical operation ID. Assert no duplicate document/fact is created and classify recovery separately from a failure before the server committed.

- [ ] **Step 5: Measure the pre-ACK gap honestly**

Run a Stop failure with no second Stop/PreCompact/session event. Assert the ach cursor remains unchanged and the official cursor remains dirty, but neither system makes another request during the observation window. Record `unrecoverable_without_outbox`; do not fail the bake-off merely because both implementations share it.

- [ ] **Step 6: Test explicit versus inferred Working State as a finding, not a new rule**

Create an explicit handoff at pair `(epoch, seq)`, then automatic candidates below, equal to and above it. Existing lexicographic behavior must reject below, treat equal-different as conflict and accept above. Record whether accepting above revives stale inferred text. Do not add `source_priority` in this phase; the final report decides whether a follow-up design is required.

- [ ] **Step 7: Run and commit**

```bash
rtk uv run pytest tests/test_memory_quality_reliability.py tests/test_capture_local.py tests/test_capture_worker.py tests/test_working_state.py -q
rtk uv run ruff check experiments tests/test_memory_quality_reliability.py
rtk git diff --check
rtk git add experiments/memory_quality/reliability.py tests/test_memory_quality_reliability.py
rtk git commit -m "test(quality): measure checkpoint recovery boundaries"
```

---

### Task 6: Score blind results and issue component rulings

**Files:**

- Create: `experiments/memory_quality/scoring.py`
- Create: `experiments/memory_quality/adjudication.example.json`
- Create: `tests/test_memory_quality_scoring.py`

**Interfaces:**

- Consumes: all `RunObservation` and optional `ConsumerAnswer` records.
- Produces: `build_blind_packet(observations: Sequence[RunObservation], key: bytes) -> BlindPacket`
- Produces: `score_run(packet: BlindPacket, adjudication: Adjudication) -> Scorecard`
- Produces: `decide(scorecard: Scorecard) -> tuple[ComponentDecision, ...]`.

- [ ] **Step 1: Write hard-gate scoring tests**

One planted secret, one cross-bank document, one missed critical decision, one revived critical proposal or one post-ACK non-recovery must make the relevant variant ineligible. A known pre-ACK/no-future-event gap must be reported but not mislabeled as post-ACK failure.

- [ ] **Step 2: Define the closed adjudication format**

```json
{
  "run_id": "00000000-0000-0000-0000-000000000000",
  "items": [
    {
      "case_id": "S01",
      "blind_variant": "V-a1b2",
      "repetition": 1,
      "required_units_met": ["S01-U1"],
      "unsupported_current_claims": 0,
      "wrong_scope_claims": 0,
      "notes_code": "NONE"
    }
  ]
}
```

`notes_code` is one of `NONE`, `PARAPHRASE_ACCEPTED`, `AMBIGUOUS_OUTPUT`, or `ADJUDICATION_BLOCKED`. No variant name appears until the packet is sealed.

`BlindPacket`, `Adjudication` and `Scorecard` are closed Pydantic models, not free-form dictionaries. `BlindPacket` contains the corpus/run digest and sorted `(case_id, blind_variant, repetition, artifact_relpath)` items. `Adjudication` contains exactly the JSON fields above and rejects missing or duplicate items. `Scorecard` contains sorted per-variant `GateResult` records, named non-critical misses, median/p95 latency and token totals, `non_inferior: bool`, and `approval_required: bool`; it contains no HMAC key or blind-to-real mapping.

- [ ] **Step 3: Implement deterministic checks before manual adjudication**

Exact literals, forbidden literals, scopes, object locations, current flags, budgets, secrets, retries and fault outcomes are machine-scored. Manual adjudication is allowed only for whether a paraphrase satisfies an expected semantic unit; it cannot override a secret, isolation, authorization, ordering or durability failure.

- [ ] **Step 4: Implement case-level non-inferiority**

Compare each challenger with the current implementation by named case over all three repetitions. Mark `non_inferior=false` on two repeatable additional non-critical misses. Mark one additional miss as `approval_required`; do not convert 16 cases into a misleading percentage margin.

If `MEMORY_BAKEOFF_CONSUMER_CMD` is absent, fails, or lacks three complete runs for any delivery case, deterministic payload scores remain reportable but `profile_compiler` and `delivery_protocol` must be `insufficient_evidence` for replacement or removal. They may still receive `keep` when a challenger fails a payload hard gate.

- [ ] **Step 5: Produce eight component decisions**

Exactly one decision must be emitted for each component:

```text
host_adapters
preprocessing
semantic_extractor
scope_router
profile_compiler
delivery_protocol
capture_reliability
working_state_ordering
```

Use ruling values `keep`, `replace_with_official`, `simplify_to_hybrid`, `add_follow_up_guard`, or `insufficient_evidence`. Each decision names the case IDs and hard gates supporting it. `insufficient_evidence` never authorizes removal or production activation.

- [ ] **Step 6: Test blindness and stable reports**

Changing the HMAC key changes labels but not scores; shuffled input produces byte-identical sorted scorecards; reports never contain API tokens, bank IDs, raw host paths or secret canaries.

- [ ] **Step 7: Run and commit**

```bash
rtk uv run pytest tests/test_memory_quality_scoring.py -q
rtk uv run ruff check experiments tests/test_memory_quality_scoring.py
rtk git diff --check
rtk git add experiments/memory_quality/scoring.py experiments/memory_quality/adjudication.example.json tests/test_memory_quality_scoring.py
rtk git commit -m "test(quality): rule on memory components by case"
```

---

### Task 7: Orchestrate the bake-off and record the architectural gate

**Files:**

- Create: `experiments/memory_quality/runner.py`
- Create: `tests/test_memory_quality_runner.py`
- Create: `docs/results/2026-09-02-memory-quality-phase-5-5.md`
- Modify: `docs/plans/2026-08-29-memory-quality-program.md`

**Interfaces:**

- Consumes: Tasks 0–6.
- Produces: `python -m experiments.memory_quality.runner preflight|legacy-calibrate|run|score|cleanup|report`
- Produces: `.artifacts/memory-quality/<run_id>/manifest.json`, `observations.jsonl`, `blind-packet.json`, `scorecard.json`, `decisions.json`, and `report.md`.

- [ ] **Step 1: Write orchestration tests before the CLI**

Test that `preflight` makes no mutating request; `legacy-calibrate` permits only the two allow-listed GETs and refuses without `ACH_MEMORY_BAKEOFF_CONFIRM=read-only`; `run` refuses without confirmation; a failed run still writes its registry; `score` refuses incomplete three-repeat data; `cleanup` touches only registered disposable banks; and `report` refuses any decision set missing one of the eight named components.

- [ ] **Step 2: Implement atomic artifact writes and provenance**

Every artifact is written to a sibling temporary file, flushed, fsynced and atomically replaced. `manifest.json` records ach-memory commit, dirty flag, Hindsight API version/OpenAPI hash, official source hashes/commit, corpus hashes, model identifiers, budgets, start/end timestamps and command modes. It stores no token, bank ID or synthetic claim content.

- [ ] **Step 3: Implement six explicit subcommands**

- `preflight`: validate clean ach-memory tree, versions, source hashes, corpus hashes, consumer command shape and isolated endpoint; create no bank.
- `legacy-calibrate`: require explicit scope/project arguments and `ACH_MEMORY_BAKEOFF_CONFIRM=read-only`, call only the legacy OpenAPI and session-brief GETs, then write only the closed content-free calibration record.
- `run`: create disposable banks and run A–D; always attempt fenced cleanup in `finally`, retaining the registry and cleanup outcomes.
- `score`: require complete observations and a completed adjudication file, then seal/unblind once.
- `cleanup`: retry cleanup only for registry entries not already confirmed deleted.
- `report`: render the fixed summary tables and decision evidence; never infer a ruling missing from `decisions.json`.

- [ ] **Step 4: Run all mocked gates before spending tokens**

```bash
rtk uv run pytest tests/test_memory_quality_corpus.py tests/test_memory_quality_upstream.py tests/test_memory_quality_official_runtime.py tests/test_memory_quality_preprocessing.py tests/test_memory_quality_semantic.py tests/test_memory_quality_delivery.py tests/test_memory_quality_reliability.py tests/test_memory_quality_scoring.py tests/test_memory_quality_runner.py -q
rtk uv run ruff check experiments tests
rtk git diff --check
```

- [ ] **Step 5: Run preflight against the selected isolated Hindsight**

```bash
rtk uv run python -m experiments.memory_quality.runner preflight
```

Expected: exact versions/hashes, two corpus digests, selected model/budgets, production-URL inequality and `mutating_requests: 0`.

- [ ] **Step 6: Execute the measured run**

With the explicit bake-off environment set by the operator:

```bash
rtk uv run python -m experiments.memory_quality.runner run
```

Expected: 16 semantic cases × 3 semantic variants × 3 repetitions, 10 delivery cases × 5 delivery variants × 3 repetitions, the three preprocessing outputs, and the full reliability matrix. All created banks are either deleted or listed as cleanup failures in the registry.

- [ ] **Step 7: Complete blinded adjudication and score once**

Copy `adjudication.example.json` into the run artifact directory, add one closed item for every blind semantic/consumer output, and validate it before unblinding:

```bash
rtk uv run python -m experiments.memory_quality.runner score
rtk uv run python -m experiments.memory_quality.runner report
```

No architecture decision is made from partial repetitions or an `ADJUDICATION_BLOCKED` critical case; the component receives `insufficient_evidence`.

- [ ] **Step 8: Write the durable results document**

`docs/results/2026-09-02-memory-quality-phase-5-5.md` records:

- exact commits, versions, models, budgets and corpus hashes;
- pass/fail by named hard gate and case;
- semantic misses and accepted paraphrases by blind output ID;
- median/p95 latency and token totals;
- pre-ACK versus post-ACK recovery matrix;
- all eight component rulings and the evidence case IDs;
- disposable-bank cleanup result;
- the next authorized action and rollback boundary.

Do not copy raw transcripts, bank IDs, secrets, credentials or unrestricted model output into the document.

- [ ] **Step 9: Update the HLD only from final rulings**

Insert Phase 5.5 between Phase 5 and Phase 0. Record its commits, exact gate counts and one of:

- current architecture retained;
- hybrid follow-up approved for planning;
- official component replacement approved for planning;
- insufficient evidence, no architectural change.

State that the SPEC product invariants remain authoritative, no winning variant was integrated, no production bank/config was touched, all activation flags remain off and Phase 0 still needs separate approval.

- [ ] **Step 10: Run repository verification**

```bash
rtk uv run pytest -q
rtk uv run ruff check src experiments tests
rtk git diff --check
rtk alembic heads
rtk git status --short
```

The full suite must match or improve the Phase 5 baseline. Live tests may report only their already documented external prerequisites; no experiment failure may be relabeled as an unavailable integration.

- [ ] **Step 11: Commit the harness and measured decision separately**

```bash
rtk git add experiments/memory_quality/runner.py tests/test_memory_quality_runner.py
rtk git commit -m "test(quality): orchestrate the hindsight bakeoff"
rtk git add docs/results/2026-09-02-memory-quality-phase-5-5.md docs/plans/2026-08-29-memory-quality-program.md
rtk git commit -m "docs(quality): record the hindsight bakeoff decision"
```

## Completion Criteria

Phase 5.5 is complete only when:

- Phase 5 is integrated and the bake-off started from a clean commit;
- the corpus has exactly 16 semantic and 10 delivery cases, each repeated three times;
- Hindsight 0.9.2 and coding-agents 0.5.1 source identity are pinned and recorded;
- preprocessing, semantic extraction, delivered context and reliability are measured independently;
- all hard gates are evaluated against documents as well as derived memories/context;
- the local Hindsight 0.9.2 service is exercised with disposable banks; any legacy ach-memory calibration is read-only, content-free and excluded from winner selection;
- the pre-ACK gap is distinguished from post-ACK durability;
- semantic ambiguity is adjudicated blind and cannot override safety gates;
- all eight component rulings cite named cases and use `insufficient_evidence` when incomplete;
- every disposable bank is deleted or its cleanup failure is explicitly recorded;
- no production code, database, deployment default, bank, model, memory or activation flag changed;
- the HLD records the decision and Phase 0 remains separately gated;
- full pytest, Ruff, diff check and Alembic head verification pass.

## Explicit Deferrals

- Replacing or deleting any ach-memory production component.
- Adding a local outbox, daemon or transcript reconciliation scanner.
- Adding `source_priority` or changing Working State ordering semantics.
- Switching profiles from structured mental models to Knowledge Pages.
- Removing INDEX/FULL or changing host delivery.
- Adding automatic reflect/Q&A.
- Adopting the official npm package as a runtime dependency.
- Expanding capture to additional hosts.
- Running Phase 0 or mutating any production Hindsight object.
