# Memory Quality Phase 5.7 — Measured Splitter Design

## Purpose

Produce the missing evidence from Phase 5.6: whether a minimal pre-retain splitter can replace part of ach-memory's semantic taxonomy while preserving the hard guarantees Hindsight does not provide.

Phase 5.7 does not activate production. It repairs one proven parser failure, corrects one invalid corpus case and measures a real hybrid. It does not revisit profiles, INDEX/FULL delivery, reliability, authorization, sanitization or Working State storage.

## Accepted boundaries

The following remain owned by ach-memory:

- local corporate sanitization before any live request;
- physical User/Project separation before retain;
- authorization, ownership and opaque bank resolution;
- durable post-ACK capture processing;
- exact external Working State with fencing.

No free-text heuristic is added to `classifier.py`. A production semantic component survives only if removing it breaks a hard gate or loses at least two distinct valid cases in every repetition.

## Rejected shortcuts

Direct native retain is not a candidate router because it stores one source document in one bank. The Phase 5.6 toy hybrid is not repaired in place: it fabricated scope/isolation gates and never persisted projections. Prompt-tuning only the five known scope misses is rejected as corpus overfitting.

## Provenance repair

Hindsight may frame submitted content with an internal role prefix. In Phase 5.6 it returned out-of-range spans for otherwise valid `stated` claims, causing the entire slice and sibling candidates to be lost.

The parser will treat an invalid span as invalid provenance, not as proof that every candidate is invalid:

1. classify the envelope normally;
2. if its span is within the submitted slice, preserve it;
3. if it is out of range, remove the span and reclassify the same structural envelope;
4. `stated` and `confirmed` retain their origin without a span;
5. `observed` without a valid artifact span degrades to `inferred` through the existing classifier rule and therefore cannot gain profile eligibility from false provenance;
6. sibling candidates survive.

The extraction prompt will also say that transcript-span provenance is required for `observed` and should be omitted for other origins. The parser remains the safety boundary.

## Versioned corpus

The Phase 5.6 corpus remains unchanged for reproducibility. Phase 5.7 uses `semantic-v2.jsonl`, copied from it except S09. S09 becomes a concrete current objective/next-step statement; meta-commentary about transient progress is not Working State.

## Minimal splitter

The challenger emits one strict route envelope per semantic claim with five destinations:

```json
{
  "destination": "durable_user|durable_project|evidence|working_state|discard",
  "subject": "user|project|null",
  "text": "one semantic claim",
  "current": true
}
```

`evidence` requires `subject=user|project`. `working_state` uses the existing Working State envelope rather than becoming a Hindsight fact. `discard` is counted but never persisted. Durable and evidence claims are projected separately and retained only in their selected disposable User or Project bank. The original sanitized transcript is never retained in either bank.

This splitter deliberately omits `kind`, `origin`, `profile_eligible`, ranking and displacement. Those fields survive only if their ablation fails the measured gate.

## Measured gates

The harness derives results from artifacts and bank snapshots, never from the variant name:

- every submitted request is canary-free;
- every expected semantic unit is present with its current/rejected meaning;
- each emitted claim has the expected destination;
- the User bank contains only the user/evidence-user projection;
- the Project bank contains only the project/evidence-project projection;
- neither bank contains the original source document;
- an empty or incomplete extractor cannot receive a clean routing result;
- duration is measured with a monotonic clock; unavailable token counts and latency are `null`, never fabricated zeroes;
- all disposable banks are deleted.

The repaired ACH arm must first reach valid output on S01, S12 and S15. Then the full v2 corpus compares repaired ACH with the measured splitter. Native extraction may remain an informational semantic reference but is not eligible as a router.

## Decision

Adopt the minimal splitter only if it passes every isolation, sanitization, current-truth and critical-unit gate and has no repeatable loss in two or more distinct valid cases versus repaired ACH. Otherwise keep the existing pipeline and record the exact failing atoms; do not add broader heuristics.
