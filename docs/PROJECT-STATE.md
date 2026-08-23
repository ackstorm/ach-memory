# ach-memory — project state and hard-won knowledge

Written for whoever picks this up next, including a future session of me with no
memory of building it. `SPEC-v1.md` is the contract; this file is what the code
and the failures taught us that the spec cannot tell you.

## Where things stand

| | |
|---|---|
| Branch | `main`, no worktrees, no open branches |
| Tests | 497 unit/API passed, 2 deselected (integration; need live Hindsight), 2 e2e smokes (REST `scripts/smoke.sh`, MCP `scripts/mcp-smoke.py`) plus `scripts/e2e.py`'s scenario suite |
| Plan 1 | **complete**, reviewed, verified end to end |
| Plan 2 | **complete**, reviewed, verified end to end (groups, projects, ownership, rename, `scope=project`) |
| Plan 3 | **complete**, reviewed, verified end to end against a live Hindsight (reflect, curation, documents, operations, §13 provenance, master-key audit trail); final whole-branch review closed 10 findings across provenance case-handling, curation IDOR coverage, smoke bank-id coverage, a document-id control-character 500, an unaddressable `..` document, dead provenance scaffolding, `get_operation`'s real not-found shape, three caller-mistake 502s, two more bounded-column 500s, and a shared test/dev database — see `.superpowers/sdd/plan3-final-report.md` |
| Plan 4 (fifteen-tool MCP surface §11, per-credential write rate limiting §20) | **complete**, verified end to end against a live Hindsight over the real MCP client SDK (`scripts/mcp-smoke.py`), not just `respx` mocks — the live run found `mcp` was in the wrong dependency group and the shipped container could not start (see below) |
| Plan 5 (admin governance and packaging: master-key destructive plane, `GET /v1/admin/audit`, REST-only directives/mental-models §14, Helm chart, CI image gate) | **complete** |
| Plan 6 (review remediation: 2 critical + 9 important findings from the 2026-08-22 whole-codebase review) | **complete** — dropped the bank config PATCH and its TTL cache (Memory Defense unused, `store_document_text` left at Hindsight's default); proved `update_mode="append"` end to end; `get_operation` derives `failed` when every child operation errored instead of polling `pending` forever; added key lifecycle (`GET /v1/users`, `GET /v1/users/{id}/keys`, `DELETE /v1/users/{id}/keys/{key_id}`); made the `git_locator` PATCH repair path real and rejected unknown PATCH fields; moved the two admin-erasure audit commits to after the upstream call; mapped upstream 422s and boundary-invalid `correct`/mental-model fields away from `HINDSIGHT_ERROR`; pinned Hindsight's `{tenant}` URL segment to `default`; locked the rate limiter and savepointed `ensure_tenant` — see `docs/superpowers/plans/2026-08-22-06-review-remediation.md` |
| Admin API (whole-bank clear/delete, `GET /v1/admin/audit`, key lifecycle) | **complete** — `src/memory/api/admin.py` (207 lines) plus `src/memory/api/users.py`'s key routes, covered by 27 tests in `tests/test_admin_api.py` and 30 in `tests/test_users_api.py`; the most security-sensitive surface in the service (whole-bank erasure, key revocation), master-key-gated throughout |

Run everything: `uv run pytest -m "not integration"`, then `./scripts/smoke.sh`
and `uv run python scripts/mcp-smoke.py` against a live `docker compose up -d`.

The task-by-task ledger is `.superpowers/sdd/progress.md` (git-ignored scratch).
It records every task, its commit range, and every finding. **After a context
loss, trust that file over recollection.** `git clean -fdx` would destroy it.

History before 2026-08-23 is NOT in `git log`: 174 commits were squashed into a
single `first commit` when this repository moved to
`github.com/ackstorm/ach-memory` (private). Their subject lines are kept in
`docs/HISTORY.md`, and the reasoning behind them is in the code comments --
this codebase puts the "why" next to the code on purpose, which is what made
the squash survivable. `git log` is authoritative again from that commit
onward.

## What works today

`scope=user` and `scope=project` end to end. A master key provisions users and
mints user keys; a user key retains and recalls against its own Hindsight bank
(`scope=user`) or against a project's bank (`scope=project`); both materialize
lazily and `bank_id` never leaves the service.

Projects are created lazily — the first authenticated user to touch an unseen
slug owns it — or explicitly via `POST /v1/projects`, with ownership `user` or
`group`. Any caller authorized for a project (owner, or a member of the owning
group) may rename it or transfer ownership; a rename leaves a forwarding
tombstone, so a stale `MEMORY_PROJECT` or a freshly re-derived Git slug still
resolves, and the response carries `resolved_from` plus a `PROJECT_RENAMED`
notice. `git_locator` is canonicalized before comparison and storage, so the
same repository spelled two ways (scp-style, `https://`, a trailing `.git`)
does not look like two repositories.

Groups are master-key-only: create, list, get, add member, remove member.
That gate is a security property and not incidental — group membership *is*
project authorization, so a user key that could add itself to a group could
read and write every project that group owns.

Plan 3 completed the memory data plane behind the same authenticate → resolve
→ authorize → bank pipeline: `reflect` (an LLM answer grounded in memory,
distinct from `recall`'s retrieval), curation (`list`/`get`/`forget`/`restore`/
`correct` — one Hindsight `PATCH` whose body decides which of the three it is),
documents (`list`/`get`/`delete`, the last irreversible and shared-namespace),
and async operations (`list`/`get`/`cancel` — cancel is `DELETE
.../operations/{id}`, not the `/delete` suffix, which removes a *terminal*
operation and is out of scope). `memory_id`/`document_id`/`operation_id` are
never looked up globally: every route resolves and authorizes the bank first,
exactly as retain/recall already did, and only then reads the secondary id
inside it (SPEC §20.1). Provenance metadata is filtered per §13.2 —
`provenance.build()` returns only the extraction fields Hindsight sees;
audit/runtime-only keys (`os`, `arch`, `client_version`, `client_name`) are
excluded, matched case-insensitively so a variant spelling can't slip
through, and this service does not otherwise persist or log them — and a
reserved key (`tenant_id`, `user_id`, `project_slug`, `memory_key`,
`on_behalf_of`, `agent`, `client_name`) in caller-supplied metadata is
`INVALID_METADATA` with nothing written, not a silent override. Every
master-key access to a bank that is not its own is now audited under a
distinct action name per route, and an optional `On-Behalf-Of` header lets a
master key record who it is acting for — provenance, never authorization
evidence, since it is never verified.

This was proven end to end against a live Hindsight (`./scripts/smoke.sh`
against `docker compose up -d --build`), not just `respx` mocks — see the
next section for what that run found that the mocks didn't.

Plan 4 added the fifteen-tool MCP surface (SPEC §11) and per-credential write
rate limiting (§20). Every tool is a thin wrapper that builds a `ScopedRequest`
(or `RetainRequest`) from its arguments and calls the exact same
`_resolve_bank`/`_strip_bank_id`/`HindsightClient` functions the REST routes
call — there is no second authorization or bank-resolution path to drift out
of sync. `retain`/`sync_retain`/`recall`/`list_memories`/`forget`/`restore`/
`get_operation` (the chain a real agent actually uses) were proven against a
live Hindsight over the real MCP client SDK, not respx (`scripts/mcp-smoke.py`
against `docker compose up -d --build`) — see below for what that run found.
The excluded set (bank/tag/mental-model/directive/admin management) is
enforced by never registering the tool, pinned by a test shown to fail when a
sixteenth tool is added. The rate limiter is a single per-credential
`Limiter` shared by both surfaces (`is_write` on `_resolve_bank`), so neither
can be used to dodge the other's count.

## Traps that cost real time

Each of these looked healthy right up until it didn't.

**Review this codebase by mutation, not by reading. Reading is how the gaps
got in.** Plan 6's reviews changed method partway through and it changed the
results. A read-only review of the credential surface came back clean; the
same reviewer then deleted each security-relevant line from
`src/memory/api/users.py` and ran the suite, and found **eleven** properties
that 477 passing tests left completely unpinned. The worst predated the task
and was one line: swap `require_master` for `current_principal` in
`create_key` and any user key mints an API key **as any other user**, suite
green. Three more examples from the same plan, all invisible to reading:
`canonical_locator()` could be replaced with a raw assignment because every
test value was already canonical — so the `git_locator` repair path was never
proven to repair; `tests/test_errors.py` claimed in its own docstring to check
SPEC §18 both ways and did not, surviving "delete a code from the list, leave
it in the prose"; and the rate-limiter concurrency test as first written could
not fail at all, because CPython's GIL makes `deque` operations atomic, so the
40 threads serialised and it passed identically with and without the lock.
When it matters, delete the line, watch the suite go red, restore by copying a
file back — never `git checkout -- <file>`, which has destroyed uncommitted
work here four times. Run the killer against only its intended test in its own
process: a kill inside a 500-test suite does not tell you which assertion
tripped. And watch the clock — a mutant that dies of `NameError` during
collection in four seconds scores as "covered" while proving nothing.

**When a fix keeps missing siblings, re-derive the question instead of
checking the patch.** Task 1 needed five review rounds for one docs change:
someone grepped `src/` and `tests/` and missed `docs/`; the next pass grepped
`docs/` and missed the repo root; the next read a file from line 168 and
missed line 166. Each correction inherited the previous one's scope. What
broke the chain was asking the reviewer to reconstruct the question from
scratch rather than verify the edits — it switched from grepping the *terms*
(`memory_defense`, `store_document_text`) to grepping the *mechanism*
(`ensure_bank`) and immediately found two false claims that contained none of
the search words.

**`mcp` was a dev dependency; the production container could not start.**
348 unit/API tests were green — `uv run pytest -m "not integration"` installs
the `dev` dependency group, which is where `mcp>=2.0.0` had lived since Task 1
of Plan 4 — while `src/memory/api/app.py` (`TransportSecuritySettings`) and
`src/memory/mcp/{server,tools}.py` (`MCPServer`, `Context`, `ToolAnnotations`)
import from it in production code, not test code. `Dockerfile` builds with
`uv export --frozen --no-dev`, so the shipped image never installed `mcp` (or
its transitive `mcp_types`/`httpx2`) at all — a rebuilt `docker compose up -d
--build api` crash-looped with `ModuleNotFoundError: No module named 'mcp'`,
visible only in `docker compose logs api`, never in a test run. Eight tasks of
code review and 100% green tests never caught it because nothing had rebuilt
the container from a clean image since the MCP work landed; every dev-loop
`uv run pytest` silently carries the full dev group regardless of which group
a package is declared under. Fixed by moving `mcp` from `[dependency-groups]
dev` to `[project.dependencies]` in `pyproject.toml` (and re-running `uv lock`
— no version changes, purely a group move). **The lesson generalizes: a green
test suite proves nothing about which dependency group a runtime import needs
verified — the only check that catches a group misclassification is
rebuilding the actual image and starting the actual container**, which is
exactly what `scripts/mcp-smoke.py`'s own `docker compose up -d --build`
prerequisite forced.

**The MCP endpoint's DNS-rebinding guard matches `Host` including the port.**
The SDK defaults `MEMORY_MCP_ALLOWED_HOSTS`-equivalent allowlist to
`127.0.0.1,localhost` (no port). This compose file always publishes the api at
`localhost:8000`, so a client's `Host: localhost:8000` does not match a bare
`localhost` entry and every MCP call gets `421 Misdirected Request` — a REST
call to the same container is unaffected, since REST has no such guard, which
is what makes this easy to miss if only `scripts/smoke.sh` is run.
`docker-compose.yml` now sets `MEMORY_MCP_ALLOWED_HOSTS` to
`127.0.0.1,localhost,localhost:8000` for the `api` service. Measured directly:
overriding it back to `127.0.0.1,localhost` and re-running
`scripts/mcp-smoke.py` reproduces the failure (the SDK surfaces it as an
`mcp.shared.exceptions.MCPError` from the JSON-RPC layer during
`session.initialize()`, not a bare `httpx.HTTPStatusError`, since the client
library wraps the transport failure); restoring the compose default fixes it.

**Everything downstream of a running container matched the mocks.** Once the
container actually started, `sync_retain -> recall -> list_memories -> forget
-> restore` and `retain -> get_operation` behaved exactly as
`tests/test_mcp_tools.py`'s `respx` fixtures predicted, on the first run, no
code changes needed on the tool-behavior side. The bank-id-in-`chunk_id`
substring leak the previous plan found and fixed in `_strip_bank_id` covers
the MCP surface for free — it is the same function every tool's `_run` calls
— and `scripts/mcp-smoke.py`'s leak scan (mirroring `scripts/smoke.sh`'s)
found nothing. Worth recording precisely because it means this plan's
real defect was in deployment wiring, not in the tool logic itself — the
`respx`-mocked unit tests earned their trust on the domain-logic side; they
just cannot, structurally, ever catch a dependency-group or container-startup
defect, since they never touch a built image.

