# Memory Quality Phase 5.6 Semantic Baseline Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a valid ACH semantic baseline by making the Hindsight-compatible experiment mission semantically identical to the production extraction contract, then repeat and adjudicate only the 144-item semantic comparison.

**Architecture:** Split the production extraction prompt into one shared semantic-rule body and two transport envelopes: production JSONL and Hindsight's single outer JSON object. Run a new semantic-only experiment in fresh disposable banks; do not reuse or rewrite the frozen Phase 5.5 evidence. Accept production semantic changes only if the repaired baseline still fails named gates.

**Tech Stack:** Python 3.12, Pydantic 2, pytest, Hindsight 0.9.2, `hindsight-coding-agents` 0.5.1, JSONL, external HMAC custody.

**Spec:** `docs/specs/2026-08-29-memory-quality-v1.4.md` (§5–§9); evidence boundary: `docs/results/2026-09-02-memory-quality-phase-5-5.md`.

## Global Constraints

- Do not modify `memory.capture.classifier` or any production semantic rule in this phase. The defect is experiment prompt drift until a repaired run proves otherwise.
- Preserve run `36b90ba4-9644-4bfa-a956-f4a350d0d86c` and every V1/V2/V3 artifact byte-for-byte.
- Run only the 16 semantic cases × 3 variants × 3 repetitions = 144 observations. Do not rerun preprocessing, delivery or reliability.
- Use Hindsight 0.9.2 and `hindsight-coding-agents` 0.5.1 with the existing pinned source hashes.
- Every live write is confined to fresh `mq55-` disposable banks whose prefix is derived from the new run UUID and registered by `DisposableHindsight`; cleanup is mandatory.
- Require `HINDSIGHT_BAKEOFF_CONFIRM=disposable-banks-only`, an external random HMAC key and the existing loopback/allow-list checks.
- Keep `MEMORY_CAPTURE_ENABLED`, `MEMORY_CAPTURE_WORKER_ENABLED`, `MEMORY_CAPTURE_CORRECTION_REFRESH_ENABLED` false and `MEMORY_PROFILE_DELIVERY_MODE=legacy`.
- Never serialize raw transcript text, canaries, HMAC material, the blind mapping or bank IDs into score/report artifacts.
- `scope="ignore"` units are forbidden-output expectations: they are never counted as missing, but an unsupported current claim in a critical rejection case remains a hard failure.
- No production or HLD architecture replacement follows automatically. The final result document records the measured ruling.

---

### Task 0: Share the semantic rule body across both transports

**Files:**
- Modify: `src/memory/capture/extractor.py`
- Modify: `experiments/memory_quality/semantic.py`
- Modify: `tests/test_capture_extractor.py`
- Modify: `tests/test_memory_quality_semantic.py`

**Interfaces:**
- Produces: `EXTRACTION_RULES: str`
- Produces: `build_extraction_prompt(transport: Literal["jsonl", "hindsight_object"]) -> str`
- Preserves: `EXTRACTION_PROMPT`, now equal to `build_extraction_prompt("jsonl")`
- Consumes: `_AchHindsightAdapter.dry_run_extract(...)`

- [ ] **Step 1: Write failing parity tests**

Add a parametrized test that asserts both transport prompts contain the same required semantic sentences and that only their response-envelope clauses differ:

```python
@pytest.mark.parametrize("rule", (
    "Plans, proposals, research output and next steps are Working State",
    "Permission to execute, test or explore a proposal is authorization",
    "Repository-local wording",
    "Preserve a negative constraint",
    "Use observed only when provenance names the exact transcript span",
    "At most one working_state object total",
))
def test_both_extraction_transports_share_every_semantic_rule(rule):
    assert rule.casefold() in build_extraction_prompt("jsonl").casefold()
    assert rule.casefold() in build_extraction_prompt("hindsight_object").casefold()


def test_transport_envelopes_do_not_leak_into_each_other():
    jsonl = build_extraction_prompt("jsonl")
    hindsight = build_extraction_prompt("hindsight_object")
    assert "one minified JSON object per line" in jsonl
    assert 'one outer object shaped like {"facts"' not in jsonl
    assert 'one outer object shaped like {"facts"' in hindsight
    assert "one minified JSON object per line" not in hindsight
```

Extend the adapter test to assert that the delegate receives the Hindsight transport prompt and that a caller-supplied incompatible mission cannot replace it.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
pytest tests/test_capture_extractor.py tests/test_memory_quality_semantic.py -q
```

Expected: FAIL because `EXTRACTION_RULES` and `build_extraction_prompt` do not exist and the compatible mission lacks the rules.

- [ ] **Step 3: Extract one shared rule body and two envelopes**

Move every semantic instruction currently in `EXTRACTION_PROMPT` into `EXTRACTION_RULES`. Implement:

```python
ExtractionTransport = Literal["jsonl", "hindsight_object"]


def build_extraction_prompt(transport: ExtractionTransport) -> str:
    if transport == "jsonl":
        envelope = JSONL_ENVELOPE
    elif transport == "hindsight_object":
        envelope = HINDSIGHT_OBJECT_ENVELOPE
    else:
        raise ValueError(f"unsupported extraction transport: {transport}")
    return f"{EXTRACTION_RULES}\n\n{envelope}"


