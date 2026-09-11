# ach-memory

`ach-memory` is a multi-tenant memory service for coding agents, built on
[Hindsight](https://github.com/vectorize-io/hindsight) (MIT).

Hindsight provides the memory engine; ach-memory adds the application boundary
around it: tenant isolation, user and project scopes, credential and access
control, and a small REST/MCP surface that agents can use safely. Hindsight
`bank_id` values stay an internal implementation detail.

## What it provides

- User-scoped and project-scoped memory with project ownership and forwarding
  after renames.
- Identity entirely from outside: a JWKS-verified JWT or a platform key
  resolved over HTTP. This service mints no credential and stores none.
- Operator authority as configuration (`MEMORY_MASTER_USERS`,
  `MEMORY_MASTER_GROUPS`) over an already-resolved identity, rather than a
  shared secret.
- REST endpoints for memory, recall/history, projects, documents, operations,
  curation, directives, mental models, and audit access.
- A streamable HTTP MCP surface backed by the same authorization and memory
  operations as REST — memory read/write, mental-model governance, Working
  State and bounded context loading. The exact tool set is pinned in
  `tests/test_mcp_tools.py` rather than restated here, so this description
  never drifts from what a host actually sees.
- Explicit, proactive `retain` through the canonical retain/curation skill
  shared by Claude Code, Codex, OpenCode and Pi: an agent decides when a
  claim is durable and independently correctable enough to keep, sanitizes
  and canonicalizes it (a 4 KiB limit, secret rejection, `ach-exact-v1`,
  English-only in 0.4.0), and stores it with typed `memory_type`/`basis`/
  `trigger`/evidence — never an implicit background capture.
- Reversible memory curation (`forget`, `restore`, `correct`), each proving
  its Hindsight outcome before ACH's own record changes. `correct` uses a
  caller-visible operation ID so exact retries deduplicate without collapsing
  separate corrections that happen to return to an earlier value.
- Governed mental models: one built-in plus five custom models per bank.
  Standing delivery is a property of being built-in, not a caller-settable
  flag, and a model withheld from delivery — never served as falsely
  current — until its refresh operation is proven complete.
- Bounded standing-context loading (`ach-memory context load` / `load_context`):
  the bank's built-in model, Project Metadata, active time-bounded claims and
  explicit Working State, each under its own token budget, assembled inside
  one two-second deadline that fails a slow model open rather than stalling
  the whole response. An optional `scope` filter narrows delivery to just
  the user or project half.
- `recall`/`reflect` may also expire a bounded batch of claims already past
  their stated expiry as a side effect of the access; both are honestly
  advertised over MCP as non-read-only for exactly that reason.
- Helm packaging for deployments where Postgres and Hindsight are managed
  separately.

The complete contract is [SPEC-v1.md](SPEC-v1.md); the 0.4.0 product
specification is [docs/specs/2026-09-03-ach-memory-v0.4.0.md](docs/specs/2026-09-03-ach-memory-v0.4.0.md).
The interactive REST reference is available at `/docs` when the service is
running.

Compatibility is measured against Hindsight 0.9.2. Shipping a release does
not activate production behavior, mutate production banks, or perform data
cleanup — those require separate approval (see
[docs/releases/0.4.0.md](docs/releases/0.4.0.md)).

## Agent setup

Copy the example and start the local stack. The shipped mock settings make no
real LLM calls.

```bash
cp .env.example .env
docker compose up -d --build
```

There is no key to mint. `ACH_MEMORY_API_KEY` holds **whatever token your
identity provider issues** — a JWT from the issuer named in
`MEMORY_AUTH_JWT_*`, or the platform key named in `MEMORY_AUTH_PLATFORM_*`.
The variable keeps its name because it keeps its job; only the source of the
value changed. See [Authentication](#authentication) below.

Locally there is no such provider, so the Compose stack ships one: a
`dev-identity` sidecar ([deploy/dev-identity/whoami.py](deploy/dev-identity/whoami.py))
answering the same whoami shape LiteLLM does. It is a deployment artifact and
not a third way in — the service is configured for the ordinary platform
resolver and cannot tell the difference. **The token is the identity**, so any
value names a person and `+` adds groups:

```bash
export ACH_MEMORY_URL=http://localhost:8000/mcp/
export ACH_MEMORY_API_KEY=alice          # alice, no groups
# export ACH_MEMORY_API_KEY=alice+sre    # ... and in the group `sre`
```

To hold operator authority on the local stack, name yourself before bringing
it up — `MEMORY_MASTER_USERS=alice` in `.env`.

That sidecar authenticates **everybody**, by construction. It exists so the
stack is drivable without an `if dev:` branch in the auth code; never run it
anywhere else.

Put the endpoint and your token in your shell profile, so every agent inherits
them however it is launched:

```bash
# ~/.zshrc or ~/.bashrc
export ACH_MEMORY_URL=https://memory.example.com/mcp/
export ACH_MEMORY_API_KEY=<token from your identity provider>
```

All four hosts run the same server: a local stdio proxy that forwards to the
service and resolves the calling project per SPEC §8. The config every host
ends up with is the ordinary MCP stdio shape — install source and endpoint as
arguments, credential in `env`:

```json
{
  "command": "uvx",
  "args": [
    "--from", "git+https://github.com/ackstorm/ach-memory@v0.7.1",
    "ach-memory", "mcp",
    "--url", "https://memory.example.com/mcp/"
  ],
  "env": { "ACH_MEMORY_API_KEY": "<token from your identity provider>" }
}
```

`uvx --from git+…@vX.Y.Z` needs no package index: the repository is public and
the tag pins an immutable revision. The endpoint is an explicit `--url` so the
config states what it talks to; the key stays in `env` (or inherited from your
shell) because `ps aux` shows every argument of every process. `--url` falls
back to `$ACH_MEMORY_URL` when omitted.

`ACH_MEMORY_URL` is the MCP endpoint, used verbatim — every call this
package makes is MCP, including the standing context the SessionStart hook
loads, so one URL has one meaning and nothing is derived from it. Point it at
this service's own mount (`https://memory.example.com/mcp/`) or at a gateway
publishing it (`https://gateway.example.com/mcp/mcp-ach-memory`); both work
unchanged.

Claude Code installs from this repository's own marketplace:

```bash
claude plugin marketplace add ackstorm/ach-memory
claude plugin install ach-memory@ach-memory
```

Codex takes the same plugin for its hooks and skill, and `ach-memory init
codex` registers the stdio server for it, the same way the other installers do:

```bash
uv run ach-memory init codex
```

OpenCode and pi have no marketplace, so they still need the installer, which
writes their config files for them (see [TODO.md](TODO.md)):

```bash
uv run ach-memory init opencode    # opencode | pi | all
```

`all` covers every supported agent found on your PATH and names the ones it
skipped:

```
ach-memory 0.4.0  →  https://memory.example.com

  ✔ claude    plugin installed from ackstorm/ach-memory
  ✔ opencode  4 files → ~/.config/opencode
  – codex     skipped, not on PATH
  – pi        skipped, not on PATH

Restart claude and opencode to load ach-memory.
```

Add `-v` to list every file written. Restart the agents afterward so they
inherit `ACH_MEMORY_API_KEY`.

Two transport flags cover the non-default cases (`init <target> --local|--http`):

- `--local` writes the stdio entry with this checkout's own `ach-memory`
  script (absolute path) instead of `uvx`, so hosts run unreleased code —
  the way to test a proxy change end to end before cutting a release.
- `--http` skips the proxy and points codex/opencode/pi at the remote HTTP
  endpoint directly (no client-side project resolution; pi gets its
  `pi-mcp-adapter` bridge back). The claude plugin is a committed static
  config, so neither flag applies to it — paste the direct config from the
  MCP section below instead.

Memory is explicit: installation adds memory tools, but agents do not retain or
recall anything automatically. Ask an agent to use memory when you want it to.
`memory_history` and `load_context` are genuinely read-only and never create
a project or enrich repository metadata. `recall` and `reflect` do not create
or enrich either, but both may expire a bounded batch of past-due claims as a
side effect of the access, so neither advertises `readOnlyHint`; `reflect`
also remains a confirmation-requiring, rate-limited LLM operation.

## MCP

`uvx ach-memory mcp` (stdio, above) is the default for all four hosts, but the
remote streamable HTTP endpoint it proxies to remains fully supported for any
MCP-capable agent that prefers to speak to it directly:

```text
POST http://<host>:8000/mcp/
Authorization: Bearer <token>
```

The trailing slash is not optional: `/mcp` answers `307` to `/mcp/`, and
because the transport is stateless that redirect costs a round trip on
every tool call, not just the first. `ach-memory init` already writes the
correct form.

The credential is whatever your identity provider issued — a JWT, or the key
a platform forwards on the header it is configured to use. An operator
identity is accepted on MCP but carries no operator authority there
(invariant 22): the same person is an operator over REST and an ordinary user
here. v1 supports native/non-browser MCP clients only.

`ach-memory context load` reads the endpoint and credential from
`ACH_MEMORY_URL` / `ACH_MEMORY_API_KEY` and prints exactly the authorized
standing-context text a session receives. Diagnostics and omissions go to stderr
so stdout can be injected directly into an agent context.

### Direct HTTP

A host that reads its own `mcpServers`-style JSON, such as Claude Code before
`ach-memory init`, can point at the endpoint above without going through the
stdio proxy at all:

```json
{
  "mcpServers": {
    "ach-memory": {
      "type": "http",
      "url": "${ACH_MEMORY_URL:-http://localhost:8000/mcp/}",
      "headers": {
        "Authorization": "Bearer ${ACH_MEMORY_API_KEY}"
      }
    }
  }
}
```

### A minimal tool set for a harness facade

A harness that fronts ach-memory with its own facade, exposing only a
subset of the MCP tools, needs at least these four to get a working
retain/recall loop:

- `retain` — store a claim. Returns immediately with an operation, not the
  stored result.
- `sync_retain` — same write, but waits until the claim is searchable
  before returning.
- `get_operation` — check whether an async `retain` has finished.
- `recall` — search memory and return grounded matching facts.

Omitting `sync_retain` and `get_operation` leaves a caller with no way to
know when a `retain` is searchable, other than guessing with a fixed delay
and retrying blind — `retain`'s own description says as much. Expose both,
or expose `sync_retain` alone and drop `retain`, rather than reimplementing
either as a client-side poll loop.

## Important limits

- `bank_id` never appears in responses or errors.
- Raw retained document text is stored by the Hindsight default and can be
  retrieved. This service is not a secret scanner or a prompt-injection
  defense; put those controls at the gateway if you need them.
- `forget` is reversible. `delete_document` removes the document and all
  memories derived from it and is irreversible. Whole-bank clear/delete are
  master-key-only and irreversible.
- The write limiter is in-process and per replica. It defaults to 60 writes per
  60 seconds per credential; replicas multiply the effective limit.
- `retain`, `recall` and `reflect` accept caller `tags` (e.g. `repo:<path>` to
  separate repositories inside one project bank). They are additive on
  retain and AND-filtered on recall/reflect; server-derived tags (`type:`,
  `basis:`, `schema:`, `validity:`) can never be overridden. Tagging is a
  convention, not an enforced scope: nothing rejects a retain that omits a
  tag, and nothing rejects a recall that forgets to filter by one.

## Seeing what is happening

Three surfaces, because "how much" and "who, where, what" are different
questions and neither store answers the other.

`GET /metrics` — Prometheus exposition, unauthenticated. Counts by action,
scope, surface and outcome, error codes by SPEC §18 code, upstream Hindsight
latency, and request counts by route template. Deliberately aggregate: no
identities, no project names, no content, no bank ids, and never a `user_id`
or `project_slug` label — an unbounded label value kills the Prometheus that
scrapes it, not this service. Disable with `MEMORY_METRICS_ENABLED=false`.

`GET /v1/admin/activity` and `GET /v1/admin/activity/summary` — operators
only, tenant-filtered. One record per data-plane call: which credential, which
action, which bank (as `scope` + `user_id`/`project_slug`, plus a
non-reversible `bank_fingerprint`), how many bytes, how long it took, and
whether it actually landed. The summary rolls this up per bank with 24 hourly
buckets, which is how you see that an agent went quiet.

Rows carry no memory content, ever — a copy here would survive
`DELETE /v1/admin/memory/{scope}` and quietly stop that from being a complete
erasure. To read what was actually written, read the bank itself through
`POST /v1/memory/list`; that path is authorized and audited. Rows age out
after `MEMORY_ACTIVITY_RETENTION_DAYS` (default 30).

Authentication failures are counted in `memory_errors_total`, not recorded as
rows — on a public ingress a row per rejected credential is an unbounded
insert. A wedged agent still reads clearly: a silent fleet row plus a 401 spike.

`GET /admin/ui` — a single static page over those two routes, plus a tab that
reads a bank live. No build step, no CDN, no third-party JavaScript: it holds
the operator's token for the tab only (`sessionStorage`, never
`localStorage`).
Disable with `MEMORY_ADMIN_UI_ENABLED=false`.

Both `/metrics` and `/admin/ui` sit behind the same ingress as everything else.
If that ingress is public, so are they.

`/metrics` is served by the same app on the same port as the API — there is no
second port to publish. The chart ships a `ServiceMonitor` for the Prometheus
Operator, off by default because rendering it without the operator's CRDs fails
the install:

```yaml
metrics:
  enabled: true
  serviceMonitor:
    enabled: true
    labels:
      release: kube-prometheus-stack   # if your Prometheus selects on one
```

`metrics.enabled: false` removes the route and the ServiceMonitor together, so a
monitor can never point at an endpoint that is switched off.

## Authentication

**Every credential is issued elsewhere.** This service mints none, stores none
and verifies none of its own, so at least one provider must be configured or
nothing can authenticate. Two ways in, tried in a fixed order and fail-closed:
the token's own shape selects its provider, and that choice is final -- a
rejected credential is never retried as something else.

1. **A JWKS-verified JWT** on `Authorization: Bearer <token>`. Use it when an
   identity provider you already run (ACH, Dex) mints tokens for the agent.
   A token is routed here by its own shape, so this can share a header with
   the platform key below.
2. **A platform API key** on a header you name. Use it when callers arrive
   through a platform that forwards its own key rather than a token this
   service could verify offline (LiteLLM). Identity comes from an HTTP round
   trip to that platform, cached on success only. `_INCOMING_HEADER` accepts
   a comma-separated list tried in order (first present wins), so one
   deployment can accept the key from more than one header -- e.g.
   `x-litellm-api-key,authorization`, where a gateway forwards
   `x-litellm-api-key` and a local stdio client sends `Authorization: Bearer`.
   `authorization` matches only a non-JWT bearer; a JWT there still routes to
   provider 1.

Both can run together: the JWT is primary, the platform header is the
fallback. Both can also assert group membership, which authorizes projects
owned by those groups with no row anywhere — and which the provider can revoke
just by no longer asserting it.

### Operator authority

Neither provider can grant it: a token that claims to be an operator is still
just a user. Authority is configuration read over the already-resolved
identity, so an operator is an ordinary external user — with their own bank
and their own projects — whom `MEMORY_MASTER_USERS` or `MEMORY_MASTER_GROUPS`
also names.

```bash
MEMORY_MASTER_USERS=juancarlos@example.com
MEMORY_MASTER_GROUPS=sre,platform
MEMORY_MASTER_ISSUER=https://idp.example.com
```

`MEMORY_MASTER_USERS` names the **subject your identity provider asserts** —
the `sub` or email on the token. Not a `usr_...` id: those are minted here on
a caller's first request and nobody can predict one in advance, so naming one
grants nothing.

`MEMORY_MASTER_ISSUER` says which provider may grant, named by its issuer (the
JWT issuer URL, or the platform resolver URL). It is required once more than
one provider is enabled, because a subject is only unique within the issuer
that minted it and a caller chooses their provider by choosing which header to
send: with both on and no issuer named, a `MEMORY_MASTER_USERS` entry naming a
JWT subject is equally satisfied by a platform credential resolving to the same
string, and a group by a matching `team_id`. Neither request contains a bad
credential.

All three default to empty, which grants **nobody**. The service refuses to
start on either of the two configurations that could over-grant: an entry that
parses to empty, or a grant with more than one provider enabled and no
`MEMORY_MASTER_ISSUER`. It gates the audit log, bank clear and delete, slug release, the
fleet view and `On-Behalf-Of` delegation — and unlike a shared secret it puts
a person in `actor_key_id`, which is the only thing that matters when
reviewing why somebody touched another user's bank.

Full rules in [SPEC-v1.md](SPEC-v1.md) §5.3.

**ACH, via JWT:**

```bash
MEMORY_AUTH_JWT_ENABLED=true
# Must equal the token's `iss` claim EXACTLY. ACH sets `iss` to its own
# ACH_BASE_URL verbatim, so this stays the public URL even in-cluster.
MEMORY_AUTH_JWT_ISSUER=https://ach.example.com
# Point the key fetch in-cluster HERE instead. Changing the issuer to the
# in-cluster URL to avoid the egress is the tempting mistake and it rejects
# every token, because `iss` then no longer matches. Defaults to
# <issuer>/.well-known/jwks.json when unset; Dex publishes at /keys.
MEMORY_AUTH_JWT_JWKS_URI=http://ach.ach.svc/.well-known/jwks.json
# Required unless MEMORY_AUTH_JWT_VERIFY_AUDIENCE=false. Comma-separated.
MEMORY_AUTH_JWT_AUDIENCE=mcp:ach-memory
```

The agent then forwards whatever ACH issued it, unchanged:

```text
Authorization: Bearer eyJhbGci...
```

**LiteLLM, via platform key:**

```bash
MEMORY_AUTH_PLATFORM_ENABLED=true
# The header the caller sends us...
MEMORY_AUTH_PLATFORM_INCOMING_HEADER=x-litellm-api-key
# ...and the header we send that key back on to ask who owns it. They are
# separate because the resolver need not want it under the same name.
MEMORY_AUTH_PLATFORM_RESOLVER_HEADER=x-litellm-api-key
MEMORY_AUTH_PLATFORM_RESOLVER_URL=http://litellm.<ns>.svc:4000/v2/user/info
# Where the identity and the groups sit in that resolver's JSON. Both required,
# neither defaulted -- see below.
MEMORY_AUTH_PLATFORM_USER_FIELD=user_id
MEMORY_AUTH_PLATFORM_GROUPS_FIELD=teams
```

`/v2/user/info` defaults to a self-lookup when `user_id` is omitted, so the
caller's own key identifies the caller. A key not bound to a user answers 400
and is refused.

Point it instead at `alitellm-auth` if you run it. That reads the key from
`x-alitellm-auth-api-key` and holds the LiteLLM master key itself, at the cost
of a second hop.

```bash
MEMORY_AUTH_PLATFORM_RESOLVER_HEADER=x-alitellm-auth-api-key
MEMORY_AUTH_PLATFORM_RESOLVER_URL=http://alitellm-auth.<ns>.svc/api/oauth/whoami
MEMORY_AUTH_PLATFORM_USER_FIELD=user_id
MEMORY_AUTH_PLATFORM_GROUPS_FIELD=team_id
```

### Naming the two fields

`_USER_FIELD` and `_GROUPS_FIELD` are dotted paths into the resolver's JSON, so
a wrapped answer is addressable: `info.user_id` reads
`{"info": {"user_id": ...}}`. A key containing a literal dot cannot be named —
the path splits on it. For groups, a bare string and a list of strings are both
accepted.

Both are required whenever platform auth is on, and neither has a default,
because there is no standard to default to:

| Resolver | `_USER_FIELD` | `_GROUPS_FIELD` |
| --- | --- | --- |
| LiteLLM `/v2/user/info` | `user_id` | `teams` (list) |
| LiteLLM `/key/info` | `info.user_id` | `info.team_id` |
| alitellm-auth `/api/oauth/whoami` | `user_id` | `team_id` |

A default would be right for one of these and silently wrong for the rest —
wrong in the dangerous direction, since a groups path that matches nothing
still authenticates the caller and merely leaves them with no membership, so
every group-owned project quietly stops authorizing. Demanding both turns that
into a refusal to start.

The resolver's value at `_USER_FIELD` becomes the identity. Nothing at that
path is a 401, not a 500 — including when the path itself is misconfigured.
A resolver that is unreachable or failing returns `AUTH_BACKEND_UNAVAILABLE`
(503), never a 401 — an outage upstream is not a bad credential.

Behind a gateway that forwards headers selectively, the incoming header has to
be on its allow-list. LiteLLM's MCP gateway forwards only the headers named in
its server registration's `extra_headers`, and some proxies (e.g.
stacklok/toolhive) block `Authorization` as a passthrough outright. When a
local stdio client sits behind such a proxy, set `ACH_MEMORY_HEADER` to a
header the proxy does pass (e.g. `x-litellm-api-key`); the client then sends
the key there instead of on `Authorization`, and the service must list that
header in `_INCOMING_HEADER`. The two only have to agree on the name.

