---
name: ach-memory
description: Read this before the first `recall` or `retain` of a session. Durable user and project memory: what to store, which scope and type it takes, what never to store, and when to read before you act.
---

# ach-memory

ach-memory is the system of record for durable context across sessions. Use it instead of
`MEMORY.md` or a host memory directory, which ach-memory cannot see. Never store secrets.

## Recall before depending on memory

Call `recall` before work that depends on a prior decision, preference, constraint, convention,
fact or gotcha. Use `scope="all"` when you do not know whether the answer is a personal
preference or a project decision; name one scope only when you do. Use `reflect` only when a
synthesized answer is more useful than exact hits. List stored memories without a query with
`list_memories`, or fetch one by id with `get_memory`. Live tools, repository state and host
policy override a remembered claim when they conflict. Do not narrate routine retrieval.

Query in English whatever language the conversation uses. Claims are stored in English and
retrieval scores the query against them directly: the same question asked in Spanish scores
below the relevance floor and comes back empty, where its English form ranks the right claim
first. Translate the question, not the answer.

## Retain one claim per call

Call `retain` when the user explicitly establishes durable information, or when you preserve the
final result of an explicit decision. Pass `wait: true` to block until the claim is searchable.
Retain when the claim lands -- the decision is accepted, the constraint stated -- not at the end
of the task and not at compaction. Claims that landed while the tools were unavailable are
retained as soon as they are back.
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
- `project_slug` is filled by the proxy from the current repository's `origin` remote (or
  `MEMORY_PROJECT`); pass it yourself only to address another repository. In a directory with
  no remote, project memory has no home: ask which project before inventing a slug.

## Correct, forget, restore, delete

Use `correct` to replace a memory's text under the same `memory_id`. Use `forget` when a new claim
supersedes the old one, followed by a fresh `retain`; use `restore` to undo a mistaken `forget`.
Use `delete_memory` only to erase for good -- nothing brings it back. `history` shows who changed a
memory and why, across every `forget`/`restore`/`correct`/`delete_memory`.

## Context

Load standing context at session start with `load_context`. Unfinished work is not memory: do not
retain task progress, and do not expect the next session to find it here.

The live tool schema is authoritative for fields, authorization and confirmation requirements.