EXTRACTION_PROMPT = build_extraction_prompt("jsonl")
```

The Hindsight envelope must request `{"facts":[{"what":"<escaped ACH envelope>", ...}]}` while preserving the exact candidate and Working State schemas from the JSONL envelope. In `semantic.py`, delete `_HINDSIGHT_COMPATIBLE_ACH_MISSION` and set:

```python
options["retain_mission"] = build_extraction_prompt("hindsight_object")
```

- [ ] **Step 4: Run focused and prompt-contract tests**

Run:

```bash
pytest tests/test_capture_extractor.py tests/test_memory_quality_semantic.py tests/test_phase3_review_closure.py -q
ruff check src/memory/capture/extractor.py experiments/memory_quality/semantic.py tests/test_capture_extractor.py tests/test_memory_quality_semantic.py
git diff --check
```

Expected: PASS. No production output parsing or classifier behavior changes.

- [ ] **Step 5: Commit**

```bash
git add src/memory/capture/extractor.py experiments/memory_quality/semantic.py tests/test_capture_extractor.py tests/test_memory_quality_semantic.py
git commit -m "fix(quality): share semantic rules across extraction transports"
```

---

### Task 1: Add a semantic-only measured runner

**Files:**
- Modify: `experiments/memory_quality/contracts.py`
- Create: `experiments/memory_quality/semantic_repair.py`
- Modify: `experiments/memory_quality/runner.py`
- Create: `tests/test_memory_quality_semantic_repair.py`

**Interfaces:**
- Produces: `SemanticRepairManifest` with `run_id`, pinned versions/digests, `observation_count=144`, `cleanup_complete` and `mutating_requests`
- Produces: `run_semantic_repair(env: Mapping[str, str]) -> dict[str, object]`
- Adds CLI: `python -m experiments.memory_quality.runner semantic-repair-run`
- Reuses: `preflight`, `run_semantic_case`, `build_blind_packet`, `_blind_semantic_artifacts`, `scan_canaries`, `DisposableHindsight`

- [ ] **Step 1: Write refusal and exact-matrix tests**

Tests must prove:

```python
def test_semantic_repair_refuses_without_explicit_authority(monkeypatch):
    monkeypatch.delenv("HINDSIGHT_BAKEOFF_CONFIRM", raising=False)
    with pytest.raises(BakeoffRefused):
        run_semantic_repair({})


def test_semantic_repair_matrix_is_exact():
    keys = semantic_repair_matrix(load_semantic_cases(CORPUS))
    assert len(keys) == 144
    assert len(set(keys)) == 144
    assert {variant for _, variant, _ in keys} == {
        "ach_semantic", "native_semantic", "hybrid_semantic"
    }
