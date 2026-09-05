# v0.4.0 test portfolio cut report

Baseline: integrated pre-Plan-4 `main` at `2cb1753`: 2,240 non-integration tests passed, 4 skipped and 6 live tests deselected in 124.66 seconds.

The post-change count is finalized by the release gate; the latest complete checkpoint is 1,309 passed and 2 skipped in 88.42 seconds, before four additional positive release-gate assertions were added. No test exists merely to remember a removed product surface.

| Tree | Baseline files / lines | Current files / lines | Change |
|---|---:|---:|---:|
| `src/memory` | 86 / 20,483 | 76 / 14,630 | -10 files / -5,853 lines |
| `tests` | 111 / 39,767 | 87 / 24,715 | -24 files / -15,052 lines |

The retained suite maps to current product responsibilities:

- identity, authentication, authorization, tenancy and audit;
- User/Project resolution, ownership, slug lifecycle and opaque bank routing;
- typed retain, sanitization, evidence, expiry and currentness;
- mental-model governance, quota, refresh fencing and bootstrap;
- deterministic bounded context delivery;
- exact Working State storage and ordering;
- REST/MCP contracts, host lifecycle integration, canonical skill and packaging;
- clean/upgrade migration and disposable Hindsight compatibility gates.

No orphan group was found after the product cut. The remaining repetition is mostly deliberate cross-surface coverage: the same authorization or scope invariant is checked at service, REST and MCP boundaries. Candidates for a later `0.4.x` consolidation review are the large parameter matrices in `test_mcp_tools.py`, repeated slug-resolution cases across API/MCP tests and overlapping response-shape snapshots. They should be reduced only after mutation coverage shows an identical failure signal; their current risk is tenant or scope isolation, not cosmetic behavior.

Recommended cut: stop here for 0.4.0. The portfolio has already removed roughly 42% of tests and 38% of test lines; further reduction is a named `0.4.x` task based on duplicate failure signals, not a target count.

`READY_FOR_TEST_PORTFOLIO_CUT_REVIEW`
