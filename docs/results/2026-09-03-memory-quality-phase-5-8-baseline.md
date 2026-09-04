# Memory Quality Phase 5.8 — validated semantic baseline

Status: **MEASURED — NOT PRODUCTION-ELIGIBLE**

Frozen run: `b948617e-88b1-45ea-93e5-698c64aa0727`  
Corpus: `semantic-v3` (`787b35f1e4fec9756ac6ef77dec852c6d8374a5f60f133158eaf0186ec737df1`)  
Decision policy: `semantic-baseline-policy-v1`  
Matrix: 14 accepted cases, three repetitions, 42 observations.

This run measures the current ACH extractor/router after the general repository-default prompt
change. It is the first retained-bank baseline over the audited corpus. It is not a replacement
comparison and is not rerun-selectable.

## Corpus audit

S03 and S14 were quarantined before the run. Their isolated utterances did not contain enough
context to determine a durable destination without turning the fixture wording into product
policy. Fourteen cases were copied byte-for-byte from `semantic-v2` into `semantic-v3`; the audit
records their accepted destination and rationale.

S14 remains especially unsuitable as a semantic-memory fixture. An instruction to perform
something "exactly once" may be a current consumable instruction, Working State, or a system
delivery guarantee. It is not automatically durable memory. S03 has the same missing-context
problem for whether a statement is personal and cross-project or local to the repository.

## Measured result

The production gate failed. Eighteen of 42 observations passed every gate, representing six of
14 cases cleanly repeated three times.

| Case | Repetitions | Physical result | Failing gates |
|---|---:|---|---|
| S01, S02, S09, S11, S12, S13 | 3 each | Expected disposition | none |
| S04 | 3 | Project | eligibility/currentness mismatch |
| S05 | 3 | Project | retained as an active decision instead of non-current project history |
| S06 | 3 | User | wrong physical bank; negative project constraint lost |
| S07 | 3 | Project | eligibility/currentness mismatch |
| S08 | 3 | none | extraction contract rejected; required evidence absent |
| S10 | 3 | none | extraction contract rejected; critical correction absent |
| S15 | 3 | Project | eligibility/currentness mismatch |
| S16 | 3 | User | discard envelope violated; unsupported active candidate retained |

The general prompt rule improved S05's physical destination from User to Project, but did not
preserve it as non-current history. S06 and S16 remain physical User-bank errors. Because the
worker consumes `candidate.bank_kind` directly, those are storage violations if capture is
enabled, not presentation-only scoring differences.

S04, S07 and S15 expose a separate contract problem: the corpus's binary `current` field is being
used as an exact proxy for the classifier's `profile_eligible`/`evidence_only` matrix. Their text
was physically routed to Project, but the expected eligibility disagreed. This is quality debt,
not evidence for relaxing the physical isolation gate. S15 is also meta-level and should be
re-audited before another semantic run.

S08 and S10 were recorded as closed `EXTRACTION_FAILED` outcomes in all repetitions. A later,
separate disposable diagnostic returned schema-conforming outputs for both cases, so the failure
is not deterministically reproducible from the fixture alone. The frozen artifacts intentionally
do not contain provider text or exception details; consequently this run proves an observed
contract-reliability failure but cannot assign its exact subtype after the fact. A future harness
version should persist a content-free closed error code before any rerun.

## Safety and cleanup

- Hindsight 0.9.2 and `hindsight-coding-agents` 0.5.1 were used.
- Exactly 126 disposable banks were created and deleted; no run-prefixed bank remains.
- Thirty-three projection retains completed; 285 mutating requests were accounted for.
- The artifact registry was removed after verified cleanup.
- No declared canary or run bank identifier appears in the artifacts.
- Production flags remained off and Phase 0 was not executed.

## Ruling

```text
semantic_boundary_required = proven
semantic_extractor = keep_as_baseline
scope_router = keep_as_baseline
replacement_approved = false
production_eligible = false
internal_complexity_justified = insufficient_evidence
semantic_baseline_gate = failed
```

This run does not authorize prompt tuning followed by repeated selection. The accepted baseline
has not met its production gate, so semantic ablation remains locked. Consumer delivery and
reliability are independent experiments and may proceed under separately frozen policies.

