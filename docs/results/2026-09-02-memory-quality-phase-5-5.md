# Memory Quality Phase 5.5 — measured result

Status: **COMPLETE WITH A SEMANTIC BASELINE LIMITATION**

Frozen run: `36b90ba4-9644-4bfa-a956-f4a350d0d86c`  
Consensus SHA-256: `04717dfe9b08ae165c3aafb52e6ef2e1d72d22dd09a0be44f1cf979b61a13633`  
Matrix: 319 observations — 3 preprocessing, 144 semantic, 150 delivery and 22 reliability.

The run used Hindsight 0.9.2 and `hindsight-coding-agents` 0.5.1, five independent blind judges and run-scoped disposable banks. Cleanup removed all 234 registered banks. No canary, mapping, HMAC key, bank ID or raw transcript appears in the scored artifacts. Production flags remain off and Phase 0 was not executed.

## Accepted findings

| Component | Finding | Consequence |
|---|---|---|
| Preprocessing | Official preprocessing failed `NO_CANARY`; ACH and hybrid passed. | Keep local ACH sanitization. Official normalization may be reused only before the ACH redaction/drop policy. |
| Host adapters | One Claude fixture and no adapter challenger. | `insufficient_evidence`; do not remove the current adapters. |
| Native semantic routing | `NO_SHARED_DOCUMENT` failed in all 16 cases. | Native single-bank ingestion cannot replace physical User/Project isolation. |
| Hybrid semantic routing | Wrong-scope remained in S03, S09 and S15. | Hybrid is not currently eligible as the full router replacement. |
| Profile compiler / delivery | `consumer_complete=false`. | Both remain `insufficient_evidence`. |
| Capture reliability / Working State ordering | The 22 observations lack structured terminal outcomes. | Both remain `insufficient_evidence`; the run proves execution, not the promised recovery/order semantics. |

## Semantic baseline limitation

The raw V3 scorer reported `add_follow_up_guard` for the ACH semantic extractor and router. Architecture review does **not** accept that as evidence of a production defect.

`experiments.memory_quality.semantic._AchHindsightAdapter` overwrote the production `EXTRACTION_PROMPT` with `_HINDSIGHT_COMPATIBLE_ACH_MISSION`. The replacement preserved the JSON transport envelope but omitted the production rules for Working State precedence, project-default scope, proposals/authorization, corrections, negative constraints, gotcha evidence and provenance. Frozen ACH outputs show the consequence: project decisions, constraints and technical evidence were repeatedly emitted as user preferences or user technical claims.

Therefore the run compared the official and hybrid variants against an underspecified ACH mission, not against the production semantic contract named by the plan. V3's exact-atom scoring remains valid, but its ACH semantic input is not a valid baseline. No production extractor/classifier change is authorized from those misses.

Phase 5.6 must make the compatibility mission semantically equivalent to the production prompt, prove that equivalence in tests, and repeat only the semantic experiment needed to restore the comparison. Existing preprocessing, delivery and reliability observations remain frozen and are not rerun.

## Scoring history

V1 and V2 remain preserved. Initial V3 artifacts are preserved as `*.v3.superseded.*` because V3 initially treated an `ignore`-scope unit as something that had to be emitted. Commit `11e3856` fixes that bug test-first; corrected V3 no longer counts S16-U1 as missing while still failing any unsupported current claim in that critical rejection case.

Corrected V3 hashes:

- `scorecard.v3.json`: `8c3dcfe291e5648dac252a7edcf2db685d1f1fe38445d04ded673334924845c4`
- `decisions.v3.json`: `051e9f240b503878829bd40cac119f5f78a6daf2da4bad8f40b2d8c54287a516`
- `report.v3.md`: `292748c55f7a18a6549b36ec0a5f4a1d1fe145706b694deb108b0f21580dbcf7`

The accepted architectural ruling in this document supersedes the raw semantic component rulings in `decisions.v3.json` because of the prompt-equivalence defect above.
