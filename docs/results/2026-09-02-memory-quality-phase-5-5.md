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

## Phase 5.6 repaired semantic run

Status: **COMPLETE — INSUFFICIENT EVIDENCE FOR REPLACEMENT**

Frozen run: `dd364495-b7fa-4908-ab43-102c35e5cd27`.

Consensus SHA-256: `8e859b2228113979f63980c35eedc3953f0e84f44bc9e5b337096c3ddcbab844`.

Scoring implementation: `e78220b`.

Matrix: 144 semantic observations — 16 cases, 3 variants and 3 repetitions.
Consensus: 96 required-unit credits, zero unsupported-current claims and 15 wrong-scope claims. Of the closed unit votes, 93 were unanimous and 3 were 4-of-5.

The run used Hindsight 0.9.2 and `hindsight-coding-agents` 0.5.1. Five independent judges received only the opaque export, rubric and output contract. Cleanup removed all 144 disposable banks; no run bank remains. Artifact scans found no canary, raw transcript, bank ID, mapping or HMAC material. Production flags remained off.

Scored artifact hashes:

- `scorecard.semantic-repair.json`: `06a44a5893f0f6d877410b4bb914875ac64d09cc6c7ef0c9935ed6723b59edc0`
- `decisions.semantic-repair.json`: `cba0422bd8dcd746d0527d9d2b86b9ce6d208ad68f6aa5a3ee8f18566f914ed4`
- `report.semantic-repair.md`: `1a81ca7f08643c8e4ac8abee03baefcb61b10030c555696f50ad41bcfa8e4958`

### What the run establishes

- Sharing the production semantic rules fixed the original Phase 5.5 prompt-drift defect.
- ACH still failed `VALID_OUTPUT` in every repetition of S01, S12 and S15. All three failures have one mechanical cause: Hindsight returned `stated` claims with provenance offsets relative to an extra provider role prefix, and the strict parser rejected the whole slice as out of range.
- ACH assigned the wrong destination in every repetition of S03, S05, S06, S09 and S14.
- Native Hindsight preserved the measured semantic units without an additional scored regression, but this does not establish replacement: its comparison baseline was mechanically zeroed for three cases, and the experiment did not pre-route source documents.
- The experimental hybrid was not a credible implementation of the intended hybrid architecture. It used only `Return only {text, bank, provenance} claims.`, did not run the production classifier or persist per-bank projections, fabricated its reported scope set whenever any fact existed, and hardcoded `NO_SHARED_DOCUMENT=true`. It lost critical units in S01, S04, S06, S07, S11, S12 and S14 and had repeatable additional misses in S04, S06, S07, S09, S11 and S14.
- `duration_ms=0` and empty token arrays are placeholders, not cost or latency evidence.
- S09's sentence is meta-level commentary about transient progress, not an actual objective, direction, question or next step. Its Working State expectation is invalid and contributed one apparent miss to ACH and hybrid. The frozen run is not rewritten; the next corpus must replace or quarantine this case before execution.

### Accepted architecture ruling

The generated `add_follow_up_guard` rulings are not accepted as authorization to add case-specific heuristics. Architecture review sets both `semantic_extractor` and `scope_router` to `insufficient_evidence`:

1. keep local sanitization, authorization, physical User/Project isolation, the durable post-ACK queue and exact external Working State;
2. do not replace or expand the production semantic pipeline from this run;
3. repair invalid-provenance blast radius separately and prove all sibling candidates survive;
4. replace S09 with a valid Working State case before the next measurement;
5. make the next hybrid actually route and persist five destinations: durable user, durable project, banked evidence, Working State and discard;
6. measure isolation from bank contents, not a variant-name constant, and emit unknown cost as `null`, never `0 ms`;
7. keep a custom semantic component only if its ablation breaks a hard gate or loses at least two distinct valid cases in all repetitions.

Preprocessing, host adapters, profile compiler, delivery protocol, capture reliability and Working State ordering were not rerun. Their Phase 5.5 rulings remain unchanged.

## Phase 5.7 measured splitter run

Status: **COMPLETE — KEEP ACH ONLY AS THE CURRENT SEMANTIC BASELINE**

Frozen run: `fa142ba9-d706-4df8-b8ac-83764b8d480e`.

Consensus SHA-256: `29a616429b1461eac3bc7111de40bc069a017afe427b7592cb762fd6d83c8fa0`.

Phase 5.7 used the versioned `semantic-v2` corpus and completed 144 observations: 16 cases, repaired ACH, native Hindsight as an informational reference, a real five-destination splitter, three repetitions each. It created exactly 192 disposable banks, issued 48 native retains and 42 hybrid projection retains, read the stored document bodies back through Hindsight, and removed all 192 banks. No duration is a fabricated zero. No run bank or declared canary remained after cleanup.

Three independent adjudications were produced by Gemini 3.7 Flash, GPT-5.6 Terra and Claude Opus. None read another adjudication or the blind mapping. Required-unit consensus used a 2-of-3 majority, numeric claim counts used the median and tied note codes used the conservative order `ADJUDICATION_BLOCKED`, `AMBIGUOUS_OUTPUT`, `PARAPHRASE_ACCEPTED`, `NONE`. All three covered the exact same 144 tuples. Consensus contains 51 required-unit credits, 11 unsupported-current claims and 54 wrong-scope claims.

