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
`load_context` · `get_operation` · `rename_project`.
One MCP endpoint (`/mcp/`), one health route (`/health`). No REST data plane.

## Install on a host

Every host runs the same thing: a local stdio proxy (`ach-memory mcp`) that
resolves the project from the checkout's git `origin` and forwards to the
remote service. Per-host details in [`docs/hosts.md`](docs/hosts.md).

Prerequisites: [`uv`](https://docs.astral.sh/uv/) (the plugin starts the proxy
with `uvx`) and the host CLI (`claude`, `codex`, `opencode` or `pi`).

### 1. Connection

The proxy reads its connection from the environment on every request, so it
must be in the shell profile (`~/.zshrc`, `~/.bashrc`) every agent inherits.
Pick one:

```bash
# A. ACH login through the LiteLLM gateway (recommended at ACKstorm).
#    `ach-cli token` prints a 60-minute JWT; the proxy runs it per request.
ach-cli login
cat >> ~/.zshrc <<EOF
export ACH_MEMORY_URL=https://api.ackstorm.ai/mcp/ach-memory
export ACH_MEMORY_TOKEN_COMMAND="$(command -v ach-cli) token"
EOF

# B. A static key from your identity provider.
export ACH_MEMORY_URL=https://api.<domain>/ach-memory/mcp/
export ACH_MEMORY_API_KEY=<your platform key>
# export ACH_MEMORY_HEADER=x-litellm-api-key   # only if a proxy blocks Authorization
```

| Variable | Meaning |
|---|---|
| `ACH_MEMORY_URL` | MCP endpoint, used verbatim. The gateway (`/mcp/ach-memory`) is the one that accepts the `ach-cli` JWT; `/ach-memory/mcp/` reaches the service directly. |
| `ACH_MEMORY_TOKEN_COMMAND` | Command whose stdout is the token. Wins over `ACH_MEMORY_API_KEY`. Run by `sh`, so name the binary's full path, not an alias. |
| `ACH_MEMORY_API_KEY` | Static token, used when no command is set. |
| `ACH_MEMORY_HEADER` | Header carrying the token. Default `Authorization: Bearer <token>`; any other header gets the bare token. Leave unset for A. |

Open a new terminal afterwards: a host started from an old shell keeps the old
environment.

### 2. Install

```bash
claude   plugin marketplace add ackstorm/ach-memory && claude plugin install ach-memory@ach-memory
codex    plugin marketplace add ackstorm/ach-memory && codex  plugin add     ach-memory@ach-memory
opencode plugin ach-memory@git+https://github.com/ackstorm/ach-memory.git --global
pi       install git:github.com/ackstorm/ach-memory
```

opencode also installs straight from the service, with no git and no registry —
the endpoint serves the running release's own bundle:

```bash
opencode plugin https://api.<domain>/ach-memory/plugin --global
```

### 3. Update

Re-running the install command does **not** update Claude Code or Codex: it
answers "already installed" or reuses the cached marketplace snapshot. Refresh
the marketplace, then update, then restart the host:

```bash
claude plugin marketplace update ach-memory && claude plugin update ach-memory@ach-memory
codex  plugin marketplace upgrade ach-memory && codex  plugin add ach-memory@ach-memory
```

opencode and pi: see [`docs/hosts.md`](docs/hosts.md).

### 4. Verify

```bash
claude plugin list | grep -A1 'ach-memory@'     # Version: the latest release
codex  plugin list | grep 'ach-memory@'
codex  mcp list    | grep ach-memory            # `uvx ... ach-memory mcp`, not a URL
cd <a git checkout> && uvx --from git+https://github.com/ackstorm/ach-memory \
  ach-memory context load | head -1             # ### Project Context
```

Troubleshooting:

- `UNAUTHORIZED: missing or malformed credential` — no credential reached the
  service: the variables are not in the host's environment (new terminal,
  restart the host), or `ACH_MEMORY_URL` points past the gateway that accepts
  your token.
- `UNAUTHORIZED: unknown platform credential` — the token arrived but the
  platform does not know it: a JWT sent to `/ach-memory/mcp/`, or a stale key.
- `codex mcp list` shows `ach-memory` as a URL — a hand-written entry from an
  old install shadows the plugin: `codex mcp remove ach-memory`.

This repository is the plugin bundle: each host reads its own root manifest
(`.claude-plugin/`, `.codex-plugin/`, `package.json`) and gets the one skill,
the one set of hook scripts, and a stdio proxy built from the checkout it just
installed -- so there is no version to pin and nothing of ours to run first.

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
