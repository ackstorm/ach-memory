---
name: ach-memory
description: "ALWAYS ACTIVE — persistent memory via ach-memory. Read this skill at the start of every conversation, before any memory call and before searching files or transcripts for a prior decision (tools are lazy-loaded; this skill names them). Proactively `retain` decisions, conventions and preferences — never wait to be asked. Supersedes MEMORY.md and the host's memory directory: never write there, because ach-memory cannot see those files."
---

Memory survives sessions and compaction. Its value is the decision that never reached a file.

ach-memory is the system of record for that context: use it instead of the host's own file-based memory directory and MEMORY.md, and prefer it over grepping files or transcripts. Anything worth remembering goes through `retain`, never into that directory or index, which ach-memory cannot see. Never store secrets.

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

Write every memory in English, whatever language the conversation is in. Retrieval reranks with an English-only cross-encoder, so a fact stored in another language keeps a good embedding score and still loses: measured, the same fact scored 0.99 against its own language and 0.0001 against the English translation of the same question.

## Scope

Use `scope="project"` with the repository slug for facts about this codebase, `scope="user"` otherwise. An unseen slug mints a project permanently and slugs are never reusable, so pass the one already in use.

## Curating

`correct` rewrites a memory in place — for when the fact was right and the wording was not. To supersede a fact, `forget` it and retain the new one; `restore` undoes a `forget`. `delete_document` removes a source and every memory derived from it, for everyone.
