# Memory Quality Phase 5.7 Measured Splitter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Replace Phase 5.6's fabricated hybrid gates with a real five-destination pre-retain splitter and produce valid evidence for keeping or removing ach-memory semantic machinery.

**Architecture:** First remove the proven provenance blast radius and version the invalid S09 corpus stimulus. Then execute a strict minimal splitter, persist only its User and Project projections into separate disposable Hindsight banks, inspect `DocumentResponse.original_text`, and score semantic coverage together with actual routing/isolation. Native Hindsight remains an extraction reference, never an eligible router.

**Tech Stack:** Python 3.12, Pydantic 2, pytest, httpx/respx, Hindsight 0.9.2, JSONL.

**Spec:** `docs/superpowers/specs/2026-09-02-memory-quality-phase-5-7-design.md`; governing product contract: `docs/specs/2026-08-29-memory-quality-v1.4.md` §5–§10.

## Global Constraints

- Keep local sanitization, authorization, physical User/Project isolation, durable post-ACK capture and exact external Working State.
- Do not add case-specific or free-text routing heuristics to `memory.capture.classifier`.
- Preserve Phase 5.5 run `36b90ba4-9644-4bfa-a956-f4a350d0d86c` and Phase 5.6 run `dd364495-b7fa-4908-ab43-102c35e5cd27` byte-for-byte.
- Keep `MEMORY_CAPTURE_ENABLED`, `MEMORY_CAPTURE_WORKER_ENABLED`, `MEMORY_CAPTURE_CORRECTION_REFRESH_ENABLED` false and `MEMORY_PROFILE_DELIVERY_MODE=legacy`.
- Use only fresh `mq55-` disposable banks and delete every registered bank.
- Never retain the unsplit transcript in a hybrid User or Project bank.
- Unknown tokens or timing are `null`; never encode unknown measurement as zero.
- Phase 5.7 does not change profile compiler, INDEX/FULL, reliability or Working State storage rulings.

---

### Task 0: Version the semantic corpus and replace invalid S09

**Files:**
- Create: `experiments/memory_quality/corpus/semantic-v2.jsonl`
- Modify: `experiments/memory_quality/contracts.py`
- Modify: `tests/test_memory_quality_corpus.py`

**Interfaces:**
- Produces: `SEMANTIC_V2_CORPUS_VERSION = "semantic-v2"`
- Preserves: `semantic.jsonl` unchanged for the frozen Phase 5.6 run

- [x] **Step 1: Write the failing corpus test**

Add a test that loads `semantic-v2.jsonl`, requires exactly S01–S16, and asserts S09 contains a concrete objective or next step rather than the literal `Transient progress is not durable memory`. Assert S09-U1 remains critical, current and `scope="working_state"`.

- [x] **Step 2: Run the test and verify RED**

Run:

```bash
pytest tests/test_memory_quality_corpus.py -q
```

Expected: FAIL because `semantic-v2.jsonl` does not exist.

- [x] **Step 3: Create the versioned corpus**

Copy all 16 closed cases. Replace only S09 with a concrete statement such as `Current objective: finish transcript replay. Next step: run the delivery gate.` and require `finish transcript replay` plus `run the delivery gate` in S09-U1. Do not change the original corpus.

- [x] **Step 4: Verify and commit**

```bash
pytest tests/test_memory_quality_corpus.py -q
ruff check experiments/memory_quality/contracts.py tests/test_memory_quality_corpus.py
git diff --check
git add experiments/memory_quality/corpus/semantic-v2.jsonl experiments/memory_quality/contracts.py tests/test_memory_quality_corpus.py
git commit -m "test(quality): version the semantic splitter corpus"
```

---

### Task 1: Make invalid provenance candidate-local

**Files:**
- Modify: `src/memory/capture/extractor.py`
- Modify: `tests/test_capture_extractor.py`
- Modify: `tests/test_memory_quality_semantic.py`

**Interfaces:**
- Produces: `_without_invalid_provenance(envelope: dict, candidate: NormalizedCandidate, slice_length: int) -> NormalizedCandidate`
- Preserves: strict malformed-envelope and multiple-Working-State failures

- [x] **Step 1: Write failing behavior tests**

Add one extractor test whose provider returns a `stated` candidate with an out-of-range span followed by a valid sibling. Require both candidates to survive and the first candidate's provenance to become `None`.

Add one test whose provider returns an `observed` convention with an out-of-range span. Require it to survive as `origin="inferred"`, `eligible="evidence_only"` and `provenance=None`.

Extend prompt parity coverage to require both envelopes to say that transcript-span provenance is required for `observed` and omitted for all other origins.

- [x] **Step 2: Run and verify RED**

```bash
pytest tests/test_capture_extractor.py tests/test_memory_quality_semantic.py -q
```

Expected: the current whole-slice `ExtractionFailed` behavior fails both new extractor tests.

