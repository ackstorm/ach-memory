# Salvage list for the successor

Decided 2026-09-14. Value is not lines: the engine data survives untouched,
the verified knowledge lives in `docs/lessons.md`, and roughly 1,200 lines of
code have already been proven against the real engine. Everything else stays
in this repository.

## What the successor keeps (product scope)

| Capability | Here | Port |
|---|---|---|
| Bounded standing context ("token harness") | `delivery.py` (tiktoken), `context_service.py`, `context load`, session-start hook | same shape; `len(text) // 4`, no tiktoken |
| Users, groups, `on_behalf_of` | `users`, `external_identities`, `groups`, `auth/principal.py` | same model, three tables |
| MCP stdio proxy | `mcp/proxy.py` (hand-rolled) | rewrite on the `mcp` SDK (`Server` + `stdio_server`, `ClientSession` over `streamable_http_client`); lift only `auth_headers`, `resolve_project_context`, `resolve_workspace_context` |
| Project resolution from git | `slugs.py`, `proxy.resolve_project_context` | as is |
| External auth | `auth/providers/jwt_provider.py`, `auth/providers/platform.py` | as is |
| Automatic bootstrap of users and projects | `auth/provisioning.link_identity`, `bootstrap.provision_before_retain` | as is |
| Built-in mental model at first retain | `builtin_models.py` (`user-context`, `project-context` v4) | as is; five more built-ins are a new requirement (name + prompt + source tags each) |

## Ported in addition (approved)

1. **Idempotent retain by `operation_id`** — advisory lock + content hash +
   journal replay (`retain.py`). Hindsight has no idempotency key.
2. **Governance journal** — actor, `on_behalf_of`, action, before/after for
   retain/forget/correct/delete (`audit.py`, `audit_events`). Engine history
   is lost on replace and has no actor.
3. **Secret scrub at the retain boundary** — `sanitization.normalize_claim`
   and the pattern set (QA F-06 shapes), plus `scripts/leakscan.py` over
   smoke output. Drop `redact_secrets`/`sanitize_ref` (no callers).
4. **Working State** — transient per-workspace state outside the bank,
   written by the pre-compact hook. Concept and table only; today's 861 lines
   become ~100.
5. **Measured engine profile as adapter defaults** — `retain_extraction_mode=verbatim`,
   recall `types=["world"]`, semantic-score floor, tag vocabulary
   `type:`/`basis:`/`schema:ach-retain-v1`. Config, not code.

## Proposed in addition (pending Juan Carlos' validation)

6. **Adapter contract test suite** — the 19 tests that run against both the
   fake and a real Hindsight. Caught three real bugs the day they were
   written (metadata leak, list ordering, empty history).
7. **`fake` backend** — in-memory adapter double; the whole suite runs
   without an engine. Dedupe `_matches_tag_groups` into `base.py`.
8. **Compose dev stack with mock LLM** + `make e2e` — offline end-to-end.
9. **Helm chart + CI + release flow** (`release-bump`/`release-cut`, GHCR
   publish, Flux roll) — rename and reuse; the gotchas are in `lessons.md`.
10. **Slug history** — `project_slugs` retired slugs + `PROJECT_RENAMED`
    notice, so a git remote rename does not orphan a bank. ~40 lines.

Runners-up, only if a caller appears: `delete_project` (empty bank only),
bank-id redaction on responses.

## Code lifted as is

| Piece | Lines | Note |
|---|---|---|
| `backend/base.py` | 117 | adapter contract; docstrings are the contract |
| `backend/hindsight.py` + `hindsight/client.py` `_request` | ~400 | drop unused recall/reflect params, dead methods, `RetainItem` extras, `HindsightOutcomeUnknown` |
| `backend/fake.py` | 171 | if item 7 is approved |
| `retain.py` | 201 | item 1 |
| `audit.py` | 79 | item 2 |
| `sanitization.py` (used part) | ~120 | item 3 |
| `auth/providers/*`, `auth/principal.py` | ~460 | dedupe `_groups` into `principal.py` |
| `auth/provisioning.py`, `bootstrap.py` | ~210 | |
| `slugs.py` + proxy `resolve_*`/`auth_headers` | ~250 | |
| `builtin_models.py` | 65 | |
| `scripts/leakscan.py`, `scripts/mcp-smoke.py` | ~280 | smoke template |

## Prose lifted (product, not code)

- `plugins/shared/ach-memory/SKILL.md` — the agent's operating manual.
- Tool descriptions from `mcp/memory_tools.py` — extract to one file.
- Plugin hooks (`session-start.sh`, `pre-compact.sh`, `subagent-start.sh`)
  and how each host (Claude Code, Codex, OpenCode, Pi) registers an MCP
  server — one document replaces `cli.py`.

## Data

- Hindsight production banks are engine data and survive the harness. Keep
  the bank naming so the successor reads them. `ach-memory` (350 memories)
  is never deleted.
- Postgres: export `projects`, `project_slugs`, `users`, `external_identities`,
  `groups` as JSON (tens of rows). Archive `audit_events`; drop the rest.

## Explicitly not ported

REST data plane (`api/*` except health), mental-model registry and mutation
state machine, activity trail and metrics, rate limiting, tenants table,
operations mirror, `cli.py` installer, dashboard, `e2e.py`/`bench.py`/probes,
migrations, SPEC-v1, `docs/superpowers/*`, `docs/plans/*`.

## Mechanics

1. Last commit here: this file + `docs/lessons.md`. Tag `v0.8.0-final`.
2. Rename the repository to `ach-memory-deprecated`; README points at the
   successor.
3. Successor, first commit: spec v3 §1–§3 trimmed, `docs/lessons.md`,
   `backend/`, `sanitization.py`, `SKILL.md`. Nothing else until the spec is
   approved.
