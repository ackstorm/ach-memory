# ach-memory v0.4.0 typed retain and lifecycle — compatibility gate

Status: **NON-LIVE GATE PASSED — LIVE HINDSIGHT GATE NOT RUN**

This branch (`feat/ach-memory-v0.4.0-retain-lifecycle`) was executed in a sandboxed session with
no reachable Hindsight deployment (no `MEMORY_HINDSIGHT_URL` configured, no listening backend on
this host). `tests/test_v040_hindsight_live.py` and `scripts/v040-hindsight-preflight.py` exist and
are structurally verified — they compile, lint clean, and every guarded test skips with the exact
`HINDSIGHT_V040_CONFIRM=disposable-banks-only` message when unconfirmed — but no assertion in them
has been proven against a real Hindsight 0.9.2 instance. Activation MUST NOT proceed on this
branch's say-so alone; whoever runs the live gate next should replace this document's live section
before treating retain/lifecycle as compatible with the target deployment.

## Non-live gate (run in this session)

```
uv run pytest tests/test_sanitization.py tests/test_retention.py tests/test_memory_api.py \
    tests/test_read_service.py tests/test_curation_service.py tests/test_expiry.py -q
```
Result: 130 passed.

```
uv run ruff check src tests scripts/v040-hindsight-preflight.py
```
Result: clean.

Full non-integration suite (`uv run pytest -q -m "not integration"`): 2192 passed, 3 skipped, 2
deselected, no failures.

## Live gate (NOT run — no reachable disposable deployment in this environment)

To run it against a real disposable Hindsight 0.9.2 deployment:

```
MEMORY_HINDSIGHT_URL=<disposable deployment> MEMORY_HINDSIGHT_API_KEY=<key> \
    HINDSIGHT_V040_CONFIRM=disposable-banks-only \
    uv run pytest tests/test_v040_hindsight_live.py -q
```

A non-loopback `MEMORY_HINDSIGHT_URL` host also requires `HINDSIGHT_V040_ALLOWED_HOST=<host>` or
the fixture skips rather than risk a non-disposable target.

Expected sections once run, filled in by that session (counts/durations/statuses/hashes only —
never claim text, a bank ID, a credential or evidence):

| Check | Status | Notes |
|---|---|---|
| `v040-hindsight-preflight.py` (chunks mode: 1 chunk, 0 entities, 0 extraction tokens) | PENDING | |
| Exact strategy: one 4096-byte claim → one verbatim `world` fact | PENDING | |
| Scale recall: 500 siblings, 10 frozen probes, source fact in top 10 every time | PENDING | |
| Consolidation cites source facts; forget removes/invalidates the dependent observation | PENDING | |
| Exact retry (same `operation_id`) after a simulated lost response is safe | PENDING | |
| 429 / timeout / unavailable backend surface as typed, non-leaking errors | PENDING | |
| Every disposable bank created was deleted, including on failure | PENDING | |

## What this branch changed, for the session that runs the live gate

- Retain is now a single typed, sanitized, idempotent `ach-exact-v1` write (Tasks 1–3).
- Recall/current reads use v0.4.0 tags (`schema:ach-retain-v1`, `type:*`) and withhold a bank whose
  currentness cannot be proven (Task 4).
- Correction, forget, restore and hard delete are outcome-safe: an indeterminate Hindsight response
  withholds the physical bank instead of guessing (Task 5).
- Expiry is bounded and access-driven — at most 32 rows per authorized recall/reflect, no daemon
  (Task 6).

See the plan (`docs/superpowers/plans/2026-09-04-ach-memory-v0-4-0-typed-retain-lifecycle.md`) and
spec (`docs/specs/2026-09-03-ach-memory-v0.4.0.md`) for the full contract these gates check.
