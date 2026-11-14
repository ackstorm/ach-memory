---
name: ach-memory
description: Read this before the first `recall` or `retain` of a session. Durable user and project memory: what to store, which scope, type and family it takes, how to title it, when to pin it, what never to store, and when to read before you act. Also read it to install or configure ach-memory, or when its tools are missing or answer UNAUTHORIZED.
---

# ach-memory

ach-memory is the system of record for durable context across sessions. Use it instead of
`MEMORY.md` or a host memory directory, which ach-memory cannot see. Never store secrets.

## What session start gives you

Session start prints, in order: the project's purpose; your pinned memories in full; the
project's pinned memories in full; an index of the project's memory, one line per family with
its count and its three newest titles (below 30 memories, the plain list of titles); and one line
of totals. Pinned memories apply to every task. The index shows what exists, not what it says.

## Recall before depending on memory

Memory holds what the repository cannot tell you: a past decision and the alternative it
rejected, a convention where several ways would work, a fact about something outside the repo
(the gateway, the cluster, credential handling, a third-party tool). Code, comments, docs and
tests you can read for yourself. Before work that depends on one of the first kind, call
`recall`, or list the family the index points to with `list_memories` and `family` (titles and
ids; `full: true` adds the text), or read one memory with `get_memory`. Use `scope="all"` when you do not know whether the answer is a personal
preference or a project decision; name one scope only when you do. Use `reflect` only when a
synthesized answer is more useful than exact hits. Live tools, repository state and host
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

- `title`: one line, at most 80 characters, naming what the claim is about the way someone would
  look for it: the component, command, file, setting or service, and the point. "Releases need
  `make release-cut` on a clean main" beats "Release process". It is what the index shows.
- `family`: the area of the project it belongs to. Once a project holds 30 memories it gets a
  closed list of at most 12 families plus `other`, and every project retain must name one from
  it: a family not on the list is refused, and the error lists them. Pick the family whose
  memories you would want next to this one; `other` when none fits. Below 30 it is optional.
  Families are a project thing: user memories need none.
- `memory_type`: `constraint` (must be obeyed), `preference` (defeasible), `decision` (an accepted
  choice with rationale), `convention` (a repeatable way of working), `fact` (durable, not cheaply
  rediscoverable), or `gotcha` (a non-obvious failure pattern). `briefing` remains for older
  memories; for something every session needs, pick the real type and pin it.
- `basis`: `human_explicit` for what the human established, `agent_verified` for what you directly
  verified with a tool or authoritative source. Neither fits -> ask, verify, or abstain.
- optional `tags` for later filtering, and an `operation_id` UUID per intended claim -- reuse it
  verbatim to retry safely.

Pin (`pin: true` on `retain`, or the `pin` tool later) by importance, in this order: first what
does serious or irreversible harm when broken in any task (credentials, client data, other
people's processes or data, destructive or production actions); then what applies to every task
in the project (a rule every change must respect, how to reach the environment, how changes
ship). Never pin what matters only for one component, file, tool or kind of task: the index and
`recall` find it when the work gets there. Pinned text has a cap per bank (3,500 characters for a project, 2,000 for a user); a
pin past it is refused with the list of what is pinned: unpin the least important by the order
above, or shorten something. Decide it yourself; do not ask the user.
Unpin what stopped applying with `pin` and `pinned: false`.

The response's `memory_id` is the handle for `correct`, `forget`, `restore`, `delete_memory`,
`pin`, `get_memory` and `history`. Those tools infer `scope` from the id, so a `recall` hit is enough to
act on it: leave `scope` out unless you know it.

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

## Purpose

When session start says no purpose is stored, read the README or AGENTS.md and retain one
project `fact` tagged `purpose`, titled "Project purpose": two or three sentences on what the
project is and does, who uses it, its main parts and where it runs. To change it, `correct` that
memory.

Unfinished work is not memory: do not retain task progress, and do not expect the next session
to find it here.

The live tool schema is authoritative for fields, authorization and confirmation requirements.

## Setup

The plugin installs a local stdio proxy that resolves the project and forwards to the remote
service. Install with the host's own plugin command:

```bash
claude plugin marketplace add ackstorm/ach-memory && claude plugin install ach-memory@ach-memory
codex  plugin marketplace add ackstorm/ach-memory && codex  plugin add ach-memory@ach-memory
```

Re-running those does not update: both hosts keep the marketplace snapshot they have. Refresh it
first, then restart the host:

```bash
claude plugin marketplace update ach-memory && claude plugin update ach-memory@ach-memory
codex  plugin marketplace upgrade ach-memory && codex  plugin add ach-memory@ach-memory
```

The proxy reads its connection from the environment on every request. Put one of these in the
shell profile (`~/.zshrc`, `~/.bashrc`), open a new terminal, and restart the host:

```bash
# A. ACH login, through the LiteLLM gateway (short-lived JWT, refreshed per request)
export ACH_MEMORY_URL=https://api.ackstorm.ai/mcp/ach-memory
export ACH_MEMORY_TOKEN_COMMAND="/path/to/ach-cli token"   # run by sh: full path, not an alias

# B. Static key
export ACH_MEMORY_URL=https://<host>/mcp/
export ACH_MEMORY_API_KEY=<token from your identity provider>
# export ACH_MEMORY_HEADER=x-litellm-api-key   # only if a proxy blocks Authorization
```

`ACH_MEMORY_TOKEN_COMMAND` wins over `ACH_MEMORY_API_KEY`. `ACH_MEMORY_HEADER` defaults to
`Authorization: Bearer <token>`; any other header carries the bare token, so leave it unset for
option A. Never write a token value into a file the user did not ask for, and never echo one.

Check from a repository: `uvx --from git+https://github.com/ackstorm/ach-memory ach-memory
context load` prints the session start when everything works (a `### Memory index` section in a
project with memories), and nothing where nothing is retained yet. When the tools
answer `UNAUTHORIZED: missing or malformed credential`, no credential reached the service: the
variables are unset in the host's environment, or `ACH_MEMORY_URL` bypasses the gateway that
accepts the token. In Codex, remove any hand-written `[mcp_servers.ach-memory]` entry
(`codex mcp remove ach-memory`); the plugin brings its own.
