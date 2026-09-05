# ach-memory v0.4.0 typed retain and lifecycle — compatibility gate

Status: **LIVE HINDSIGHT 0.9.2 GATE PASSED**

The gate was executed on 2026-09-05 against the real loopback Hindsight 0.9.2 deployment with
explicit disposable-bank confirmation. It proves the v0.4.0 typed-retain boundary and governed
mental-model lifecycle are compatible with that deployment. It does not activate v0.4.0, mutate a
production bank or replace the separate release/activation gates in the SPEC.

## Contract corrections found by the live run

The first measured run exposed four real compatibility gaps, all fixed before the passing run:

- A named retain strategy is not globally available merely because ACH names it. Bootstrap and
  typed retain now idempotently install and verify the frozen `ach-exact-v1` definition in each
  governed bank, while preserving unrelated strategies. A failed verification blocks retain.
- Hindsight 0.9.2 creates a mental model as `mental_model_id` plus `operation_id`, not `id`. ACH now
  consumes both and withholds generated content until that exact operation succeeds.
- Hindsight returns reflection text in `text`, not `answer`.
- Hindsight has no `mode: manual` trigger. ACH represents manual refresh as `{}`, omits the trigger
  on upstream creation and normalizes Hindsight's inactive defaults during crash recovery.

The read-only preflight was also corrected. Hindsight 0.9.2 dry-run extraction does not expose
chunk/entity structure and its per-call chunks override is not equivalent to a named-strategy
retain on this deployment. Preflight therefore verifies the exact version and resolved strategy;
the guarded synchronous disposable retain supplies the behavioral proof.

## Live gate

```bash
MEMORY_HINDSIGHT_URL=http://127.0.0.1:8888 \
HINDSIGHT_V040_CONFIRM=disposable-banks-only \
uv run pytest tests/test_v040_hindsight_live.py \
  tests/test_v040_mental_models_live.py -q -rs --durations=10
```

Result: **6 passed in 57.72 seconds**.

| Check | Status | Evidence |
|---|---|---|
| Read-only preflight | PASS | Hindsight 0.9.2; named strategy present and exact; zero mutations |
| Exact retain | PASS | One 4,096-byte claim became one identical `world` fact and one document unit; no entities; every extraction-token counter was zero |
| Scale recall | PASS | 500 siblings; all ten frozen v2 lexical/paraphrased probes returned the unique intended fact in the first ten; 46.94 s |
| Consolidation and curation | PASS | Explicit consolidation produced observations with `source_memory_ids`; invalidating a source removed its dependent active observation; 7.58 s |
| Exact retry | PASS | Replaying the accepted `operation_id` kept the document total at three and did not restore the invalidated fact |
| Fault boundary | PASS | An unavailable backend surfaced as typed, non-leaking `HindsightError` |
| Mental-model governance | PASS | Idempotent built-in bootstrap, five-custom quota, budget refusal, update, refresh withholding and deletion; 1.42 s |
| Unknown upstream model | PASS | Reported in inventory but never adopted into ACH governance |
| Cleanup | PASS | No bank with any v0.4.0 live-test or diagnostic prefix remained after the run |

The original scale fixture produced 6/10 before acceptance. Two failed probes did not identify any
one claim among 500 equivalent siblings, so the oracle had no uniquely correct answer; the other
two reduced the only identifier to an artificial number-format conversion. The fixture was
replaced with one uniquely identifiable semantic fact and ten queries that all designate it, then
frozen as v2. The required threshold remained 10/10 and the complete gate was rerun from clean
disposable banks.

## Non-live verification

`uv run pytest -q -m "not integration"`: **2,239 passed, 5 skipped, 6 deselected** in 126.20
seconds. `uv run ruff check src tests scripts/v040-hindsight-preflight.py` and
`git diff --check` both passed. The separately deployed ACH API append integration remains outside
this gate because no plaintext `MEMORY_MASTER_KEY` was present; it is not counted as passing or as
a failure of this branch.

## Activation boundary

Typed retain/lifecycle is now compatible with the measured Hindsight 0.9.2 deployment. Production
flags remain unchanged and no production bank was read or written. Whole-release production
eligibility still requires the remaining v0.4.0 gates and an explicit operator activation.
