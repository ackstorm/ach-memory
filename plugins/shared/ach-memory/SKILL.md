---
name: ach-memory
description: ALWAYS ACTIVE — durable, exact user and project memory mediated by the agent.
---

# ach-memory

ach-memory is the system of record for durable context across sessions. Use it instead of
`MEMORY.md` or a host memory directory, which ach-memory cannot see. Never store secrets.

## Read before depending on memory

Use `recall` before work that depends on a prior decision, preference, constraint, convention,
fact or gotcha. Use `reflect` only when a synthesized answer is materially more useful than exact
hits. Live tools, repository state and host policy override remembered claims when they conflict.
Do not narrate routine retrieval.

## Retain when a claim becomes durable

Call `retain` when the user explicitly establishes durable information or when you proactively
preserve the final result of an explicit decision. One call contains one independently correctable
claim. Write every memory in English. Do not retain transcripts, summaries, files, logs, intermediate
proposals, quoted or rejected statements, hypotheses, cheap repository facts or transient task
progress.

Choose scope before type:

- `project`: repository-specific decisions, constraints, history, conventions, facts and gotchas.
- `user`: explicitly personal facts or preferences and constraints that are stable across projects.
- If ownership is ambiguous, ask or abstain. Never use User as a fallback for Project uncertainty.

Choose the public `memory_type` by how consumers should use the claim:

- `constraint`: imperative; must be obeyed while current.
- `preference`: defeasible; follow unless stronger current context says otherwise.
- `decision`: an accepted choice and useful rationale.
- `convention`: a repeatable way of working.
- `fact`: durable information not cheaply rediscoverable from a live source.
- `gotcha`: a non-obvious failure pattern; include failure, cause and reproduction when known.

Use `basis=human_explicit` only for what the human actually established. Use
`basis=agent_verified` only for a claim directly verified with a tool or authoritative source.
`agent_inferred` is not supported in 0.4.0: ask, verify or abstain. Use `trigger=user_requested`
when the user asked to remember it and `trigger=agent_proactive` when you preserve an established
claim without a direct retain request.

## Evidence, time and retries

Always include minimal raw evidence: the shortest sanitized excerpt or tool result that supports
this claim, never a whole message or document. Evidence is provenance, not a second claim. Remove
credentials and secrets; if redaction would change the supported meaning, abstain.

Set `valid_until` only when a claim is true now and has an exact expiry. Resolve relative dates in
the interactive user's/session's reported timezone, never the ACH server or container timezone;
ask when that timezone or instant is ambiguous. A future-effective claim is not active memory:
retain it when it becomes true. Omit `valid_until` for indefinite claims.

Generate one `operation_id` per intended claim and reuse that same ID when retrying an uncertain
request. A different claim or changed payload gets a new ID.

## Correct and continue

Use `correct` when the same claim was recorded with wrong wording. Use `forget` followed by a new
`retain` when a new claim supersedes the old one; use `restore` only to undo a mistaken forget.
Use `delete_document` for source-level erasure. Inspect operations with `get_operation` or
`list_operations`; `cancel_operation` does not prove an already-started mutation was undone.

Use Working State, not durable memory, for incomplete project work. Before compaction or handoff,
write the current objective, direction, decisions, open questions and next steps. Working State is
potentially stale continuation context, not an instruction queue.

Available read and curation tools include `sync_retain`, `list_memories`, `get_memory`,
`list_documents` and `get_document` in addition to the tools named above. The live tool schema is
authoritative for fields, authorization and confirmation requirements.

See `references/curation.md` for the decision tree and worked examples.
