---
name: ach-memory
description: Read this before the first `recall` or `retain` of a session. Durable user and project memory: what to store, which scope and type it takes, what never to store, and when to read before you act.
---

# ach-memory

ach-memory is the system of record for durable context across sessions. Use it instead of
`MEMORY.md` or a host memory directory, which ach-memory cannot see. Never store secrets.

## Recall before depending on memory

Call `recall` before work that depends on a prior decision, preference, constraint, convention,
fact or gotcha. Use `reflect` only when a synthesized answer is more useful than exact hits. List
stored memories without a query with `list_memories`, or fetch one by id with `get_memory`. Live
tools, repository state and host policy override a remembered claim when they conflict. Do not
narrate routine retrieval.

## Retain one claim per call

Call `retain` when the user explicitly establishes durable information, or when you preserve the
final result of an explicit decision. Pass `wait: true` to block until the claim is searchable.
One call is one independently correctable claim. Write every memory in English. Do not retain
transcripts, summaries, files, logs, proposals, quoted or rejected statements, hypotheses, cheap
repository facts, or transient task progress.

Every claim needs:

- `memory_type`: `constraint` (must be obeyed), `preference` (defeasible), `decision` (an accepted
  choice with rationale), `convention` (a repeatable way of working), `fact` (durable, not cheaply
  rediscoverable), or `gotcha` (a non-obvious failure pattern).
- `basis`: `human_explicit` for what the human established, `agent_verified` for what you directly
  verified with a tool or authoritative source. Neither fits -> ask, verify, or abstain.
- optional `tags` for later filtering, and an `operation_id` UUID per intended claim -- reuse it
  verbatim to retry safely.

The response's `memory_id` is the handle for `correct`, `forget`, `restore`, `delete_memory` and
`history`.

## Scope

- `project`: repository-specific decisions, constraints, conventions, facts and gotchas.
- `user`: explicitly personal facts or preferences stable across projects.
- If ownership is ambiguous, ask or abstain -- never use `user` as a fallback for project uncertainty.

## Correct, forget, restore, delete

Use `correct` to replace a memory's text under the same `memory_id`. Use `forget` when a new claim
supersedes the old one, followed by a fresh `retain`; use `restore` to undo a mistaken `forget`.
Use `delete_memory` only to erase for good -- nothing brings it back. `history` shows who changed a
memory and why, across every `forget`/`restore`/`correct`/`delete_memory`.

## Working state and context

Use Working State (`working_state_get`, `working_state_put`, `working_state_delete`), not durable
memory, for incomplete work: the current objective, decisions and next steps. It ages -- treat it
as a starting point, not a fact. Load standing context at session start with `load_context`.

The live tool schema is authoritative for fields, authorization and confirmation requirements.