Those aggregate counts combine three arms and are not a quality result by themselves. There were
45 routeable expected-unit opportunities per arm across the three repetitions (S13 has no expected
unit and S16's expected disposition is `ignore`):

| Arm | Credited units | Recall | Unsupported current claims | Wrong-scope claims | Cases with every measured gate clean |
|---|---:|---:|---:|---:|---:|
| Repaired ACH | 33/45 | 73.3% | 3 | 15 | 11/16 |
| Measured splitter | 18/45 | 40.0% | 6 | 27 | 5/16 |
| Native Hindsight | not applicable | not applicable | 2 | 12 | 0/16 |

ACH and the splitter produced identical credited-unit, unsupported-current and wrong-scope counts
in each repetition. Native produced the same route result and wrong-scope count in all repetitions;
its unsupported-current count was one, one and zero.

Native's zero route credits are not a claim that its extractor has zero semantic quality. Native
is `structurally_ineligible_as_router`: it retained the complete sanitized document in one shared
bank and did not produce the physically separated User/Project projections the routing rubric
requires. This experiment did not separately score the semantic understanding inside that
unscoped document.

Scored artifact hashes:

- `scorecard.semantic-splitter.v2.json`: `4faa0841ac9c252e97d5c19fa158e2ac822bd41677ae1275f792d9e3d7f2d63c`
- `decisions.semantic-splitter.v2.json`: `5836953dd02778fb95cd7e198a9f81848029e4a0fced1ff5a57b260b30df933a`
- `report.semantic-splitter.v2.md`: `5a115af2c6e7b7602eb0b5b81fd6d32628a8bdd0b489f168e0255cd822e8425d`
- `manifest.json`: `9e1cd2bc7ec955ae01cce488c6d99128a86bec4663dac5a7ab432fb782037886`

The first generated splitter score is preserved without overwrite. Its generic decision policy again returned `add_follow_up_guard` when it saw baseline defects. V2 changes only the component decision policy over the same frozen observations and adjudication: baseline quality debt is reported separately from the replacement question.

### Result by arm

| Arm | Measured result | Architecture consequence |
|---|---|---|
| Repaired ACH | Retained the expected meaning and scope in S01, S02, S04, S07–S12 and S15. It still misclassified S03, S05, S06, S14 and S16; the critical baseline guards in the frozen rubric are S03, S06 and S14. | Keep it only as the current baseline. It is not production-eligible, and the result does not justify more taxonomy or free-text heuristics. |
| Measured splitter | Matched ACH on S01, S02, S04, S11 and both S12 units. It lost additional valid cases S07, S08, S09, S10 and S15 in every repetition; it also misrouted S03, S05, S06 and S14, emitted active User truth for S16 and returned an invalid empty discard envelope on all three S13 repetitions. | It fails both the hard-gate and the two-distinct-case adoption rules. Do not replace or simplify the ACH extractor/router with this splitter. |
| Native Hindsight | Retained one complete sanitized source document in a single shared bank in every repetition. Bank inspection derived `NO_SHARED_DOCUMENT=false` and both scope-clean gates false; no judge treated the unscoped objects as proof of correct User/Project routing. | Useful as extraction/consolidation infrastructure, but ineligible as the physical scope router. This run does not claim that its content extraction is intrinsically worse, only that it cannot prove the required route. |

### Accepted architecture ruling

The accepted state is:

```text
semantic_boundary_required = proven
semantic_extractor = keep_as_baseline
scope_router = keep_as_baseline
replacement_approved = false
production_eligible = false
internal_complexity_justified = insufficient_evidence
```

The preserved machine-readable V2 decisions use the older `keep` enum and are not rewritten. In
this report, `keep_as_baseline` is the authoritative interpretation: it rejects the measured
replacements but does not approve the baseline for production or validate all of its internal
machinery. The physical consequence of the baseline misses is direct. ACH emitted S03, S05, S06
and S14 as `profile_eligible` User candidates and S16 as an `evidence_only` User candidate.
Production `worker._retain_stage` groups candidates by `bank_kind` and resolves that value directly
to the User or Project bank before retain. If capture were enabled, these outputs would be stored
in the destination they name; the wrong-scope findings are not merely report-label errors.

This closes only the measured replacement question:

1. Hindsight remains the extraction/consolidation/knowledge engine and its official runtime remains reusable.
2. ach-memory requires a pre-retain semantic boundary because native ingestion cannot provide
   physical routing and the measured splitter did not preserve the semantic gates. The current ACH
   implementation remains only the baseline for improving that boundary.
3. Local sanitization, authorization/ownership, physical User/Project separation, the durable post-ACK queue and exact fenced Working State remain non-negotiable ACH responsibilities.
4. Ranking, displacement, profile compilation and INDEX/FULL delivery were not justified by this semantic experiment. Their Phase 5.5 `insufficient_evidence` rulings remain unchanged until a real consumer evaluation exists.
5. The experiment does not establish that the baseline is sufficiently good for production or
   that its full taxonomy is necessary. S03 and S14 require corpus audit before becoming code
   requirements; S05, S06 and S16 are accepted baseline defects. None permits case-specific string
   rules.

Se rechazan Hindsight nativo y el splitter medido como sustitutos del límite semántico de ACH. El
extractor/router actual se conserva únicamente como baseline y permanece desactivado para
producción hasta superar los hard gates de un corpus validado. El experimento demuestra la
necesidad de una frontera pre-retain, pero no valida la implementación completa de esa frontera,
su complejidad interna ni las capas posteriores de compilación y entrega.

No production activation flag or Phase 0 operation was executed.
