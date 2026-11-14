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
| Bounded standing context | at session start, in order: the newest `purpose`-tagged claim verbatim; the user's pinned memories in full; the project's pinned memories in full; an index of the project's memories, one line per family that holds any, with its count and its 3 newest unpinned titles (below 30 memories, the flat list of titles); one line of totals. Without a project, the user's pinned memories alone. Pinned text stops at each bank's cap, newest first, with a line counting what was left out. To keep the whole within `context_budget_chars` (8,000) the index sheds sample titles (3, 2, 1, none per family) and never a family line. Activation text plus context must stay under Claude Code's ~10,000-character inline limit for SessionStart output (`tests/test_plugins.py`). Hosts deliver it again after compaction |
| Titles, families, pins | every memory has a title (≤80 chars) and, in a project, a family; a pin is optional. A project's family list is closed (≤12 plus `other`), drafted once by an LLM (`MEMORY_LLM_*`; unset, nothing is drafted and families stay optional) when the project first holds 30 valid memories, redrafted with the reason while a family is named after the project or holds over about a third of them, and editable by its owner. Pinned text is capped per bank (3,500 chars project, 2,000 user); a pin past the cap is refused with the list of what is pinned. Nothing is ever updated automatically by title, family or key |
| Read log | every session-start load, recall, list and get: who, project, tool, query or family, result count, latency; `read_daily` view per day, project and tool |
| Users, groups, `on_behalf_of` | identity model behind every mutation and read |
| MCP stdio proxy | runs on the SDK, on the host, resolves project and injects identity |
| Project resolution from git origin | slug derived from the git remote; a hand-typed spelling of the current repository resolves to it on calls without a `memory_id`, so it cannot split the repository into a second project |
| External auth | JWT/JWKS plus platform whoami |
| Automatic bootstrap | users and projects created on first authorized contact |
| Idempotent retain | same `operation_id` replays to the same memory id, no duplicate |
| Governance journal | one row per mutation, actor and outcome, survives engine replacement |
| Secret scrub | content sanitized at the retain boundary before it reaches the engine |
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
| `recall` | required | ranked hits for a query, scope-bounded, floor applied; `scope="all"` merges the user and project banks by score, project first on a tie; empty for a project nobody retained into | no |
| `reflect` | optional (`synthesis`) | one synthesized answer over scoped memories | no |
| `forget` | required | soft-invalidate one memory | yes |
| `restore` | required | reverse a `forget` | yes |
| `correct` | required | replace a memory's text, same memory id | yes (before/after) |
| `delete` | required | hard-erase one memory and anything derived from it | yes |
| `list` | required | page memories, newest first, from ACH's own record: titles and ids, text on request; filter by family, type, pin, state; empty for a project nobody retained into | no |
| `pin` | ACH-owned | pin or unpin one valid memory; refused past the bank's cap | yes |
| `families` | ACH-owned | show a project's family list with counts; replace it (owner or operator), renaming families and moving their memories | yes (replace) |
| `get` | required | fetch one memory by id | no |
| `history` | ACH-owned | current view plus journal entries for one memory | no |
| `context load` | ACH-owned | deliver bounded standing context, creates nothing | no |

Reads (`context load`, `recall`, `list`, `get`) are not journaled; they go to the read log (§7).

### 3.2 Vocabulary

| Term | Values |
|---|---|
| `scope` | `user`, `project` |
| `memory_type` | constraint, preference, decision, convention, fact, gotcha, briefing (kept for older memories; pinning replaced it) |
| `title` | one line, ≤80 chars, required on retain |
| `family` | lowercase-hyphenated; in a project with a list, required and on the list |
| `pin` | delivered in full at session start; capped per bank |
| `basis` | `human_explicit`, `agent_verified` |
| `on_behalf_of` | operator-only: act as another user; recorded in the journal |
| tags | `type:<memory_type>`, `basis:<basis>`, `schema:ach-retain-v1` |

### 3.3 Identifiers

