# v0.4.0 release gates

Status: **IMPLEMENTATION GATES PASSED; PRODUCTION NOT ACTIVATED**.

Verified on 2026-09-06, on top of `fed3d50` (release-review closure: canonical correction/expiry
boundaries, real model-refresh operation identities, bounded context deadline):

- deterministic context assembler and `ach-delivery-o200k-v1` budgets;
- exact User/Project authorization on registered models and active claims;
- one shared two-second deadline measured once per request, never recomputed per phase; active
  claims fetched as a bounded, ordered prefix (never the whole ledger) with a true not-rendered
  count; a transaction-local PostgreSQL statement timeout backs every variable-cost query;
- bounded, fenced Working State completion and stale-writer rejection;
- canonical skill and reference sync across four supported host bundles;
- real headless retain-skill evaluation: 120 raw decisions / 40 majority (family, case) outcomes,
  every hard gate passed (`aggregate_recall=1.0`, `wrong_scope=0`, `secret_retention=0`);
- real clean install and v0.3.5-to-v0.4.0 data-preserving migrations, plus a second explicit
  upgrade path proving Working Session/Working State survive `a7b8c9d0e1f2`'s removal of
  `capture_slices`/`context_revisions` while only those two tables disappear;
- one Alembic head: `c9d0e1f2a3b4`;
- real Hindsight 0.9.2 disposable-bank gate (confirmed via `/openapi.json`): 9 passed in 204.73
  seconds;
- maximum context selection (4 User + 5 Project custom models): nine live model reads, 30 warmed
  calls, p95 0.155154 seconds;
- default-like context selection (both built-ins + 2 User + 4 Project custom models): 30 warmed
  calls, p95 0.097423 seconds;
- a controlled single-model failure (a wrapped client sleeping past the shared deadline) fails
  open: the fast peer and Project Metadata are still delivered, the slow model is reported as
  exactly one `model_unavailable` omission, and downstream phases that the deadline leaves no time
  for (Active Claims, Working State) are explicitly reported as `deadline_exceeded` rather than
  silently missing;
- cleanup of every disposable bank created by every gate above, including the ones from the
  iteration that first caught the `observe_model_refresh` bug below.

Two genuine bugs surfaced by this live gate and fixed before it passed (recorded here because a
mocked-only suite could not have caught either):

1. `observe_model_refresh` compared Hindsight's operation id under the key `"id"`; the real
   `get_operation` response carries it under `"operation_id"` and never sends `"id"` at all, so
   the exact-identity guard never matched and a model already synthesized upstream could never be
   observed into `ready`. Fixed to compare `"operation_id"`.
2. The same function only recognized `"pending"`/`"running"` as non-terminal; Hindsight 0.9.2's
   real intermediate status is `"processing"`, which fell through to the failure branch and
   applied a backoff to an operation that was still correctly in progress. Fixed to a
   terminal-status whitelist (`"completed"`/`"failed"`), matching `retention.py`'s own
   `_TERMINAL_STATUSES` shape, so an unrecognized future status defaults to "still in progress."

The final non-live repository count and runtime are recorded in the test-portfolio report. Ruff,
lock-file validation and `git diff --check` are part of the final branch gate and were run clean
before and after the live gate.

Not performed: production activation, production cleanup, deployment, tagging or publishing.

Activation status: `RELEASE_REVIEW_CLOSURE_GATES_PASSED` -- pending the plan's own final
independent-review gate before `production_eligible` is recorded.
