# ach-memory

A governed, engine-neutral access and delivery layer for memory engines, used
by coding agents over MCP. The engine remembers; ach-memory decides who may
read and write which bank, keeps the journal, scrubs secrets, and delivers a
bounded standing context at session start. Hindsight is the first engine.

Status: 0.1.0. Contract in [`SPEC.md`](SPEC.md); why it looks like this in
[`docs/lessons.md`](docs/lessons.md).

```
agent host (Claude Code, Codex)
  │ stdio                       ┌────────────────────────────┐
  ▼                             │ ach-memory (this service)   │
ach-memory mcp  ── HTTPS/MCP ──▶│  auth → scope → service     │──▶ Postgres
  (proxy: git origin → slug,    │  retain · recall · curation │    users, projects,
   auth header)                 │  standing context           │    journal
                                │  Backend ABC ─ Hindsight    │──▶ Hindsight (banks,
                                └────────────────────────────┘    mental models)
```

## Tools

`retain` · `recall` · `reflect` · `forget` · `restore` · `correct` ·
`delete_memory` · `list_memories` · `get_memory` · `history` ·
`load_context` · `get_operation`.
One MCP endpoint (`/mcp/`), one health route (`/health`). No REST data plane.

## Install on a host

See [`docs/hosts.md`](docs/hosts.md). Short version:

```bash
export ACH_MEMORY_URL=https://api.<domain>/memory/mcp/
export ACH_MEMORY_API_KEY=<your platform key>
uvx --from git+https://github.com/ackstorm/ach-memory@v0.3.0 ach-memory init all   # claude, codex, opencode, pi
```

## Run it

```bash
make up          # Postgres + Hindsight (mock LLM) + migrations + API on :8000
make e2e         # the same stack, then scripts/mcp-smoke.py over all twelve tools
make test        # unit + contract tests against the fake engine (Postgres on :5434)
MEMORY_HINDSIGHT_URL=http://localhost:8888 uv run pytest -m integration tests/backend
```

## Configure

Every setting is an environment variable with the `MEMORY_` prefix
(`src/memory/config.py`). The ones a deployment must set:

| Variable | Meaning |
|---|---|
| `MEMORY_DATABASE_URL` | Postgres, `postgresql+psycopg://…` |
| `MEMORY_HINDSIGHT_URL` | engine base URL (`…/api` in the cluster, bare `:8888` locally) |
| `MEMORY_MCP_ALLOWED_HOSTS` | Host header values the MCP transport accepts (421 otherwise) |
| `MEMORY_AUTH_PLATFORM_*` | who owns the caller's key: incoming header, resolver URL/header, user and groups fields |
| `MEMORY_AUTH_JWT_*` | alternatively, JWKS-verified JWTs |
| `MEMORY_MASTER_USERS` / `_GROUPS` | operators, matched on the identity provider's subject |

Engine profile (`verbatim` extraction, `types=world` recall, semantic floor)
lives in the Hindsight adapter, not in core; see `SPEC.md` §5.

## Add an engine

Subclass `memory.backend.base.Backend` in `src/memory/backend/<name>.py`, add
one line to `get_backend()`, and run the contract suite
(`tests/backend/test_contract.py`) against it. Core never imports a concrete
adapter.

## Deploy

Helm chart in `deploy/helm/ach-memory` (image `ghcr.io/ackstorm/ach-memory`,
chart `oci://ghcr.io/ackstorm/charts/ach-memory`). Release:
`make release-bump VERSION=x.y.z` → commit → `make release-cut VERSION=x.y.z`;
CI publishes the image, the chart and the GitHub release from the tag.
