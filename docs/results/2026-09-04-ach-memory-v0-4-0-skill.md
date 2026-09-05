# v0.4.0 retain skill evaluation

The bounded release corpus contains 20 behavior-mapped scenarios, including four holdouts, across two supported agent families and three repetitions: 120 closed decision tuples from six headless model calls. The evaluator stores no reasoning or scenario text. Policy and corpus are validated before execution, every tuple is required exactly once, and scoring rejects hash drift.

The first headless attempt exposed an invalid oracle in case 07: its text combined a rejected option with an accepted final decision while expecting unconditional abstention. That non-release result scored 100% critical and aggregate recall, 98.63% precision, 97.92% abstention, zero secrets and one apparent wrong scope. The scenario was corrected to contain only the rejected option; neither the skill nor the frozen policy changed before the complete rerun.

## Final release run

| Metric | Codex | Claude Code | Aggregate | Gate |
|---|---:|---:|---:|---:|
| critical claim recall | 100% | 100% | 100% | 100% |
| aggregate recall | 100% | 100% | 100% | >=85% |
| retention precision | 100% | 100% | 100% | >=95% |
| abstention accuracy | 100% | 100% | 100% | >=90% |
| memory-type accuracy | 96.97% | 100% | 98.48% | reported |
| wrong scope | 0 | 0 | 0 | 0 |
| secret retention | 0 | 0 | 0 | 0 |

The sole non-gating classification mismatch was Codex repetition 0 on case 17: it selected `fact` where the frozen oracle selected `constraint`; action and Project scope were correct. All hard gates pass.

Recorded runners were `codex-cli 0.152.1` and `Claude Code 2.1.261`; each used its configured default model, so the result supports the two host families but does not claim a model-version comparison.

Hashes:

- corpus: `006129d0bb961cae007fb2e3f8bfd2215f57bcea0286aa93107cc85d910a4437`
- policy: `248085efb1146d70e569ad3c9fc0f9d8adf725189c92bc258be53520b7555cba`
- canonical skill: `ddba9af9b5011b4b5d2de532ae018191bc34a060be01b876fdd600f01da22662`
- decision schema: `c72364096b4fd900ecd85244563eb02538582eb147448d3dfd7066d461149604`

Result: `PASS`.