- [x] **Step 3: Implement the candidate-local repair**

After structural classification, preserve an in-range span. For an out-of-range span, shallow-copy the already validated envelope, remove `provenance`, and call `classify` again. The existing classifier then degrades `observed` to `inferred` and recalculates eligibility. Do not inspect claim text and do not modify `classifier.py`.

- [x] **Step 4: Verify and commit**

```bash
pytest tests/test_capture_extractor.py tests/test_memory_quality_semantic.py tests/test_phase3_review_closure.py -q
ruff check src/memory/capture/extractor.py tests/test_capture_extractor.py tests/test_memory_quality_semantic.py
git diff --check
git add src/memory/capture/extractor.py tests/test_capture_extractor.py tests/test_memory_quality_semantic.py
git commit -m "fix(capture): isolate invalid provenance to its candidate"
```

---

### Task 2: Read real disposable-bank documents and measure elapsed time

**Files:**
- Modify: `experiments/memory_quality/contracts.py`
- Modify: `experiments/memory_quality/hindsight.py`
- Modify: `experiments/memory_quality/semantic.py`
- Modify: `tests/test_memory_quality_upstream.py`
- Modify: `tests/test_memory_quality_semantic.py`

**Interfaces:**
- Extends: `DisposableHindsight.retain_and_wait(..., strategy: Literal["conversation", "verbatim"] = "conversation")`
- Extends: `SnapshotObject` with `original_text: str | None`
- Produces: actual monotonic `duration_ms: int`; unknown token counts remain `None`

- [x] **Step 1: Write failing boundary tests**

Use respx to return a document list followed by `GET /documents/{document_id}` with `original_text`. Require `list_bank_objects` to expose that exact value. Add a retain test proving `strategy="verbatim"` is sent per item. Add a semantic test that patches `time.monotonic` to two non-zero values and asserts the observation records their delta.

- [x] **Step 2: Verify RED**

```bash
pytest tests/test_memory_quality_upstream.py tests/test_memory_quality_semantic.py -q
```

- [x] **Step 3: Implement the measured boundary**

Fetch each listed document through the OpenAPI-confirmed `GET /v1/default/banks/{bank_id}/documents/{document_id}` route and copy `original_text` into the snapshot. Accept only the two closed retain strategies. Wrap each semantic variant in `started = time.monotonic()` / `elapsed` and never substitute zero for missing provider token data.

- [x] **Step 4: Verify and commit**

```bash
pytest tests/test_memory_quality_upstream.py tests/test_memory_quality_semantic.py -q
ruff check experiments/memory_quality/contracts.py experiments/memory_quality/hindsight.py experiments/memory_quality/semantic.py tests/test_memory_quality_upstream.py tests/test_memory_quality_semantic.py
git diff --check
git add experiments/memory_quality/contracts.py experiments/memory_quality/hindsight.py experiments/memory_quality/semantic.py tests/test_memory_quality_upstream.py tests/test_memory_quality_semantic.py
git commit -m "fix(quality): measure semantic bank effects"
```

---

### Task 3: Implement the strict five-destination splitter

**Files:**
- Create: `experiments/memory_quality/splitter.py`
- Create: `tests/test_memory_quality_splitter.py`
- Modify: `experiments/memory_quality/semantic.py`

**Interfaces:**
- Produces: `SplitDestination = Literal["durable_user", "durable_project", "evidence", "working_state", "discard"]`
- Produces: `SplitEnvelope` and `SplitResult`
- Produces: `split_and_persist(case, repetition, banks) -> SplitResult`, carrying the normalized `SemanticOutput` plus measured isolation gates

- [x] **Step 1: Write strict-schema and isolation tests**

Cover extra fields, an evidence item without `subject`, a durable item with the wrong subject, more than one Working State item and malformed outer facts. Use a fake disposable boundary to prove the raw transcript is never passed to `retain_and_wait`, user/project projection texts go only to their respective bank, evidence follows its explicit subject, and discard/Working State are never retained.

- [x] **Step 2: Verify RED**

```bash
pytest tests/test_memory_quality_splitter.py -q
```

- [x] **Step 3: Implement the minimum route contract**

Ask Hindsight dry-run extraction for one outer `facts` object whose fact text is an escaped `SplitEnvelope`. Include only the SPEC rules needed to choose the five destinations. Do not request or emit kind, origin, eligibility, ranking, displacement, tags or proof count.

Create separate disposable User and Project banks. Serialize one semantic claim per projection document and retain it with `strategy="verbatim"`. Build `SemanticOutput.claims` from parsed envelopes and Working State from the single Working State envelope.

- [x] **Step 4: Derive isolation gates from bank snapshots**

After retain completion, load both banks. Fail `no_shared_document` unless all persisted `original_text` values are exact members of that bank's projection and none equals or contains the canonical raw transcript. Emit separate `user_bank_scope_clean` and `project_bank_scope_clean` gates.

