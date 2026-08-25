---
name: ach-memory
description: How and when to recall and retain durable user and project context with ach-memory. Read before the first memory call in a session — the tools are lazy-loaded and this names them.
---

Memory survives sessions and compaction. Its value is the decision that never reached a file.

## Tools

- write: `retain` (async, returns an operation), `sync_retain` (waits until searchable)
- read: `recall` (facts matching a query), `reflect` (synthesized answer), `list_memories`, `get_memory`
- curate: `correct`, `forget`, `restore`, `delete_document`
- sources: `list_documents`, `get_document`
- async: `get_operation`, `list_operations`, `cancel_operation`

Each tool's own description is authoritative for what it does to your data.

## When

Recall before work that depends on prior decisions, preferences or project facts — and before searching files for one.

Retain once a fact is durable:

- a decision made in conversation: the user confirms, rejects, or states a preference, or a direction is agreed after you proposed it
- a convention, constraint or gotcha that outlives the task
- after compaction, whatever the summary establishes

Skip anything routine, already stored, or secret.

## Scope

Use `scope="project"` with the repository slug for facts about this codebase, `scope="user"` otherwise. An unseen slug mints a project permanently and slugs are never reusable, so pass the one already in use.

## Curating

`correct` rewrites a memory in place — for when the fact was right and the wording was not. To supersede a fact, `forget` it and retain the new one; `restore` undoes a `forget`. `delete_document` removes a source and every memory derived from it, for everyone.
