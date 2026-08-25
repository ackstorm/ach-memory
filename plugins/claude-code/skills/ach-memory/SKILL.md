---
name: ach-memory
description: Recall or retain durable user and project context with ach-memory. Use when prior decisions, preferences, or project facts would improve the task.
---

Use `recall` before work that depends on prior context. Use `scope="project"` with the repository's project slug when known; otherwise use `scope="user"`.

Retain only durable decisions, preferences, or project facts. Use `retain` normally and `sync_retain` only when the fact must be searchable in the same task. Never retain credentials, tokens, or other secrets.

Use `forget`, `correct`, and `delete_document` only when the task explicitly calls for curation; `delete_document` is irreversible.