| Id | Minted by | Lifecycle |
|---|---|---|
| memory id | ACH, format `mem_<32hex>` | stable identity of one logical memory; also the engine document id |
| `operation_id` | caller, a UUID | one requested mutation; replayed idempotently on retry |

### 3.4 Journal

One row per `retain`/`forget`/`restore`/`correct`/`delete`/`pin`/`unpin`, and one per family-list
change (`project.families`, drafted or set), carrying: actor, `on_behalf_of`, action, memory id,
`operation_id`, and details (content hash, `memory_type`, `basis`, tags, title, family, pinned,
reason, before/after for corrections and family lists).

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
| `PIN_CAP_EXCEEDED` | a pin would put the bank's pinned text over its cap; the message lists what is pinned |
| `UNSUPPORTED` | capability not declared by the adapter |

Notices (not errors): `PROJECT_CREATED`, `PROJECT_RENAMED`.

## 4. Adapter contract

`Backend` is an ABC in `memory.backend.base`.

| Required (`@abstractmethod`) | Optional (concrete, raise `UnsupportedCapability`) |
|---|---|
| `capabilities` | `reflect` |
| `provision(bank_id)` | `get_operation` |
| `retain` | |
| `recall` | |
| `invalidate` | |
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

Retrieval changes are gated on a reproduced failure, in this order: adapter configuration
first, then ingestion, then candidate width, and a second index or reranker last. Reproduce it
with `scripts/probe-recall.py` against production before writing anything -- on 2026-11-05 that
probe found 21/23, 20 of them at rank 1, which retires "the right memory ranks outside the top
candidates" as a premise; its two misses are the Spanish queries in §5.

## 5. Hindsight profile