## Configuration

| Setting | Default |
| --- | --- |
| `MEMORY_DATABASE_URL` | required |
| `MEMORY_HINDSIGHT_URL` | required |
| `MEMORY_MASTER_USERS` | empty (grants nobody) |
| `MEMORY_MASTER_GROUPS` | empty (grants nobody) |
| `MEMORY_MASTER_ISSUER` | empty (required with >1 provider) |
| `MEMORY_HINDSIGHT_API_KEY` | empty |
| `MEMORY_TENANT_ID` | `default` |
| `MEMORY_MAX_CONTENT_BYTES` | `256000` |
| `MEMORY_MCP_ALLOWED_HOSTS` | `127.0.0.1,localhost,127.0.0.1:*,localhost:*` |
| `MEMORY_HINDSIGHT_TIMEOUT_SECONDS` | `30` |
| `MEMORY_HINDSIGHT_LLM_TIMEOUT_SECONDS` | `180` |
| `MEMORY_RECALL_MIN_SEMANTIC` | `0.60` |
| `MEMORY_RECALL_RELATIVE_CUT` | `0.01` |
| `MEMORY_RECALL_KEYWORD_ONLY_MIN_RERANKER` | `0.10` |
| `MEMORY_WRITE_LIMIT` | `60` |
| `MEMORY_WRITE_WINDOW_SECONDS` | `60` |
| `MEMORY_PROJECT_CREATION_LIMIT` | `10` |
| `MEMORY_PROJECT_CREATION_WINDOW_SECONDS` | `3600` |
| `MEMORY_METRICS_ENABLED` | `true` |
| `MEMORY_ADMIN_UI_ENABLED` | `true` |
| `MEMORY_ACTIVITY_RETENTION_DAYS` | `30` |
| `MEMORY_AUTH_JWT_ENABLED` | `false` |
| `MEMORY_AUTH_JWT_ISSUER` | empty (required when JWT is enabled) |
| `MEMORY_AUTH_JWT_JWKS_URI` | empty (derived from the issuer) |
| `MEMORY_AUTH_JWT_AUDIENCE` | empty (required unless verification is off) |
| `MEMORY_AUTH_JWT_VERIFY_AUDIENCE` | `true` |
| `MEMORY_AUTH_JWT_GROUPS_CLAIM` | `groups` |
| `MEMORY_AUTH_PLATFORM_ENABLED` | `false` |
| `MEMORY_AUTH_PLATFORM_INCOMING_HEADER` | empty (required when platform auth is enabled) |
| `MEMORY_AUTH_PLATFORM_RESOLVER_HEADER` | empty (required when platform auth is enabled) |
| `MEMORY_AUTH_PLATFORM_RESOLVER_URL` | empty (required when platform auth is enabled) |
| `MEMORY_AUTH_PLATFORM_CACHE_TTL` | `300` |
| `MEMORY_AUTH_PLATFORM_USER_FIELD` | empty (required when platform auth is enabled) |
| `MEMORY_AUTH_PLATFORM_GROUPS_FIELD` | empty (required when platform auth is enabled) |

