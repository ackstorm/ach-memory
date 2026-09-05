# v0.4.0 release gates

Status: **IMPLEMENTATION GATES PASSED; PRODUCTION NOT ACTIVATED**.

Verified on 2026-09-05:

- deterministic context assembler and `ach-delivery-o200k-v1` budgets;
- exact User/Project authorization on registered models and active claims;
- two-second shared deadline with concurrent model reads and fail-open omissions;
- bounded, fenced Working State completion and stale-writer rejection;
- canonical skill and reference sync across four supported host bundles;
- real headless retain-skill evaluation: 120/120 decisions, every hard gate passed;
- real clean install and v0.3.5-to-v0.4.0 data-preserving migrations;
- one Alembic head;
- real Hindsight 0.9.2 disposable-bank gate: 7 passed in 65.82 seconds;
- maximum context selection: nine live model reads, 30 warmed calls, p95 0.140193 seconds;
- cleanup of every disposable bank created by the final gate.

The final non-live repository count and runtime are recorded in the test-portfolio report. Ruff,
lock-file validation and `git diff --check` are part of the final branch gate.

Not performed: production activation, production cleanup, deployment, tagging or publishing.

Activation status: `READY_FOR_TEST_PORTFOLIO_CUT_REVIEW`.