- [x] **Step 5: Verify and commit**

```bash
pytest tests/test_memory_quality_splitter.py tests/test_memory_quality_semantic.py -q
ruff check experiments/memory_quality/splitter.py experiments/memory_quality/semantic.py tests/test_memory_quality_splitter.py
git diff --check
git add experiments/memory_quality/splitter.py experiments/memory_quality/semantic.py tests/test_memory_quality_splitter.py tests/test_memory_quality_semantic.py
git commit -m "feat(quality): measure a five-destination splitter"
```

---

### Task 4: Make adoption scoring reject silent routers

**Files:**
- Modify: `experiments/memory_quality/scoring.py`
- Modify: `tests/test_memory_quality_scoring.py`

**Interfaces:**
- Extends: `score_run_v3(...)` router hard gates with missing routeable expected units
- Preserves: extraction and routing atoms as separate report fields

- [x] **Step 1: Write the failing silent-router mutation test**

Construct a challenger with no wrong-scope claims but no credited required unit. Assert its scope-router challenger is not eligible and contains `MISSING_ROUTEABLE_UNIT`. Keep a complete correctly scoped challenger eligible.

- [x] **Step 2: Verify RED**

```bash
pytest tests/test_memory_quality_scoring.py -q
```

- [x] **Step 3: Implement the composed eligibility gate**

For `scope_router`, retain the existing wrong-scope atom. Additionally add a hard `variant:case:MISSING_ROUTEABLE_<unit>` failure for every non-ignored expected unit absent from the adjudication. This is an adoption gate; do not remove the extractor's independent missing-unit atom.

- [x] **Step 4: Verify and commit**

```bash
pytest tests/test_memory_quality_scoring.py tests/test_memory_quality_semantic_repair.py -q
ruff check experiments/memory_quality/scoring.py tests/test_memory_quality_scoring.py
git diff --check
git add experiments/memory_quality/scoring.py tests/test_memory_quality_scoring.py
git commit -m "fix(quality): reject silent semantic routers"
```

---

### Task 5: Run the measured Phase 5.7 comparison and close the ruling

**Files:**
- Modify: `experiments/memory_quality/semantic_repair.py`
- Modify: `experiments/memory_quality/runner.py`
- Modify: `tests/test_memory_quality_semantic_repair.py`
- Modify: `docs/results/2026-09-02-memory-quality-phase-5-5.md`
- Modify: `docs/plans/2026-08-29-memory-quality-program.md`

**Interfaces:**
- Adds CLI: `semantic-splitter-run`
- Consumes: `semantic-v2.jsonl`
- Produces: fresh exact semantic matrix and final component ruling

- [x] **Step 1: Write the failing runner test**

Require a new run ID, exact 144 observations over repaired ACH, native reference and measured splitter, corpus version/digest in the manifest, actual cleanup counts, no `executed=false`, and no hard gate derived solely from a variant name.

- [x] **Step 2: Implement and verify the lifecycle**

Reuse the existing authority, loopback allow-list, atomic artifact, canary scan, external mapping and cleanup boundaries. Do not rerun Phase 5.5/5.6 artifacts.

```bash
pytest tests/test_memory_quality_semantic_repair.py tests/test_memory_quality_runner.py -q
ruff check experiments/memory_quality/semantic_repair.py experiments/memory_quality/runner.py tests/test_memory_quality_semantic_repair.py
git diff --check
```

- [x] **Step 3: Execute preflight and the live matrix**

If `127.0.0.1:8888` is absent, run `kubectl port-forward -n hindsight service/hindsight-api 8888:8888`. Require Hindsight 0.9.2, fresh disposable prefix and all production flags off. Execute once, verify exact observation cardinality, bank cleanup and canary absence.

- [x] **Step 4: Obtain independent semantic judgments and score**

Judges must not see implementation identity or another judgment. Cryptographic ceremony is not a product gate; exact tuple coverage and independent access are. Score once after complete judgments.

- [x] **Step 5: Apply the decision rule**

The splitter may replace custom machinery only if every hard gate passes and it loses fewer than two distinct valid cases across all repetitions. Native remains ineligible as a router if bank inspection finds the raw/shared source. Otherwise retain the failed component and record exact atoms without adding heuristics.

- [x] **Step 6: Verify, document and commit**

```bash
pytest tests/test_memory_quality*.py tests/test_capture_extractor.py tests/test_phase3_review_closure.py -q
ruff check experiments/memory_quality src/memory/capture tests/test_memory_quality*.py tests/test_capture_extractor.py
git diff --check
git add experiments/memory_quality/semantic_repair.py experiments/memory_quality/runner.py tests/test_memory_quality_semantic_repair.py docs/results/2026-09-02-memory-quality-phase-5-5.md docs/plans/2026-08-29-memory-quality-program.md
git commit -m "docs(quality): close measured splitter decision"
```
