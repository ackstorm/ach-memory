# ach-memory SPEC

Status: DRAFT — awaiting Juan Carlos' approval
Version: 0.1.0

## 1. Boundary

> Every layer traces to the spec; every defence traces to a failure reproduced against the real backend.

**The decision rule:** for any line of code, ask — is this *engine behaviour*, or is it
*governance/delivery that must survive an engine replacement*? Anything else does not get written.

| # | Invariant | Why ACH must hold it |
|---|---|---|
| I1 | Scope isolation: a principal reaches exactly the namespaces its identity resolves to; never a client parameter | the engine has no tenancy |
| I2 | Identifier opacity: memory id (ACH-minted, stable), operation id (caller's idempotency key), engine ids (adapter-owned, never public) | engine ids churn on every upsert |
| I3 | Sanitization boundary: secret patterns and control characters never reach the engine | the engine stores whatever it is given |
| I4 | Mutation authorization and journal: every mutation is authorized and recorded — who, on whose behalf, memory id, action, reason, outcome, before/after for corrections | the engine hard-deletes on replacement; history is a product promise |
| I5 | Noise floor: recall withholds hits below a measured semantic floor | the engine never abstains |
| I6 | Capability honesty: an unsupported intention answers `UNSUPPORTED`; ACH never emulates engine intelligence | otherwise ACH becomes part of the engine |
| I7 | Correction ordering: two corrections of one memory cannot land out of order; a read after a correction sees the correction | async engines give no ordering |
| I8 | Redaction: engine namespaces and secrets never appear in responses, errors or logs | operational hygiene the engine cannot do for us |

## 2. Scope

| Capability | One line |
|---|---|
| Bounded standing context | token harness, `len(text) // 4` budget, whole entries only, delivered at session start |
| Users, groups, `on_behalf_of` | identity model behind every mutation and read |
| MCP stdio proxy | runs on the SDK, on the host, resolves project and injects identity |
| Project resolution from git origin | slug derived from the git remote, no client-supplied project id |
| External auth | JWT/JWKS plus platform whoami |
| Automatic bootstrap | users and projects created on first authorized contact |
| Built-in mental models | provisioned at first retain: `user-context`, `project-context`, plus five more (names TBD) |
| Idempotent retain | same `operation_id` replays to the same memory id, no duplicate |
| Governance journal | one row per mutation, actor and outcome, survives engine replacement |
| Secret scrub | content sanitized at the retain boundary before it reaches the engine |
| Working State | transient per-workspace handoff state, outside the bank |
| Measured engine profile as adapter defaults | e.g. `verbatim` extraction — config, not code |
| Adapter contract tests | one suite, fake and real backends parametrised |
| Fake backend | in-memory adapter double; the core suite runs without an engine |
| Compose dev stack with mock LLM | offline end-to-end |
| Helm/CI/release flow | chart, pipeline, version bump and cut, GHCR publish |
| Slug history | retired slugs kept; a git remote rename does not orphan a bank |

## 3. Contract

### 3.1 Intentions

| Intention | Required/optional | Semantics | Journaled? |
|---|---|---|---|
| `retain` | required | remember one claim, returns the memory id | yes |
| `recall` | required | ranked hits for a query, scope-bounded, floor applied | no |
| `reflect` | optional (`synthesis`) | one synthesized answer over scoped memories | no |
| `forget` | required | soft-invalidate one memory | yes |
| `restore` | required | reverse a `forget` | yes |
| `correct` | required | replace a memory's text, same memory id | yes (before/after) |
| `delete` | required | hard-erase one memory and anything derived from it | yes |
| `list` | required | page memories, newest first | no |
| `get` | required | fetch one memory by id | no |
| `history` | ACH-owned | current view plus journal entries for one memory | no |
| `working_state get` | ACH-owned | read this workspace's checkpoint | no |
| `working_state put` | ACH-owned | replace this workspace's checkpoint | no |
| `working_state delete` | ACH-owned | clear this workspace's checkpoint | no |
| `context load` | ACH-owned | deliver bounded standing context, creates nothing | no |

### 3.2 Vocabulary

| Term | Values |
|---|---|
| `scope` | `user`, `project` |
| `memory_type` | constraint, preference, decision, convention, fact, gotcha |
| `basis` | `human_explicit`, `agent_verified` |
| `on_behalf_of` | operator-only: act as another user; recorded in the journal |
| tags | `type:<memory_type>`, `basis:<basis>`, `schema:ach-retain-v1` |

### 3.3 Identifiers

| Id | Minted by | Lifecycle |
|---|---|---|
| memory id | ACH, format `mem_<32hex>` | stable identity of one logical memory; also the engine document id |
| `operation_id` | caller, a UUID | one requested mutation; replayed idempotently on retry |

### 3.4 Journal

One row per `retain`/`forget`/`restore`/`correct`/`delete`, carrying: actor, `on_behalf_of`, action,
memory id, `operation_id`, and details (content hash, `memory_type`, `basis`, tags, reason, before/after
for corrections).

### 3.5 Errors

| Code | Meaning |
|---|---|
| `INVALID_REQUEST` | malformed or out-of-bounds input |
| `UNAUTHORIZED` | no valid principal |
| `FORBIDDEN` | principal cannot act in this scope |
| `NOT_FOUND` | generic missing resource |
| `MEMORY_NOT_FOUND` | unknown memory id |
| `PROJECT_NOT_FOUND` | unknown project |
| `CONTENT_REJECTED_BY_SANITIZER` | content failed the retain-boundary scrub |
| `UPSTREAM_ERROR` | adapter outcome unknown or failed; safe to retry with the same `operation_id` |
| `UNSUPPORTED` | capability not declared by the adapter |

Notices (not errors): `PROJECT_CREATED`, `PROJECT_RENAMED`.

## 4. Adapter contract

`Backend` is an ABC in `memory.backend.base`.

| Required (`@abstractmethod`) | Optional (concrete, raise `UnsupportedCapability`) |
|---|---|
| `capabilities` | `reflect` |
| `provision(bank_id)` | `get_operation` |
| `retain` | `list_operations` |
| `recall` | `cancel_operation` |
| `invalidate` | `provision_mental_models(bank_id, builtins)` |
| `revalidate` | |
| `delete` | |
| `list` | |
| `get` | |

Dataclasses: `Hit`, `MemoryView`, `WriteAck(memory_id, status, operation_ref)`, `Page`,
`TagGroup {"tags", "match": "any"|"all"}` — groups are ANDed together.

| Obligation | One line |
|---|---|
| Idempotent retain | the same `operation_id` on the same memory id never writes a duplicate |
| Unknown outcomes | surface as `UPSTREAM_ERROR`, safe to retry with the same `operation_id` |
| Restart recovery | no mutation is lost or duplicated across an ACH or engine restart |
| No leaked engine ids | engine-internal ids never reach a caller |
| `list` ordering | newest-first; `state=None` returns valid memories only |

Adding an engine = one module `backend/<name>.py` subclassing `Backend`, one line in `get_backend()`,
and the contract suite green (fake and real, parametrised). Core code never reads an
engine-specific setting or imports a concrete adapter.

## 5. Hindsight profile

| Setting/gotcha | Value |
|---|---|
| `retain_extraction_mode` | `verbatim`, set and verified at provision |
| recall | `types=["world"]` |
| read-side shaping | semantic-score floor only |
| dates | three (`occurred_*`, `mentioned_at`, `invalidated_at`), no expiry |
| document id | upsert replaces the document, its units, and the observation twin |
| API path | `/api/v1/...` in-cluster; no prefix on the local compose stack |
| `PATCH /config` body | `{"updates": {...}}`, not the bare dict |
| local LLM | mock: verbatim extraction works, consolidation and model refresh do not |
| `history` | not declared; the ACH journal is the history |

## 6. Delivery

MCP streamable-HTTP server: SDK app, `/health`, header auth resolving to a `Principal`; must accept
a header-less `initialize` (LiteLLM sends none). Stdio proxy on the SDK, running on the host,
resolving project from `MEMORY_PROJECT` or the git origin, injecting slug and workspace. Hooks:
session-start (standing context), pre-compact (Working State nudge), subagent-start. No REST data
plane.

| Tool | Intention | One line |
|---|---|---|
| `retain` | retain | store one claim; `wait: bool = false` blocks until searchable — one tool, one intention, not two near-duplicate tools |
| `recall` | recall | ranked, floor-filtered hits for a query |
| `reflect` | reflect | one synthesized answer |
| `forget` | forget | soft-invalidate one memory |
| `restore` | restore | reverse a forget |
| `correct` | correct | replace a memory's text, same id |
| `delete_memory` | delete | hard-erase one memory |
| `list_memories` | list | page memories, newest first |
| `get_memory` | get | fetch one memory by id |
| `history` | history | journal entries for one memory |
| `working_state_get` | working_state get | read this workspace's checkpoint |
| `working_state_put` | working_state put | replace this workspace's checkpoint |
| `working_state_delete` | working_state delete | clear this workspace's checkpoint |
| `load_context` | context load | deliver bounded standing context |
| `get_operation` | — | engine-side async outcome; only if the adapter declares `operations` |

## 7. Storage

| Table | Key columns |
|---|---|
| `users` | id, bank_id, created_at |
| `external_identities` | user_id, provider, subject |
| `groups` | id, name — membership comes from the auth provider at request time |
| `projects` | id, slug, bank_id, owner_user_id, owner_group_id (nullable), created_at |
| `project_slugs` | project_id, slug, retired_at |
| `audit_events` | actor, on_behalf_of, action, memory_id, operation_id, details, created_at |
| `working_states` | user_id, workspace_id, state (JSON), updated_at |

No tenants table.

Bank ids are deterministic and readable: `user_<user_id>` for a user scope, `project_<slug>` for a
project scope. The row stores it; nothing needs the row to recompute it. Banks created by the
predecessor (`project_<uuid>`) stay in the engine untouched and unmounted.

## 8. Non-goals

REST data plane; tenants; metrics/observability; rate limiting; dashboard; expiry/`valid_until`;
evidence/trigger fields; dedup across operation ids; a ledger of engine state owned by ACH;
directives; a host installer CLI; a documents API.
