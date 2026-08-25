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
- User keys for agents and a separate master key for provisioning and admin
  operations.
- REST endpoints for memory, users, projects, groups, documents, operations,
  curation, directives, mental models, and audit access.
- A 15-tool streamable HTTP MCP surface backed by the same authorization and
  memory operations as REST.
- Asynchronous retain operations, recall, reflect, and reversible memory
  curation (`forget`, `restore`, and `correct`).
- Helm packaging for deployments where Postgres and Hindsight are managed
  separately.

The complete contract is [SPEC-v1.md](SPEC-v1.md). The interactive REST
reference is available at `/docs` when the service is running.

## Agent setup

Copy the example, set `MEMORY_MASTER_KEY` and its SHA-256 value in `.env`, then
start the local stack. The shipped mock settings make no real LLM calls.

```bash
cp .env.example .env
# Edit .env: MEMORY_MASTER_KEY=... and MEMORY_MASTER_KEY_HASH=<sha256 of that key>
docker compose up -d --build
```

Mint one user key without placing the master key in a curl command argument:

```bash
set -a; . ./.env; set +a
curl_config=$(mktemp)
chmod 600 "$curl_config"
trap 'rm -f "$curl_config"' EXIT
cat >"$curl_config" <<EOF
header = "Authorization: Bearer $MEMORY_MASTER_KEY"
header = "Content-Type: application/json"
EOF
user_id=$(curl --config "$curl_config" -fsS -X POST http://localhost:8000/v1/users -d '{}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["user_id"])')
user_key=$(curl --config "$curl_config" -fsS -X POST "http://localhost:8000/v1/users/$user_id/keys" -d '{}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
export ACH_MEMORY_URL=http://localhost:8000
export ACH_MEMORY_API_KEY="$user_key"
```

Put the endpoint and your key in your shell profile, so every agent inherits
them however it is launched:

```bash
# ~/.zshrc or ~/.bashrc
export ACH_MEMORY_URL=https://memory.example.com
export ACH_MEMORY_API_KEY=<user-key>
```

Claude Code installs from this repository's own marketplace:

```bash
claude plugin marketplace add ackstorm/ach-memory
claude plugin install ach-memory@ach-memory
```

Claude resolves both values at run time, so nothing is written per install and
the same commands work against any deployment. With neither variable set it
falls back to `http://localhost:8000`, which is what `docker compose up` serves.

Codex takes the same plugin for its hooks and skill, but cannot expand
`${ACH_MEMORY_URL}` in a URL, so its server has to be registered separately.
`ach-memory init codex` does that for you, using the endpoint exported above:

```bash
uv run ach-memory init codex
```

Only the URL is fixed at install time; the key is read from the environment on
every call, so rotating it needs no reinstall. Because the URL is pinned,
`init codex` refuses to run without `ACH_MEMORY_URL` rather than quietly
recording `http://localhost:8000`, and re-running it after changing the
variable repoints the server.

OpenCode and pi have no marketplace, so they still need the installer, which
writes their config files for them (see [TODO.md](TODO.md)):

```bash
uv run ach-memory init opencode    # opencode | pi | all
```

`all` covers every supported agent found on your PATH and names the ones it
skipped:

```
ach-memory 0.1.2  →  https://memory.example.com

  ✔ claude    plugin installed from ackstorm/ach-memory
  ✔ opencode  4 files → ~/.config/opencode
  – codex     skipped, not on PATH
  – pi        skipped, not on PATH

Restart claude and opencode to load ach-memory.
```

Add `-v` to list every file written. Restart the agents afterward so they
inherit `ACH_MEMORY_API_KEY`.

Memory is explicit: installation adds memory tools, but agents do not retain or
recall anything automatically. Ask an agent to use memory when you want it to.

## MCP

Configure another MCP-capable agent with:

```text
POST http://<host>:8000/mcp/
x-ach-memory-key: <user key>
```

`Authorization: Bearer <user key>` also works. Prefer the dedicated header when
a gateway already uses `Authorization`; when both are sent,
`x-ach-memory-key` wins. The master key is rejected on MCP, and v1 supports
native/non-browser MCP clients only.

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

## Configuration

| Setting | Default |
| --- | --- |
| `MEMORY_DATABASE_URL` | required |
| `MEMORY_MASTER_KEY_HASH` | required |
| `MEMORY_HINDSIGHT_URL` | required |
| `MEMORY_HINDSIGHT_API_KEY` | empty |
| `MEMORY_TENANT_ID` | `default` |
| `MEMORY_MAX_CONTENT_BYTES` | `256000` |
| `MEMORY_MCP_ALLOWED_HOSTS` | `127.0.0.1,localhost` |
| `MEMORY_HINDSIGHT_TIMEOUT_SECONDS` | `30` |
| `MEMORY_HINDSIGHT_LLM_TIMEOUT_SECONDS` | `180` |
| `MEMORY_WRITE_LIMIT` | `60` |
| `MEMORY_WRITE_WINDOW_SECONDS` | `60` |
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
| `MEMORY_AUTH_PLATFORM_GROUPS_FIELD` | `team_id` |

The three required variables are supplied by Compose for local setup; deployed
service operators configure them separately. See
[src/memory/config.py](src/memory/config.py) for defaults.

## Development

```bash
uv sync --dev
make verify
make e2e
```

`make e2e` runs Hindsight and its databases for real but uses Hindsight's
`MockLLM`, makes zero external LLM calls, and tears down its isolated stack and
volumes afterward. `make verify` is the local lint, test, secret-scan, and Helm
gate.

### Releasing

`make release-bump VERSION=X.Y.Z` then `make release-cut VERSION=X.Y.Z`.

**Any change under `plugins/` needs a release, not just a commit.** Agent hosts
cache an installed plugin by version: with the version unchanged, both
`claude plugin update` and `claude plugin marketplace update` report success,
keep the stale copy, and the change never reaches anyone who already installed.
Recovering without a bump means `claude plugin uninstall`,
`rm -rf ~/.claude/plugins/cache/ach-memory`, then reinstall — which is not
something to ask users to do. `release-bump` rewrites the plugin manifests
alongside pyproject.toml and Chart.yaml, so cutting a release is all it takes;
a test fails if a manifest carrying a version is not in that list.

For deployment, see the
[Helm chart guide](deploy/helm/README.md). The chart runs ach-memory only;
Postgres and Hindsight are dependencies supplied by the deployment.

Further operational context lives in
[docs/PROJECT-STATE.md](docs/PROJECT-STATE.md).

## License

MIT. See [LICENSE](LICENSE).
