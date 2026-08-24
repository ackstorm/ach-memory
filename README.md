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

## Quickstart

The Compose stack is for local development. It runs Postgres, Hindsight,
migrations, and the API; published ports bind to loopback.

Set the OpenAI-compatible gateway used by Hindsight and generate a master key:

```bash
export HINDSIGHT_LLM_BASE_URL=https://llm.example.com
export HINDSIGHT_LLM_API_KEY=...
export MEMORY_MASTER_KEY="mem_local_$(openssl rand -hex 32)"
export MEMORY_MASTER_KEY_HASH=$(python3 -c \
  "import hashlib,os; print(hashlib.sha256(os.environ['MEMORY_MASTER_KEY'].encode()).hexdigest())")

docker compose up -d --build
```

The master key is used to provision a user and mint a user key. The API is at
`http://localhost:8000`; use its `/docs` page for the REST workflow.

## MCP

Configure an MCP-capable agent with:

```text
POST http://<host>:8000/mcp/
Authorization: Bearer <user key>
```

The endpoint exposes 15 tools as one memory surface: retain, sync retain,
recall, reflect, memory curation, document operations, and async-operation
inspection/cancellation. The bearer credential is the same user key used by
REST; the master key is rejected on MCP. v1 supports native/non-browser MCP
clients only: requests with a browser `Origin` are not supported.

MCP callers provide a memory scope (`user` or `project`) and, for project
scope, a project slug. Tenant, user, and Hindsight bank identifiers are
resolved server-side and are not accepted as caller-controlled bank IDs.

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

The required settings are:

```text
MEMORY_DATABASE_URL
MEMORY_MASTER_KEY_HASH
MEMORY_HINDSIGHT_URL
```

Useful optional settings include `MEMORY_HINDSIGHT_API_KEY`,
`MEMORY_TENANT_ID`, `MEMORY_MCP_ALLOWED_HOSTS`,
`MEMORY_HINDSIGHT_TIMEOUT_SECONDS`, `MEMORY_HINDSIGHT_LLM_TIMEOUT_SECONDS`,
`MEMORY_WRITE_LIMIT`, and `MEMORY_WRITE_WINDOW_SECONDS`. See
[src/memory/config.py](src/memory/config.py) for defaults and the full list.

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

For deployment, see the
[Helm chart guide](deploy/helm/README.md). The chart runs ach-memory only;
Postgres and Hindsight are dependencies supplied by the deployment.

Further operational context lives in
[docs/PROJECT-STATE.md](docs/PROJECT-STATE.md).

## License

MIT. See [LICENSE](LICENSE).
