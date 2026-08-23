# ach-memory

Multi-tenant memory service for coding agents, over
[Hindsight](https://github.com/vectorize-io/hindsight) (MIT).

`SPEC-v1.md` is the contract. Build plans live in `docs/superpowers/plans/`.

**This build covers Plan 1 through Plan 6: `scope=user`
and `scope=project` memory, groups and project ownership, reflect, curation
(list/get/forget/restore/correct), documents, async operations, the §13
provenance/audit trail (now readable via `GET /v1/admin/audit`), the
fifteen-tool MCP surface (§11), per-credential write rate limiting (§20), the
master-key-only destructive plane (whole-bank clear/delete, retired-slug
release), key lifecycle (list/revoke a user's keys), REST-only directive and
mental-model management (§14), and Plan 6's review-remediation fixes (bank
materialization no longer configures anything, `update_mode="append"` proven
end to end, a `git_locator` repair path, and the boundary/error-mapping
hardening in `SPEC-v1.md` §18).** A Helm
chart (`deploy/helm/ach-memory`, see `deploy/helm/README.md`) and CI
(`.github/workflows/ci.yml`: lint, test, and a build-the-image-and-prove-it-starts
job) package and gate this build.

## What works today

```
POST   /v1/users                        master key   provision a user (id optional)
GET    /v1/users/{user_id}              master key
GET    /v1/users                        master key   list users in the tenant
POST   /v1/users/{user_id}/keys         master key   mint a user key, plaintext once
GET    /v1/users/{user_id}/keys         master key   list a user's keys (never the plaintext)
DELETE /v1/users/{user_id}/keys/{key_id} master key  revoke a key; the same id twice is 404 the second time
POST   /v1/groups                       master       create a group
GET    /v1/groups                       master       list groups
GET    /v1/groups/{group_id}            master
PUT    /v1/groups/{group_id}/members/{user_id}    master   add a member
DELETE /v1/groups/{group_id}/members/{user_id}    master   remove a member
POST   /v1/projects                     master/user  create a project, owner defaults to the caller
GET    /v1/projects                     master/user  list projects the caller can reach
GET    /v1/projects/{project_slug}      master/user
PATCH  /v1/projects/{project_slug}      master/user  rename and/or repair git_locator (clear with null, leave with omit); unknown fields are 422
PATCH  /v1/projects/{project_slug}/owner master/user transfer ownership
POST   /v1/memory/retain                user key     async, returns an operation
POST   /v1/memory/sync_retain           user key     blocks until extraction completes
POST   /v1/memory/recall                user key
POST   /v1/memory/reflect               user key     an LLM answer grounded in memory, not a search
POST   /v1/memory/list                  user key     list memories in the resolved bank
POST   /v1/memory/get                   user key     get one memory by memory_id
POST   /v1/memory/forget                user key     soft-invalidate a memory; reversible
POST   /v1/memory/restore               user key     revert a forgotten memory to valid
POST   /v1/memory/correct               user key     rewrite a memory's text in place
POST   /v1/memory/documents/list        user key     list documents in the resolved bank
POST   /v1/memory/documents/get         user key     get one document by document_id
POST   /v1/memory/documents/delete      user key     irreversible; see below
POST   /v1/memory/operations/list       user key     list async retain operations
POST   /v1/memory/operations/get        user key     get one operation by operation_id
POST   /v1/memory/operations/cancel     user key     cancel a pending operation
POST   /v1/directives                   user key     REST-only, never MCP (§14.1); create a standing rule
GET    /v1/directives                   user key     list directives on the resolved bank
GET    /v1/directives/{directive_id}    user key
PATCH  /v1/directives/{directive_id}    user key
DELETE /v1/directives/{directive_id}    user key
POST   /v1/mental-models                user key     REST-only, never MCP (§14.2); create from a source query
GET    /v1/mental-models                user key     list mental models on the resolved bank
GET    /v1/mental-models/{mental_model_id}       user key
PATCH  /v1/mental-models/{mental_model_id}       user key
DELETE /v1/mental-models/{mental_model_id}       user key
POST   /v1/mental-models/{mental_model_id}/refresh  user key   force an immediate refresh; a full reflect upstream
POST   /v1/mental-models/{mental_model_id}/clear    user key
POST   /mcp/                            user key     MCP endpoint (streamable HTTP); see "MCP" below
GET    /v1/admin/audit                  master key   read the audit trail, newest first
POST   /v1/admin/memory/{scope}/clear   master key   whole-bank (or one type) clear; irreversible
DELETE /v1/admin/memory/{scope}         master key   delete a whole bank; irreversible; §12.3 erasure path
POST   /v1/admin/slugs/{slug}/release   master key   free a retired slug for reuse; the project it forwarded from is untouched
```

A user key only ever reaches its own memory. The master key must name its
target explicitly (`user_id` for `scope=user`), and every master-key access to
someone else's bank is written to the audit trail; an optional `On-Behalf-Of`
header lets it record the subject it is acting for. The Hindsight `bank_id`
never appears in any response or error. **The master key is refused outright
over `/mcp/`** (`FORBIDDEN`): invariant 22 says it never resides in an
ordinary agent runtime, and MCP has no header equivalent of `On-Behalf-Of` to
audit a delegated call with, so there is no safe way to accept it there.

### Audit trail

`GET /v1/admin/audit` (master key only) reads it back: ordered by
`created_at`, then `id`, both descending, filterable by
`action`/`actor_key_id`/`on_behalf_of`/`since`, always scoped to the caller's
tenant, and page-bounded (`limit`, `le=500`). Several events from one request
share a timestamp, so `created_at` alone is not a total order — the same
query could return the rows in a different order on every call. The `id`
tiebreak only buys **determinism**, not recency: `AuditEvent.id` is
`ids.new_audit_id()`, a random uuid4 hex uncorrelated with insertion order, so
it does not recover which of two same-timestamp events actually happened
first, it just makes repeated calls agree with each other. Built response
field by field — never a serialized row — so a column added later cannot leak
through it by accident.

**Not every master-key action produces a row.** `GET /v1/users/{id}`,
`GET /v1/users`, `GET /v1/users/{id}/keys`, `GET /v1/groups`,
`GET /v1/groups/{id}`, `GET /v1/projects`, and `GET /v1/projects/{slug}` are
identity-metadata reads with no memory content — the same reasoning that
already exempts a user reading their own memory — so instrumenting them would
mean a routine `GET /v1/projects` on every agent start drowns the log that
actually matters. Every mutation (user/key/group/project create, rename,
transfer, membership, key revocation), every master-key delegated access to
someone else's bank, and every admin destructive operation
(`clear`, `delete`, `slug release`) **is** recorded.

The three destructive operations above are REST-and-master-key-only by
design (SPEC §11.7): they are never advertised over MCP, and a user key that
owns the very bank being cleared or deleted is still refused —
`require_master` gates on the credential alone, before scope or ownership is
ever resolved. `delete_bank` never mutates the `users` row: `User.bank_id` is
`NOT NULL`, so there's no schema-safe way to clear it, and deleting the row
would cascade into that user's keys/memberships/project ownership — well
outside "erase this bank's content." A bank_id whose Hindsight bank was torn
down behaves like one freshly allocated and never materialized (SPEC §17):
the next write against it just re-creates an empty bank under the same id.

### Write rate limiting

Writes are rate-limited per credential (SPEC §20): `MEMORY_WRITE_LIMIT`
(default `60`) calls per `MEMORY_WRITE_WINDOW_SECONDS` (default `60`), an
in-process sliding window shared by REST and its MCP twin for the same key —
switching surfaces does not reset it. Exceeding it returns `429` with
`error.code == "RATE_LIMITED"` and `error.details.retry_after_seconds`.

"Write" means *has a side effect worth metering*, not just *mutates a
memory*: `retain`/`sync_retain`, `forget`, `correct`, `restore`,
`documents/delete` and `operations/cancel` are writes for the obvious reason,
but so are `reflect` (spends LLM tokens on a server-level credential with no
per-user cost attribution, SPEC §19.4) and `recall` (defaults to
`create=True` — an unmetered loop mints one Project row per call, each one
permanently squatting a tenant-unique slug that can never be released,
invariant 8). `list`/`get`-shaped routes that never create a project are the
only calls this limiter never touches. Not Redis-backed: it does not survive
a restart, and N replicas multiply the effective limit by N — a quota
mechanism for one runaway credential, not a distributed rate limiter.

### Forget, restore, correct, delete: what is reversible and what is not

`forget` **invalidates** a memory rather than deleting it — Hindsight's own
description is "soft-retire ... reversible" — and `restore` reverts it to
`valid`. Both are `PATCH` under the hood, driven entirely by the request body
(`state: invalidated` / `state: valid` / `text: "..."` for `correct`); there is
no `DELETE /memories/{id}` because memory is append-only (SPEC §12).

`delete_document`, by contrast, is **irreversible**: it removes the document
and every memory Hindsight ever derived from it. It is available to any
caller authorized for the bank, because a document belongs to the shared bank
namespace, not to whoever retained it.

Whole-bank operations — clearing every memory in a bank, deleting a bank
outright — are **deliberately absent from this surface**. They live behind
the master key on the admin plane (`POST /v1/admin/memory/{scope}/clear`,
`DELETE /v1/admin/memory/{scope}`, see the route table above), never behind a
user key, because their blast radius is an entire bank rather than one memory
or one document.

`scope=project` requests are resolved by `project_slug` (optionally paired
with `git_locator`): the first authenticated user to send an unseen slug owns
the project it lazily creates, and a project owned by someone else is a 403.
Renaming a project leaves a forwarding tombstone, so a `retain`/`recall`
against the old slug still reaches the same bank — the response then carries
`resolved_from` (the slug you asked for), `project_slug` (what it resolved to)
and a `PROJECT_RENAMED` notice, so the client can update its config without a
second `GET /v1/projects/{slug}`.

### Memory Defense: what's actually enforced

Hindsight ships a content-screening pipeline (`memory_defense`) that can
detect secrets, block prompt injection and LLM-screen retained content. v1
enabled it on every bank until Plan 6 turned it off, because **measured live
against the self-hosted MIT `hindsight-api 0.9.1` (2026-08-22), this build
accepts the full-pipeline config and enforces none of it**: a configured
`block` pipeline still let a prompt injection and a raw RSA private key
through with a 200, and the key was retrievable afterward through
`get_document`. The one thing that *did*
block a secret (an AWS key) was **LiteLLM's** `credential-filter` guardrail
at the gateway, not Hindsight — real, but not ours, pattern-based, and blind
to anything already stored, since it only ever sees a `retain` call on its
way out, never a later `get_document`.

Accepted v1 position: prompt-injection screening is absent (memory poisoning
is a known accepted risk, per "Forget, restore..." above and SPEC §20.2), and
credential filtering is whatever the LLM gateway in front of Hindsight
happens to provide — not a property of this service. As of Plan 6 the wrapper
sends **no bank configuration at all**: not `memory_defense` (this build
accepts it and enforces nothing) and not `store_document_text`.

Screening now lives in a LiteLLM `pre_mcp_call` guardrail configured outside
this service. It is an *input-side* control: it screens what enters on
`retain`, and does nothing about text already stored, nor about a REST caller
that never traverses the MCP path.

`store_document_text` therefore sits at Hindsight's default, which is `true`:
a retained document's *raw* text **is** stored and **is** retrievable through
`get_document`, an MCP tool an LLM can call. Deliberate trade — an explicit
`false` disables the original-text storage that `update_mode="append"`
(SPEC §11.4) depends on. SPEC §20.2 carries the measurement.

The raw-document path is not the only one, and closing it never was enough:
`recall` and `list_memories` surface a retained secret verbatim too. Those read the
*extracted memory* units Hindsight's fact extractor produced, not the raw
document, and a short, direct statement (e.g. "The staging passphrase is
X") is routinely extracted as one memory whose text is the original sentence
unchanged — measured live. It does nothing for memory poisoning either,
since a poisoned fact still gets extracted and recalled either way (SPEC
§20.2).

## MCP

Point an MCP-capable agent at `POST http://<host>:8000/mcp/` (streamable
HTTP) with `Authorization: Bearer <user key>` — the same key REST takes,
minted by `POST /v1/users/{user_id}/keys`. There is no separate MCP
credential, and no tool takes a bank id, a tenant id, or the caller's own
user id: `scope` (`"user"` or `"project"`) plus, for `scope=project`,
`project_slug` (optionally `git_locator`) is all a tool needs — everything
else is resolved server-side from the credential. **The master key is
refused outright** (`FORBIDDEN`) rather than accepted and audited: MCP has no
header equivalent of REST's `On-Behalf-Of`, so a delegated master-key call
here could never be attributed to anyone.

The MCP protocol's own `initialize` and `list_tools` calls succeed with no
credential at all — only executing a tool authenticates. Nothing is exposed
by this (the tool list and their schemas carry no tenant data), but it is
worth knowing so a bare `initialize` succeeding is not mistaken for a bug.

**The server only answers the `Host` it is configured for, port included.**
The SDK's DNS-rebinding guard matches `Host` literally, so a client reaching
the service at `localhost:8000` gets `421 Misdirected Request` unless
`localhost:8000` — not just `localhost` — is in `MEMORY_MCP_ALLOWED_HOSTS`.
`docker-compose.yml` sets it to `127.0.0.1,localhost,localhost:8000` for
exactly this reason: the compose file always publishes the api on
`localhost:8000`, so the fix belongs in the deployment config once, not in
every agent's client setup.

| Tool | What it does |
|---|---|
| `retain` | Store something worth remembering; returns immediately with an operation to follow up with `get_operation`. |
| `sync_retain` | Store something and wait until it is searchable. |
| `recall` | Search memory and return the matching facts. |
| `reflect` | Ask memory a question and get a synthesized answer, not a list of facts — costs more than `recall`. |
| `list_memories` | List stored memories, most recent first. |
| `get_memory` | Fetch one memory by id. |
| `forget` | Retire a memory that is wrong or obsolete. |
| `correct` | Replace the text of an existing memory. |
| `restore` | Bring back a memory that `forget` retired. |
| `list_documents` | List the documents memories were derived from. |
| `get_document` | Fetch one document by its id. |
| `delete_document` | Delete a document and every memory derived from it. |
| `get_operation` | Check whether an async `retain` has finished. |
| `list_operations` | List recent async operations. |
| `cancel_operation` | Cancel a pending async operation. |

**`forget` is reversible; `delete_document` is not.** `forget` invalidates a
memory — it leaves the active set but the record survives, and `restore`
brings it back. `delete_document` removes the document *and every memory
Hindsight ever derived from it*, with nothing left to undo it. Same domain
functions back both the REST and MCP surfaces, so the behavior — including
this asymmetry — is identical either way; see "Forget, restore, correct,
delete" above for the full detail.

The excluded set (`clear_memories`, `delete_bank`, bank/tag/mental-model/
directive management, project rename/ownership, key/group administration) is
never advertised: `list_tools` returns exactly these fifteen, pinned by
`tests/test_mcp_tools.py::test_the_advertised_tool_surface_is_exactly_the_spec_set`,
which fails if a sixteenth tool is registered.

Proven against a live server, not just `respx` mocks:
`uv run python scripts/mcp-smoke.py` (see "Run the whole scenario" below).

## Run the whole scenario

Four services: our API and database, Hindsight and its database. The only
external dependency is LiteLLM.

> **Local development only.** `docker-compose.yml` binds every published port
> to `127.0.0.1` and ships development credentials. It is not a deployment
> topology — use `deploy/helm/ach-memory` for that.

> If you followed an earlier version of this README, the master key was a
> published literal (`mem_local_master_change_me`) — rotate it.

```bash
export LITELLM_BASE_URL=https://api.ackstorm.ai
export LITELLM_API_KEY=...
# Generate one. The old literal in this README was a working credential for
# every stack anyone set up by following it.
export MEMORY_MASTER_KEY="mem_local_$(openssl rand -hex 32)"
export MEMORY_MASTER_KEY_HASH=$(python3 -c \
  "import hashlib,os; print(hashlib.sha256(os.environ['MEMORY_MASTER_KEY'].encode()).hexdigest())")

docker compose up -d --build
docker compose run --rm api python -m alembic upgrade head
./scripts/smoke.sh
uv run python scripts/mcp-smoke.py
```

Expect
`PASS: user and project memory, curation, reflect, isolated, no bank_id leak`
and
`PASS: 15 tools, retain -> recall -> forget -> restore, operation followed, no bank_id leak`.

Hindsight's first start runs its migrations and loads a local embedding model,
so allow a few minutes; the healthcheck's retries cover it.

### Three things that will bite you

**`hindsight-db` must be `pgvector/pgvector`, not stock `postgres`.** Hindsight
stores embeddings in a vector column. On a stock image everything starts
healthy and then the first retain fails with
`could not access file "$libdir/vector"`.

**The model matters.** Hindsight extracts facts through function calling and
rejects any response carrying `tool_calls` with empty message content. Verified
working: `bedrock.openai.gpt-oss-20b-1-0` (the default, and fast),
`bedrock.openai.gpt-oss-120b-1-0`, `ackstorm.smart`. Verified broken:
`ackstorm.fast` and `gemini.gemini-flash-latest` fail that way, and every retain
dies after four retries while the service still reports healthy;
`kubeai.gpt-oss-20b` times out. Change `HINDSIGHT_LLM_MODEL` only with a smoke
run to back it.

**The MCP endpoint 421s if `MEMORY_MCP_ALLOWED_HOSTS` doesn't name the exact
`Host` a client sends, port included.** `docker-compose.yml` already covers
the default `localhost:8000` this compose file publishes; only add to
`MEMORY_MCP_ALLOWED_HOSTS` if you map the api to a different host or port.

## State and hard-won knowledge

`docs/PROJECT-STATE.md` — what is done, what is next, and the traps that cost
real time (pgvector, which models actually work, where Hindsight's docs
disagree with its server). Read it before changing anything infrastructural.

## Test

```bash
uv sync --dev
docker compose up -d postgres           # our database only
uv run pytest -m "not integration"      # 497 passed, 2 deselected, no Hindsight needed

docker compose up -d hindsight          # adds Hindsight + its database
uv run pytest -m integration            # live round-trip
```

The test suite runs against its own `memory_test` database on the same
Postgres server (created automatically on first run), never against the
`memory` database `docker compose up -d api` uses — they used to be the same
database, so `uv run pytest` while a dev stack was up would silently wipe
its data mid-session (`Base.metadata.drop_all`) and reset its schema to
`create_all`'s shape. Safe now to run the suite and `./scripts/smoke.sh`
against the same running compose stack. Override `MEMORY_TEST_DATABASE_URL`
to point elsewhere if you need to.

`respx` mocks cover every tool and route in isolation; neither mock nor unit
test can prove the real MCP transport, the real Hindsight response shapes, or
that the shipped container actually starts. `./scripts/smoke.sh` and
`uv run python scripts/mcp-smoke.py` against a live `docker compose up` close
that gap — see "Run the whole scenario" above and
`docs/PROJECT-STATE.md` for what each has caught that the mocks didn't.

## Layout

```
src/memory/
  config.py     settings (MEMORY_ env prefix)
  ids.py        identifier and bank-id generation
  errors.py     domain errors and their stable codes
  db.py         engine and session dependency
  models.py     Tenant, User, ApiKey, Group, GroupMember, Project, RetiredSlug, AuditEvent
  banks.py      scope -> bank resolution and its authorization
  projects.py   project resolution, ownership authorization, rename/transfer, creation race
  slugs.py      slug normalization and Git-locator canonicalization
  audit.py      audit event recording: master-key access to another principal's
                bank, user/key/group provisioning and membership, project
                create/rename/transfer, admin clear/delete/slug-release
  provenance.py §13 metadata split: builds the extraction fields Hindsight
                sees, excluding audit/runtime-only keys (os, arch,
                client_version, client_name) that this service does not
                otherwise persist; reserved-key rejection
  ratelimit.py  per-credential sliding-window write limiter (in-process, SPEC §20)
  auth/         key crypto, principal resolution
  hindsight/    endpoint paths pinned to openapi.json, and the client
  api/          FastAPI app, users, memory, groups, projects
    common.py   shared response mixin (rename-forwarding notice)
    memory.py   retain/sync_retain/recall/reflect, `_resolve_bank`, `_strip_bank_id`
    curation.py list/get/forget/restore/correct — one Hindsight PATCH, three meanings
    documents.py list/get/delete for the documents a bank's memories were extracted from
    operations.py list/get/cancel for async retain operations
    groups.py   group CRUD and membership (master key only)
    projects.py project control plane (create/list/get/rename/transfer)
    admin.py    GET /v1/admin/audit; master-key-only destructive plane
                (clear/delete a bank, release a retired slug)
  mcp/          the fifteen-tool MCP surface, mounted at /mcp in api/app.py
    server.py   build_mcp(), tool_session() — the shared auth/session pipeline
    tools.py    the fifteen tool registrations, all routed through `_run`
```

`hindsight/paths.py` is the first thing to re-check on a Hindsight upgrade: the
paths are pinned against a live `openapi.json`, and the published documentation
disagrees with the server about them.