**`hindsight-db` must be `pgvector/pgvector:pg17`.** Hindsight stores embeddings
in a vector column. On stock `postgres:17-alpine` every service reports healthy
and the **first retain** fails with `could not access file "$libdir/vector"`.

**The model matters, and the failure is invisible until first retain.** Hindsight
extracts facts through function calling and rejects any response carrying
`tool_calls` with empty message content. Measured by real smoke runs:

| Model | Result |
|---|---|
| `bedrock.openai.gpt-oss-20b-1-0` | works, whole smoke in ~2s — **the default** |
| `bedrock.openai.gpt-oss-120b-1-0` | works |
| `ackstorm.smart` | works, slower |
| `ackstorm.fast` | **fails** — tool_calls, empty content, 4 retries then error |
| `gemini.gemini-flash-latest` | **fails**, same way |
| `kubeai.gpt-oss-20b` | **times out** |

Never change `HINDSIGHT_LLM_MODEL` without a `./scripts/smoke.sh` run.

**Hindsight's LLM wiring must be explicit.** With only an API key set, Hindsight
falls back to `provider=openai/gpt-4o-mini` with no base URL, and the OpenAI SDK
silently picks up whatever `OPENAI_BASE_URL` is in the ambient environment. On
this host that is `localhost:8787`, which is how a green test on one machine
becomes a red one everywhere else. Compose sets
`HINDSIGHT_API_LLM_{PROVIDER,BASE_URL,API_KEY,MODEL}` for exactly this reason.

