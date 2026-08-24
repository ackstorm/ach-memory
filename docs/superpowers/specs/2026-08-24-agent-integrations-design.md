# Conservative Agent Integrations

## Goal

Make ach-memory an always-available, explicitly used capability in Codex,
Claude Code, OpenCode, and Pi. A new session and every subagent must receive a
short memory protocol without relying on semantic skill selection.

Installation must be one idempotent command:

```text
ach-memory init codex|claude|opencode|pi|all
```

The integration reads the public ach-memory base URL from `ACH_MEMORY_URL`,
defaulting to `http://localhost:8000`, derives its HTTP MCP endpoint, and
references `ACH_MEMORY_API_KEY`. It never copies or persists the credential
itself. Agents never receive or need the internal Hindsight URL.

## Non-goals

- No automatic `recall` for a greeting or other prompt.
- No automatic `retain`, transcript ingestion, prompt capture, session
  tracking, save reminders, or passive subagent capture.
- No automatic Docker Compose or Hindsight startup.
- No new memory API or MCP tools.
- No uniform UI/status subsystem across agents.

These exclusions are deliberate. Unlike Engram's SQLite/FTS path, ach-memory
can cause Hindsight LLM work when memory operations run. The agent therefore
decides when the task justifies a memory call.

## User-visible behavior

At session start the agent receives a compact instruction that ach-memory is
active, that it should recall context when prior decisions or preferences may
matter, and that it should retain only durable information. The same
instruction is delivered to subagents.

Codex and Claude Code show their native temporary hook status, `Loading
ach-memory...`. A prompt such as `hi` does not call MCP and does not write
memory. OpenCode and Pi use their native context injection without adding a
custom banner.

The existing skill remains available for detailed tool guidance and explicit
invocation. The hook does not ask the model to rediscover or load every tool;
it supplies only the invariant activation policy.

## Architecture

One integration bundle owns:

- the canonical activation text;
- the existing `ach-memory` skill;
- the MCP server definition;
- thin host adapters and their manifests.

The activation text has one source. Host adapters translate only the lifecycle
event and output shape required by their host.

The MCP definition is rendered at installation time from `ACH_MEMORY_URL`.
The installer strips trailing slashes and appends `/mcp/`; a deployment under
a path prefix keeps that prefix. The resulting concrete URL is written to each
agent's native configuration, so changing the public endpoint requires
re-running `ach-memory init`.

### Codex

The Codex plugin bundles the skill, MCP definition, and command hooks.
`SessionStart` and `SubagentStart` inject the activation text as additional
developer context. The hook is dependency-free JavaScript and fails open: a
hook error must not block the agent.

No `UserPromptSubmit`, `Stop`, `SessionEnd`, or subagent-output hook is added.

### Claude Code

The Claude plugin bundles the same skill, MCP definition, and activation text.
Its `SessionStart` and `SubagentStart` hooks emit Claude's native context shape.
It has the same fail-open and no-automatic-memory behavior as Codex.

### OpenCode

`ach-memory init opencode` installs a small global OpenCode plugin and the
skill. The plugin uses OpenCode's system-context transform so root agents and
subagents receive the activation text on model dispatch. The installer adds a
remote `ach-memory` MCP entry to the global OpenCode configuration, refreshing
that owned entry on later `init` runs.

The MCP entry targets the endpoint derived from `ACH_MEMORY_URL` and resolves
`ACH_MEMORY_API_KEY` from the environment.

### Pi

`ach-memory init pi` installs `pi-mcp-adapter` through Pi's package manager,
installs the skill and a small Pi extension, and adds an `ach-memory` server to
Pi's global MCP configuration, refreshing that owned entry on later `init`
runs. The server uses Streamable HTTP and `bearerTokenEnv:
"ACH_MEMORY_API_KEY"`.

The Pi extension injects the activation text through `before_agent_start`.
It performs no HTTP requests and registers no duplicate memory tools; MCP
remains owned by `pi-mcp-adapter`.

## Installer

The existing Python package exposes a small CLI without adding a dependency.
`init` requires one target; `all` means exactly Codex, Claude Code, OpenCode,
and Pi.