| Setting/gotcha | Value |
|---|---|
| `retain_extraction_mode` | `verbatim`, set and verified at provision |
| `enable_observations` | `false`, set and verified at provision: no consolidation, no derived views. Observation units written before stay in the bank; `reflect` still searches them (its `search_observations` tool keys off the request's fact types, not this flag) and now reads them as stale |
| recall | `types=["world"]` |
| read-side shaping | semantic-score floor only |
| dates | three (`occurred_*`, `mentioned_at`, `invalidated_at`), no expiry |
| document id | upsert replaces the document, its units, and the observation twin |
| API path | `/api/v1/...` in-cluster; no prefix on the local compose stack |
| `PATCH /config` body | `{"updates": {...}}`, not the bare dict |
| local LLM | mock: verbatim extraction works, `reflect` does not |
| `history` | not declared; the ACH journal is the history |
| semantic floor | `0.60`, calibrated on an English corpus and measured against lowering it on 2026-09-17 (`scripts/probe-recall.py`, floor temporarily 0 in production): Spanish probes that fail score 0.536 and 0.595 where their English form scores ~0.72 at rank 1, and catching both needs 0.53, which takes irrelevant hits per query from 2.6 to 6.3 (0.58 catches one, for 3.3). Agents query in English instead; the floor stays. Re-measure the same way before reopening |
| identifier recall | Hindsight 0.9.2 fuses BM25 with semantic search and reranks, so identifiers inside code spans are retrievable: of 38 sampled from production banks on 2026-11-05, 35 reached the top 10 and 25 rank 1. The misses were `customInstructions`, `projects` and `basis`. No ACH retrieval layer is needed |

## 6. Delivery

MCP streamable-HTTP server: SDK app, `/health`, header auth resolving to a `Principal`; must accept
a header-less `initialize` (LiteLLM sends none). Stdio proxy on the SDK, running on the host,
resolving project from `MEMORY_PROJECT` or the git origin, injecting the slug. Hooks:
session-start (standing context; Claude Code and Codex run it again after a compaction, opencode and pi keep it in the system prompt), pre-compact (retain nudge), subagent-start. No REST data plane.

| Tool | Intention | One line |
|---|---|---|
| `retain` | retain | store one claim with its `title`, `family` and optional `pin`; `wait: bool = false` blocks until searchable — one tool, one intention, not two near-duplicate tools |
| `recall` | recall | ranked, floor-filtered hits for a query; `scope="all"` spans both banks |
| `reflect` | reflect | one synthesized answer; same `memory_types`/`basis` filter as recall |
| `forget` | forget | soft-invalidate one memory |
| `restore` | restore | reverse a forget |
| `correct` | correct | replace a memory's text, same id |
| `delete_memory` | delete | hard-erase one memory |
| `list_memories` | list | titles and ids, newest first; `family`, `memory_types`, `pinned`, `state`; `full=true` adds the text |
| `get_memory` | get | fetch one memory by id |
| `history` | history | journal entries for one memory |
| `load_context` | context load | deliver bounded standing context again |
| `pin` | pin | pin or unpin one memory |
| `list_families` | families | a project's family list with counts |
| `set_families` | families | replace a project's family list; for people, through `ach-memory families set` |
| `get_operation` | — | engine-side async outcome; only if the adapter declares `operations` |
| `rename_project` | — | move a project to a new slug after its git remote moved; the old slug keeps forwarding |

The tools that address one memory by id (`forget`, `restore`, `correct`, `delete_memory`, `get_memory`, `history`) take `scope` as optional: left out, it is read from that memory's `memory.retain` journal row, and the bank the caller resolves to must own the row (I1), else `MEMORY_NOT_FOUND`.

## 7. Storage

| Table | Key columns |
|---|---|
| `users` | id, created_at — the bank is `user_<id>`, derived, never stored |
| `external_identities` | user_id, provider, subject |
| `groups` | id, name — membership comes from the auth provider at request time |
| `projects` | id, slug, bank_id, owner_user_id, owner_group_id (nullable), families (nullable), created_at |
| `memories` | memory_id, scope, owner_id, title, family, pinned, memory_type, state, chars, created_at — ACH's own record of each memory; the text stays in the engine |
| `read_events` | actor, project, tool, scope, query, family, results, latency_ms, created_at; view `read_daily` |
| `project_slugs` | project_id, slug, retired_at |
| `audit_events` | actor, on_behalf_of, action, memory_id, operation_id, details, created_at |

No tenants table.

Bank ids are opaque and minted once: `user_<user_id>` for a user scope (the user id is itself a
hash), `project_<uuid4>` for a project scope. Only the row knows the mapping, so renaming a project
is a `project_slugs` change and the engine never hears about it. Banks created by the predecessor
(`project_<uuid>`, same shape) stay in the engine untouched and unmounted.

## 8. Non-goals

REST data plane; tenants; metrics/observability beyond the read log; rate limiting; dashboard;
expiry/`valid_until`; evidence/trigger fields; dedup across operation ids; a ledger of engine
content owned by ACH (the `memories` record holds title, family, pin, state and size, never the
text); directives; a documents API.

**Derived views.** No LLM-synthesized summary of memories reaches an agent: no mental models, no
consolidation. Measured 2026-11: a model rewrite turned one note into its opposite, full-mode
refreshes dropped and recovered content with no new retain, and a deterministic index of
1.3k-2.9k chars fits the session start with closed families.

**Automatic updates by key.** A title, family or topic key never decides that a retain replaces
an earlier memory. Measured 2026-11: topic keys agreed 0.65-0.69 between two labelling runs, and
a reused key was a false merge in 20-36% of hand-checked cases.

**Automatic capture.** No conversation observer, transcript reader, continuous tool inspection,
candidate extraction or background distillation. A memory exists because an agent decided it was
worth one and asked; processing all activity to guess is not worth its cost, and it is not what
this service is for.

**Transient task state.** Checkpoints, resume summaries and "where the work was left" are not
memory. Working State shipped in 0.1.x and was removed in 0.2.0 (`56bce30`): nothing ever read
it, the schema needed a task identifier no agent could compute, and the host's own compaction
summary already carries it. A durable conclusion found mid-task is a `retain`, not a checkpoint.

**A second presentation of a memory.** No compact/extract view and no batch read. The index is
titles only, and `list_memories` returns text only on request.