**Hindsight's published docs contradict the server.** Retain is
`POST /v1/{tenant}/banks/{bank}/memories`, **not** `/memory/retain` — that path
does not exist. The bank-config PATCH body must be wrapped as
`{"updates": {...}}` or it 422s. Both found by reading `openapi.json` off a
running container. `src/memory/hindsight/paths.py` is pinned to that and is the
first thing to re-check on a Hindsight upgrade. Three more from the same
`openapi.json` reading, pinned during Plan 3 and now measured against a live
server too:

1. **Listing memories is `GET .../memories/list`**, not `GET .../memories`.
   `DELETE .../memories` is `clear_memories`, admin-only and out of scope.
2. **`DELETE .../operations/{id}` cancels.** `DELETE
   .../operations/{id}/delete` — a real, different path — deletes a terminal
   operation. This service exposes cancel and has no `delete_operation`.
3. **Curation is one `PATCH` with three meanings**, driven by the body:
   `{"state": "invalidated"}` is `forget`, `{"state": "valid"}` is `restore`,
   `{"text": "..."}` is `correct`. There is no `DELETE /memories/{id}` —
   memory is append-only.
4. **`GET .../operations/{id}` on an absent operation is a 200**, not a 404
   — the body is `{"operation_id": ..., "status": "not_found"}`. The `not_found=`
   404-mapping `HindsightClient._request` uses everywhere else never fires for
   this route; `get_operation` checks `result["status"]` itself and raises
   `OperationNotFound` from there. `cancel_operation` (`DELETE`) is
   unaffected — that one genuinely 404s. The fifth time this plan found a
   mocked assumption about Hindsight's behavior that the real server
   contradicts; see the `list_memories`/`bank_id` entry below for the fourth.
