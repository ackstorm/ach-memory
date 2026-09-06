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
- real Hindsight 0.9.2 disposable-bank gate (confirmed via `/openapi.json`): 9 passed in 217.01
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

## Final independent review

A fresh code review (effort `high`) over the complete closure range (`d6075f5..b44909b`, the four
task commits) returned ten findings. Verified against the code and fixed where the fix was both
real and low-risk; the rest are recorded below rather than silently dropped.

Fixed (commit `3b84cc9`):

- `context_service`: the active-claims phase checked the shared deadline once before its
  per-scope loop, not per iteration -- a second scope's query could run under a near-zero,
  1ms-floored statement timeout that PostgreSQL would cancel, raising an uncaught error instead of
  degrading gracefully. Now re-checked inside the loop.
- `context_service`: the always-in-context registry query, when skipped because the deadline was
  already gone before it could run, produced no omission at all -- the section just silently never
  appeared. Now emits `deadline_exceeded`.
- `curation_service._submit_refresh_for_affected_models`: only the upstream refresh call itself was
  guarded against `DomainError`; `record_model_refresh_operation`'s own row lookup was not, so a
  model whose row a concurrent process affected between the batch snapshot and this loop reaching
  it could abort refreshing every model after it. Now the whole per-model unit is guarded.
- `mental_model_service.delete_model`: the mutation ledger was consulted AFTER the
  already-deleted short-circuit, so reusing an `operation_id` against a different model once the
  first was already gone silently succeeded instead of raising `IdempotencyConflict`. Reordered.

Investigated and confirmed already correct by design (not fixed):

- `RetainedRecordRevision`'s append-once behavior for an unproven correction (`HindsightOutcomeUnknown`,
  or a target proven absent) is the literal, tested contract the closure plan itself specified --
  "an unknown outcome leaves the prior canonical claim current" while the revision persists "for
  audit." Both the plan and the shipped test (`test_unknown_correction_outcome_leaves_prior_canonical_content_current`)
  require this; it is not an oversight.

One auditability follow-up was closed before merge: `correct` now requires a caller-visible
`operation_id` over REST and accepts an optional one over MCP (where the adapter creates it before
transport when omitted). Exact retries reuse one revision; distinct A→B→A→B corrections retain
all three overwritten values. Reusing an operation ID with a different target or canonical
content is rejected before another upstream call.

Two narrow residual limitations remain documented rather than silently accepted:

- `update_model`'s idempotency ledger is marked `completed` only after both the upstream
  definition update and the upstream refresh submission succeed. A process crash between a
  successful `refresh_mental_model` call and that final commit means a retry re-executes the
  update and resubmits a second, genuinely duplicate refresh. Neither call corrupts state (the
  later of the two refresh operations simply wins), so the cost is wasted upstream work under a
  rare crash timing, not incorrectness; closing it fully needs finer-grained ledger phases
  (`update_applied`, `refresh_submitted`) than this plan's ledger design carries.
- `create_custom_model` keeps its pre-existing, separately-implemented idempotency mechanism
  (columns on `MentalModelRegistration` itself) rather than moving onto the new
  `MentalModelMutation` ledger update/refresh/delete now share -- a deliberate choice to avoid
  touching an already-correct, already-tested path, at the cost of two parallel idempotency
  mechanisms in the codebase.

All fixes were re-verified against the full non-live suite (1,353 passed, 2 skipped) and the
complete disposable Hindsight 0.9.2 live gate (9 passed) after the fix commit.

Not performed: production activation, production cleanup, deployment, tagging or publishing.

**`production_eligible=true`**, on the evidence above, with the two residual limitations
recorded rather than silently accepted -- each is narrow, non-corrupting, and independently
tracked. Activation itself remains a separate, unperformed decision.