The three required variables are supplied by Compose for local setup; deployed
service operators configure them separately. See
[src/memory/config.py](src/memory/config.py) for defaults.

`MEMORY_RECALL_MIN_SEMANTIC` and `MEMORY_RECALL_RELATIVE_CUT` are the two that
change what a caller sees rather than how the service runs, so they are worth
knowing before someone reports that recall "lost" a memory. Together they make
recall withhold hits it cannot justify instead of padding the answer out to
`max_results`: the first is an absolute floor on semantic similarity (so a
query about nothing in the bank returns nothing at all), the second drops
whatever scores below a fraction of the best hit in the same response. Each
hit that does come back carries the `score` it was ranked by.

**A caller cannot lower either one.** There is no per-request override, on
purpose — a quality contract that any single caller can switch off is not a
contract. Tuning is an operator decision, through these two variables.

The default floor of `0.60` is measured, not guessed. Against
`benchmarks/corpus.jsonl`, it answers all 25 questions, cuts the reply from 68
hits to 12.7, and removes 98.7% of the hits returned for deliberately absurd
queries. It does not have much room above it: `0.62` breaks the smoke test and
`0.65` starts blinding real questions. Note that no floor separates the two
cleanly — the best nonsense hit scores `0.6355` and the weakest answered
question `0.6313` — so raising this trades one error for the other rather than
removing either. If a memory seems genuinely missing from a recall, setting
`MEMORY_RECALL_MIN_SEMANTIC=0` is how to tell a withheld hit from one that was
never retrieved. The reasoning and the full table are in
[src/memory/config.py](src/memory/config.py).

## Development

```bash
uv sync --dev
make verify
make e2e
```

`make verify` is the local lint, test, secret-scan and Helm gate. It starts its
own Postgres on port 5434 (`make testdb`, idempotent; `make testdb-rm` to drop
it) — deliberately a different server from the compose stack rather than
another database inside it, since both defaulted to 5433 and whichever
container held the port served the suite.

`make e2e` runs Hindsight and its databases for real but uses Hindsight's
`MockLLM`, makes zero external LLM calls, and tears down its isolated stack and
volumes afterward.

For how to cut a release — and why a change under `plugins/` needs one — see
[docs/reference/RELEASING.md](docs/reference/RELEASING.md).

For deployment, see the
[Helm chart guide](deploy/helm/README.md). The chart runs ach-memory only;
Postgres and Hindsight are dependencies supplied by the deployment.

Further operational context lives in
[docs/PROJECT-STATE.md](docs/PROJECT-STATE.md).

## License

MIT. See [LICENSE](LICENSE).