5. **`update_mode="append"` requires a `document_id`.** SPEC §11.4 blesses
   `append` for interactive coding sessions, but a caller who follows the
   spec and forgets `document_id` gets Hindsight's own 400
   (`"update_mode='append' requires a document_id"`). Bounded at the
   boundary now: `RetainRequest.update_mode` is `Literal["replace",
   "append"]` and a `model_validator` rejects `append` without a
   `document_id` as a 422 before the request ever reaches Hindsight, instead
   of relaying a fixed 502 that gave a spec-following caller no way to learn
   why. `ListMemoriesRequest.state` is bound the same way, to Hindsight's own
   `{"valid", "invalidated"}` enum.

**A live `memories/list` response leaked `bank_id` through a field no mock
ever included.** Every respx fixture for `list`/`get`/curation used a
key-based leak check (`{"bank_id": "..."}`, possibly nested) because that is
the only shape anyone had reason to write by hand. The real server's
`chunk_id` is literally `f"{bank_id}_{document_id}_{n}"` — the bank_id as a
*substring* of a field with an unrelated name — and `_strip_bank_id`'s
key-only filter (`if k != "bank_id"`) let it straight through. Found only by
reading an actual `./scripts/smoke.sh` response body, not by strengthening a
mock: the smoke script's own "no bank_id in any curation response" grep
caught it, since it checks the literal bank_id pattern in the response text
rather than trusting a key name. Fixed in `_strip_bank_id`
(`src/memory/api/memory.py`) by threading the resolved `bank_id` through
every call site and redacting it as a substring of any string value, not just
dropping a key named `bank_id`; a live-shaped regression test
(`test_bank_id_embedded_in_chunk_id_is_redacted` in `test_curation_api.py`)
pins it. Also worth knowing when writing a new mock: the real `memories/list`
response wraps results under `"items"`, not `"memories"` — existing fixtures
use `"memories"` because nobody had a live response to copy from; the routes
themselves never parse the key (pure passthrough after stripping), so this
did not hide a bug, but a new mock should use the real shape.