Before changing files, the installer resolves the user configuration directory
and checks that every requested agent executable exists. It then opens an MCP
connection with `ACH_MEMORY_API_KEY` and lists the available tools. This
validates the public URL, rejects accidental use of the master key, and does
not invoke Hindsight or an LLM. An `all` preflight failure makes no changes.
Explicit single-agent setup fails clearly when its agent is unavailable.

The installer then:

1. invokes native plugin/package installation where the host provides it;
2. copies only ach-memory-owned adapter and skill files where no portable
   plugin installation exists;
3. upserts the installer-owned `ach-memory` MCP entry without replacing
   unrelated keys or configuration;
4. reports files and registrations changed, plus the required agent restart.

Re-running the same command produces the same effective configuration. It may
refresh owned adapter files and the `ach-memory` MCP entry, including its
public URL; other user configuration and credentials are not overwritten. No
`--force`, interactive wizard, auto-detection, or setup state database is
included in v1.

## Installation UX

The README describes the complete setup in two roles.

For a local service operator:

1. copy `.env.example` to `.env` and configure the master-key pair and
   `HINDSIGHT_LLM_*` settings; the shipped example contains every field used
   by this flow;
2. run `docker compose up -d --build`; Compose supplies
   `MEMORY_HINDSIGHT_URL=http://hindsight:8888` internally;
3. provision a user and one user key through the REST API, using a
   permission-restricted curl config file so the master key never appears in
   process arguments;
4. export `ACH_MEMORY_URL=http://localhost:8000` and the minted
   `ACH_MEMORY_API_KEY`;
5. run `ach-memory init <agent|all>` and restart the selected agents.

From a source checkout the documented command is `uv run ach-memory init ...`.
An installed package exposes the same command directly.

For a remote user, the service operator supplies only the public ach-memory
base URL and a user key. The user exports those two values and runs the same
`init` command. Hindsight, Postgres, the master key, and LLM configuration stay
server-side.

`ACH_MEMORY_URL` is an installation input and may remain exported for clarity,
but the installer materializes the derived MCP URL in agent configuration.
`ACH_MEMORY_API_KEY` must be present whenever an agent starts because generated
configuration references the environment variable rather than its value.

## Configuration and security

- `ACH_MEMORY_API_KEY` is referenced by name and never written into generated
  files, command arguments, logs, or hook output.
- `ACH_MEMORY_URL` must be an absolute `http` or `https` URL without a query or
  fragment. It defaults to `http://localhost:8000`.
- Generated HTTP MCP configuration appends `/mcp/` to the normalized public
  base URL.
- Hooks contain no secrets and make no network calls.
- Existing agent configuration is parsed before modification. Invalid JSON or
  unsupported structure fails without replacing the file.

## Failure behavior

Activation hooks fail open and remain silent on failure. After installation,
a later MCP outage surfaces only when the agent explicitly tries a memory
tool. This keeps an unavailable memory service from blocking coding work.

Installation failures are explicit and non-zero. Each configuration write is
atomic. The installer preflights `all`, but it does not attempt cross-file
rollback after an unexpected filesystem or native-agent failure; its
idempotency makes re-running the recovery path.

## Verification

Automated checks cover:

- installer idempotency in isolated temporary homes;
- preservation of unrelated user configuration and refresh of the owned MCP
  entry;
- local, remote, path-prefixed, and invalid `ACH_MEMORY_URL` handling;
- authenticated MCP preflight without a memory-tool invocation;
- absence of literal credentials in generated files and process arguments;
- Codex and Claude `SessionStart`/`SubagentStart` hook output;
- OpenCode system-context injection;
- Pi `before_agent_start` injection and MCP configuration;
- packaging of every adapter, manifest, skill, and activation resource.

Local product verification uses the installed versions present in the test
environment: Codex 0.149.1, Claude Code 2.1.241, OpenCode 1.18.10, and Pi
0.84.3. It confirms plugin/skill discovery and MCP registration for all four.
A Codex session performs the existing explicit retain/recall smoke against the
Docker Compose stack. Starting an agent with `hi` must activate the protocol
without producing a memory request.

## Deferred work

Automatic recall, automatic retention, session summaries, compaction capture,
prompt ingestion, service auto-start, status-bar integrations, and additional
agent hosts require separate product decisions. They are not scaffolding
points in this implementation.