```

Add mutation tests for foreign bank IDs, `executed=false`, missing repetitions, duplicate keys and canary-bearing serialized artifacts.

- [ ] **Step 2: Run tests and verify RED**

```bash
pytest tests/test_memory_quality_semantic_repair.py -q
```

Expected: FAIL because the module and CLI command do not exist.

- [ ] **Step 3: Implement the minimal semantic-only lifecycle**

`semantic_repair_matrix` returns deterministic `(case_id, variant, repetition)` tuples. `run_semantic_repair` must:

1. run existing preflight and pin the new run ID;
2. execute only `run_semantic_case` for the 144 tuples;
3. blind only after all outputs exist;
4. atomically write `manifest.json`, `observations.jsonl` and `blind-packet.json`;
5. scan the run directory for every corpus/host canary;
6. clean every registered bank in `finally` and record cleanup failures outside the blind packet.

Reject a run directory that already contains a measured manifest. Never reuse the Phase 5.5 run ID.

- [ ] **Step 4: Verify lifecycle and CLI**

```bash
pytest tests/test_memory_quality_semantic_repair.py tests/test_memory_quality_runner.py -q
ruff check experiments/memory_quality/contracts.py experiments/memory_quality/semantic_repair.py experiments/memory_quality/runner.py tests/test_memory_quality_semantic_repair.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add experiments/memory_quality/contracts.py experiments/memory_quality/semantic_repair.py experiments/memory_quality/runner.py tests/test_memory_quality_semantic_repair.py
git commit -m "feat(quality): add fenced semantic repair run"
```

---

### Task 2: Score a new consensus without weakening V3

**Files:**
- Modify: `experiments/memory_quality/scoring.py`
- Modify: `experiments/memory_quality/runner.py`
- Modify: `tests/test_memory_quality_scoring.py`
- Modify: `tests/test_memory_quality_semantic_repair.py`

**Interfaces:**
- Produces: `score_semantic_repair(...) -> ScorecardV3`
- Adds CLI: `semantic-repair-score`
- Writes once: `scorecard.semantic-repair.json`, `decisions.semantic-repair.json`, `report.semantic-repair.md`
- Requires: `HINDSIGHT_ADJUDICATION_CONSENSUS_SHA256`, external HMAC key and private sealed mapping

- [ ] **Step 1: Write failing scoring-boundary tests**

Cover these exact properties:

- consensus SHA mismatch refuses scoring;
- packet/adjudication coverage is exactly 144/144;
- `scope=ignore` units are excluded from missing-unit atoms;
- critical rejected-current output remains a hard gate;
- `NO_SHARED_DOCUMENT` and `WRONG_SCOPE` affect only `scope_router`;
- different missing units in the same case do not cancel;
- a second write refuses to overwrite the first score.

- [ ] **Step 2: Run tests and verify RED**

```bash
pytest tests/test_memory_quality_scoring.py tests/test_memory_quality_semantic_repair.py -q
```

- [ ] **Step 3: Implement semantic-only scoring**

Load only the new run's 144 observations, consensus, blind packet and sealed mapping. Call `score_run_v3` with:

```python
expected_units = {
    case.id: tuple(unit.unit_id for unit in case.expected_units)
    for case in semantic_cases
}
critical_units = {
    case.id: tuple(unit.unit_id for unit in case.expected_units if unit.critical)
    for case in semantic_cases
}
ignored_units = {
    case.id: tuple(unit.unit_id for unit in case.expected_units if unit.scope == "ignore")
    for case in semantic_cases
}
critical_rejection_cases = tuple(
    case.id
    for case in semantic_cases
    if any(unit.critical and not unit.current for unit in case.expected_units)
)
```

Emit decisions only for `semantic_extractor` and `scope_router`; copy no ruling from the earlier run. Scan the three outputs for canaries and forbidden identity fields before returning success.

- [ ] **Step 4: Run scoring and mutation suites**

```bash
pytest tests/test_memory_quality_scoring.py tests/test_memory_quality_semantic_repair.py -q
ruff check experiments/memory_quality/scoring.py experiments/memory_quality/runner.py tests/test_memory_quality_scoring.py tests/test_memory_quality_semantic_repair.py
git diff --check
```

- [ ] **Step 5: Commit**

```bash
git add experiments/memory_quality/scoring.py experiments/memory_quality/runner.py tests/test_memory_quality_scoring.py tests/test_memory_quality_semantic_repair.py
git commit -m "feat(quality): score repaired semantic baseline"
```

---

### Task 3: Execute, adjudicate and close the architecture ruling

**Files:**
- Modify: `docs/results/2026-09-02-memory-quality-phase-5-5.md`
- Modify: `docs/plans/2026-08-29-memory-quality-program.md`
- Create: `.artifacts/memory-quality/$HINDSIGHT_BAKEOFF_RUN_ID/` (ignored, mode `0600` files)

**Interfaces:**
- Consumes: new 144-item blind export and five independent adjudications using the existing closed rubric
- Produces: one consensus, one unblind/score execution and final two-component rulings

- [ ] **Step 1: Run preflight and the semantic-only matrix**

Use a new UUID. External custody exports `HINDSIGHT_BAKEOFF_HMAC_KEY` without writing it into the repository or command log:

```bash
export HINDSIGHT_BAKEOFF_RUN_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
HINDSIGHT_BAKEOFF_CONFIRM=disposable-banks-only \
HINDSIGHT_BAKEOFF_URL=http://127.0.0.1:8888 \
python -m experiments.memory_quality.runner semantic-repair-run
```

Verify 144 unique executed observations, no canaries and zero registered banks remaining after cleanup.

- [ ] **Step 2: Export and obtain five blind adjudications**

Use the same variant-neutral rubric and output contract as Phase 5.5. Each judge receives only the export, rubric and example contract. Consensus must cover all 144 tuples exactly; no judge may access mapping, source, git history or another adjudication.

- [ ] **Step 3: Score exactly once**

The custodian exports the existing run ID plus `HINDSIGHT_BAKEOFF_HMAC_KEY`, `HINDSIGHT_BAKEOFF_MAPPING_PATH` and the independently calculated `HINDSIGHT_ADJUDICATION_CONSENSUS_SHA256`. Then run:

```bash
python -m experiments.memory_quality.runner semantic-repair-score
```

- [ ] **Step 4: Apply the closed decision rule**

- If repaired ACH has no hard-gate failure, erase the provisional production-defect finding and compare native/hybrid by exact repeatable atoms.
- If repaired ACH still fails a named hard gate, create a separate production-fix plan containing only those confirmed cases; do not add broad heuristics here.
- Native remains ineligible as a full router while any `NO_SHARED_DOCUMENT` gate fails.
- Hybrid may replace/simplify a component only if every applicable hard gate passes and it has no additional repeatable atom versus repaired ACH.

- [ ] **Step 5: Update results and HLD, then verify**

Record run ID, consensus hash, artifact hashes, cleanup proof, exact misses and accepted rulings. Run:

```bash
pytest tests/test_memory_quality*.py -q
ruff check experiments/memory_quality src/memory/capture tests/test_memory_quality_semantic_repair.py
git diff --check
```

- [ ] **Step 6: Commit documentation**

```bash
git add docs/results/2026-09-02-memory-quality-phase-5-5.md docs/plans/2026-08-29-memory-quality-program.md
git commit -m "docs(quality): close repaired semantic bake-off"
```