**Hindsight runs its task worker in-process** (`HINDSIGHT_API_WORKER_ENABLED`
defaults true), which matters because our `retain` is async by default: with no
worker, retained memories would never be extracted. One container is enough.

The server entry point is `hindsight-api`, **not** `hindsight-local-mcp` — the
latter embeds its own Postgres and ignores `HINDSIGHT_API_DATABASE_URL`.

**A first `alembic revision --autogenerate` against the test database comes
back empty.** The test suite's `Base.metadata.create_all` has already built
every table there, so autogenerate diffs the live schema against itself and
finds nothing to write — a false "nothing changed" that looks like success.
Generate against a database that migrations actually built instead: drop the
schema (or point at a throwaway one `create_all` has never touched), replay
every existing revision with `alembic upgrade head`, *then* run `alembic
revision --autogenerate`.

**`uv run pytest` used to destroy a running dev stack.** `tests/conftest.py`'s
`Base.metadata.drop_all` ran against `localhost:5433/memory` — the exact same
database `docker compose up -d api` connects to (same Postgres container,
same `POSTGRES_DB=memory`). Running the suite against a stack that was also
being poked by hand (`./scripts/smoke.sh`, manual curl) deleted its data
mid-session and reset its schema to `create_all`'s shape while leaving
`alembic_version` at head — so a later `alembic upgrade head` looked like a
no-op and fixed nothing. `MEMORY_TEST_DATABASE_URL` now defaults to
`memory_test`, a separate database on the same server, auto-created on first
use (`tests/conftest.py`'s `_ensure_database_exists`). The two can no longer
collide; override the env var if you need to point elsewhere.

**Isolating the test database exposed that `scripts/smoke.sh` was never
idempotent.** It minted a fresh user every run but retained into a *fixed*
project slug, and a project belongs to whoever first touches its slug — so
run 2 was a different user asking for run 1's project, which is correctly a
`403 PROJECT_ACCESS_DENIED`. It had always looked green because `pytest` kept
dropping the tables out from under it. The slug is now per-run
(`smoke-project-$(date +%s)-$$`), and two consecutive runs pass. Worth
remembering as a shape, not just a bug: a shared mutable database was hiding a
real defect in the check that is supposed to catch real defects.

**The MCP SDK, measured against `mcp==2.0.0` rather than its docs.** Every one
of these differs from what the published examples and older tutorials show, and
each would cost a cycle to rediscover:

- The server class is `mcp.server.mcpserver.MCPServer`. `FastMCP` is the old
  name and does not exist here.
- **A tool may be a plain `def`.** It runs in an AnyIO worker thread (verified:
  `threading.current_thread().name` is `AnyIO worker thread`), so this project's
  synchronous stack — sync SQLAlchemy, sync httpx — works inside a tool with no
  `to_thread` dance and without blocking the event loop.
- A tool reads the HTTP request headers through `Context.headers`, a
  `Mapping[str, str] | None`. Its own docstring is worth heeding: "Headers are
  client-supplied input - never treat one as an identity assertion."
- Returning a pydantic `BaseModel` produces structured output; returning a
  `dict` does not (`structured_content` comes back `None`).
- Mounting under FastAPI needs the host app's lifespan to enter
  `mcp.session_manager.run()`. Starlette does not run nested lifespans under a
  `Mount`, so forgetting this yields a server that accepts connections and then
  hangs.
- Client-side, `streamable_http_client(url, http_client=...)` yields a
  **2-tuple** `(read, write)`, takes no `headers=` argument (put them on the
  `httpx2.AsyncClient`), and results are snake_case: `structured_content`,
  `is_error`, `Tool.input_schema`.
- **Writing an actual client (`scripts/mcp-smoke.py`) confirmed and extended
  the above.** `httpx2` is a real, separately-installed package (`mcp`'s own
  transport dependency, version 2.12.0 here) — not a typo for `httpx` and not
  the same object (`httpx2.AsyncClient is not httpx.AsyncClient`); our own
  code keeps using plain `httpx` throughout, and only the MCP client script
  touches `httpx2`, because that is what `mcp.client.streamable_http` imports.
  `ClientSession` is `mcp.client.session.ClientSession`, not re-exported from
  the `mcp` package root (`mcp/__init__.py` only re-exports `mcp_types`
  models). `TransportStreams` (what `streamable_http_client` yields) really is
  a plain `tuple[ReadStream, WriteStream]` type alias, so the "2-tuple" claim
  above is not approximate. `CallToolResult.structured_content` is exactly our
  `ToolResult`'s `model_dump(mode="json", by_alias=True)` — top-level keys
  `result`/`project_slug`/`resolved_from`/`notice`, so a client reads the
  actual tool payload at `structured_content["result"]`, not at the top level.
  A tool's `MCPToolError` surfaces as `CallToolResult(is_error=True,
  content=[TextContent(text="CODE: message {...}")])`, matching
  `mcp/server/mcpserver/server.py`'s `_handle_call_tool` — a client must check
  `is_error` itself; the SDK does not raise for a tool-level failure the way
  it does for a protocol-level one.

## Design decisions that are load-bearing

**History — how the `memory_defense` / `store_document_text` question got
asked at all.** Measured live against the self-hosted MIT `hindsight-api
0.9.1` on 2026-08-22 (resolves SPEC §25 item 13): a bank was configured with
the full Memory Defense pipeline (`detect_secrets`, `prompt_injection`,
`llm_screen` all `block`), the config was accepted and echoed back verbatim,
and then a prompt injection and an RSA private key both went through with a
200 — the raw key was retrievable afterward through `GET /documents/{id}`, an
MCP tool (`get_document`). Only an AWS access key was blocked, and that was
**LiteLLM's** `credential-filter` guardrail (`guardrail_mode: pre_call`)
surfacing as `500 "Fact extraction failed"`, not Hindsight — real, but not
ours, pattern-based rather than complete, and blind to stored document text
(it only ever sees the `retain` call's content, never what `get_document`
returns later). This measurement is why v1 originally treated
`store_document_text` as the actual control and kept sending
`memory_defense: {enabled: true}` anyway (cheap, and correct if the tier ever
changed): with it set `false` on a scratch bank, `get_document` returned
`original_text: null` while `recall`, `list_memories` and the extracted
memory count were unchanged — no measured recall cost. That was the v1
decision: the wrapper set `store_document_text: false` at bank
materialization to close the raw-text retrieval path above.

**Current reality, as of Plan 6 (commit `73fb73c`) — that decision is
reversed.** `ensure_bank` is now a bare `PUT` upsert; the config PATCH that
carried both fields is deleted along with its TTL cache, and the wrapper sets
**no bank configuration at all** — neither `memory_defense` nor
`store_document_text` is sent anywhere. `store_document_text` therefore sits
at Hindsight's default, which is `true`, not `false`: retained document text
**is stored** and **is retrievable** via `get_document`, exactly the path the
2026-08-22 measurement above shows open. This is a deliberate trade, not a
regression — leaving `store_document_text` at its default is what makes
`update_mode="append"` work (SPEC §11.4); an explicit `false` disables the
original-text storage `append` depends on. This is no longer a claim taken on
faith: `tests/test_append_integration.py` retains `replace` then `append` into
the same `document_id` against a live Hindsight and reads both lines back
through `get_document`, and `scripts/e2e.py`'s
`retain.append_accumulates_document_text` scenario does the same over the
full stack — before Plan 6 this path could not succeed at all (Task 2; the
only prior coverage mocked a 200 and asserted the call was sent, never that
Hindsight accepted it). Content screening did not go away,
it moved: it is now a LiteLLM `pre_mcp_call` guardrail configured outside
this repo, running ahead of the MCP call that would otherwise reach `retain`.
That guardrail is an **input-side control only** — it screens what enters on
`retain`, before the call reaches this service. It does nothing for text
already stored, and nothing for a REST caller that never traverses the MCP
path at all. See SPEC §19.5, §20 (MUST list) and §20.2 for the full current
statement, including the `sensitive_data`/`private_key_pem` regex gap that
also does not change the conclusion above.

**Hindsight auto-materializes a bank on `recall`/`reflect`.** Measured live:
calling `recall` (or `reflect`) against a `bank_id` that was never
`ensure_bank`'d creates the bank on the fly (a 200, not a 404). Since Plan 6
(commit `73fb73c`) this observation is moot rather than notable: `ensure_bank`
itself sets no config either, so an auto-materialized bank and an
explicitly-`ensure_bank`'d one are configured identically (i.e. not at all).
Harmless in practice regardless: nothing ever enters a bank except through
`retain`, so a `recall`/`reflect` against a bank with no prior `retain` finds
nothing in it either way. (Until Plan 6 `_retain` called `ensure_bank` first.
It no longer does: Hindsight auto-creates a bank on first `retain` too —
measured live — so the round trip bought nothing on the hot path.
`create_directive` is the only caller left, because a directive POST on a
never-retained bank 500s upstream.)

**`bank_id` never crosses the API boundary.** Harder than it sounds, because the
Hindsight URL *contains* the bank id. Three separate leaks were found and closed:
`raise ... from exc` chained an httpx error whose `.request.url` carried it;
`raise ... from None` still left it on `__context__`, so the raise now happens
*outside* the `except` block; and httpx logs the full URL at INFO, so the httpx
logger is pinned to WARNING at app construction. `_strip_bank_id` is recursive
because Hindsight nests `bank_id` in recall results.

**`get_session` never commits.** FastAPI awaits the response *inside* the
dependency exit stack, so a teardown commit runs after the client already has its
bytes — a failed commit could hand a caller a plaintext API key that
authenticates nowhere. Write handlers commit explicitly before returning.

**`api_keys.user_id` is NOT NULL and `is_master` is a literal `False` on the
DB branch.** Deriving `is_master` from a nullable column meant one bad row could
mint tenant-wide authority.

**Conflict rollbacks use `db.begin_nested()`, not `db.rollback()`.** A blanket
rollback discards the whole request transaction; the project-creation race has
the same catch-and-recover shape but with earlier writes that must survive.

**Every request gets its own Session in tests.** They used to share one, so no
API test exercised commit or rollback and a handler that forgot to commit looked
correct. All request sessions join one outer transaction via
`join_transaction_mode="create_savepoint"`, which is also required for any test
that triggers an `IntegrityError` — without it the fixture's rollback silently
becomes a no-op.

**Derived project slugs carry an 8-hex digest of the canonical locator.**
`normalize_slug` collapses `/`, `.` and `-` to one separator, so without it
`github.com/acme/payments-api` and `github.com/acme-payments/api` produced the
same slug — two unrelated repositories sharing one memory bank. The slug is
**not** the bank id (that is an opaque UUID); it is a URL path segment and
something a human types into `MEMORY_PROJECT`.

**The write rate limiter (`memory.ratelimit`) is in-process only — not a
distributed quota.** It is a per-credential sliding window kept in a plain
dict inside one running process (`MEMORY_WRITE_LIMIT`/
`MEMORY_WRITE_WINDOW_SECONDS`, default 60 writes / 60s). It does **not**
survive a process restart and does **not** coordinate across replicas: with N
replicas behind a load balancer, the effective limit for one credential is up
to N times the configured number, since each replica enforces its own
independent window. This is a deliberate v1 scope decision (SPEC §20's MUST
is "rate-limit memory writes per credential", not "enforce a global quota") —
it bounds the runaway single-key retain/reflect loop that actually threatens
billing (§19.4) without adding Redis or any shared state. Move to a
Redis-backed (or DB-backed) limiter before running more than one replica if
this needs to become a real ceiling rather than a per-replica one. Checked
once, centrally, inside `memory.api.memory._resolve_bank` (an `is_write` flag
set by each REST route and by the shared MCP `_run` pipeline), so both
surfaces share one Limiter instance per credential and neither can be used to
dodge the other's count.

**Packaging (`deploy/helm/ach-memory`) and CI (`.github/workflows/ci.yml`)
ship the service, not a database.** The chart is Deployment + Service +
optional Ingress + a `pre-install,pre-upgrade` hook Job running `python -m
alembic upgrade head` — Postgres and Hindsight stay external, so `helm
install` can never seed a cluster with test data the way an in-chart database
would. `MEMORY_MASTER_KEY_HASH` only ever comes from a `Secret`
(`masterKeySecret.name` for an existing one, `.value` to have the chart
create one); rendering `fail`s if neither is set — verified with `helm
template` against no master-key config at all. `MEMORY_MCP_ALLOWED_HOSTS`
defaults from `ingress.host` for the same reason the compose file hardcodes
`localhost:8000`: the SDK's DNS-rebinding guard matches `Host` including
port, and getting this wrong makes every MCP call 421 while REST keeps
working — see `deploy/helm/README.md`. The rate limiter's per-replica
multiplication (previous bullet) is called out again right next to
`replicaCount` in `values.yaml`, not only in prose. No dedicated health route
exists yet, so both probes and the CI image job hit `/docs` (unauthenticated,
no DB dependency) — the same signal `scripts/smoke.sh` already polls.

**CI's `image` job — not the unit tests — is the load-bearing check.** Plan
4 shipped seven green tasks with `mcp` in the dev dependency group while
production code imported it; the Dockerfile's `uv export --no-dev` meant
every one of those images crash-looped with `ModuleNotFoundError`, and
nothing automated caught it because `uv run pytest` always installs the dev
group regardless of which group a package is declared in. The `image` job
builds the actual production image, runs
`python -c "import memory.api.app, memory.mcp.tools"` inside it (fails fast,
pinpoints exactly this class of bug), then starts the container against a
real Postgres service with a dummy master-key hash and polls `/docs` with a
bounded, explicit-failure loop. Verified locally, twice: passes against the
current tree, then fails with the exact `ModuleNotFoundError: No module
named 'mcp'` after moving `mcp` back into `dependency-groups.dev`, **re-running
`uv lock`**, and rebuilding — restored afterward. That `uv lock` step is not
optional: `uv export --frozen` reads `uv.lock`'s own recorded group
membership, not `pyproject.toml` directly, so editing `pyproject.toml` alone
and rebuilding ships an image that still contains the moved package — the
mutation only actually reproduces the failure once the lock file is
regenerated to match. The `image` job's later `alembic upgrade head` step
(added to close a gap the import guard could not see — `alembic` and
`psycopg` are never imported by `memory.api.app`/`memory.mcp.tools`) was
verified the same way: moving either to `dependency-groups.dev` with no `uv
lock` leaves every check in this job green, including the old `/docs` probe;
only after `uv lock` does the migration step fail with
`ModuleNotFoundError`. Adding this gate surfaced 65 pre-existing
`ruff` violations the project had never been checked against (45 were `B008`,
FastAPI's own idiomatic `Depends(...)`-as-default pattern misread as a
mutable-default bug — now an explicit, commented `ignore` in
`[tool.ruff.lint]` — the rest were mechanical `--fix`-able typing/import-order
cleanup plus two `ClassVar` annotations); fixed in the same change so the new
`lint` job is green from its first run rather than red on `main` by
construction.

## Process notes

The subagent loop (implementer → independent reviewer → fix → re-review) found
**12 real defects**, and all but two were errors in the plan rather than in the
transcription. Two worth remembering:

- A blanket find-and-replace in the plan document rewrote our own public route
  `/v1/memory/retain` into `/v1/memories` while fixing the *upstream* Hindsight
  path. Targeted edits only.
- Plan-authored tests are not exempt from review. Several would have passed
  against a broken implementation; the reviewers caught them.

Ask reviewers for mutation evidence on any test asserting a security property:
break the implementation, prove the test fails, restore. Several "passing" tests
turned out to be tautologies without it.

## Open questions

- **No cost attribution.** Hindsight's model config is server-level, so all
  extraction and reflection spend lands on one LiteLLM key with no per-project
  or per-user breakdown. Accepted for v1; it is why the refresh defaults are
  left at Hindsight's own (no automatic refresh at all).
- **Slug readability.** Derived slugs are ugly (`github-com-acme-payments-api-1a2b3c4d`).
  Renaming is expected and cheap — that is what the forwarding tombstones in
  Plan 2 are for.
