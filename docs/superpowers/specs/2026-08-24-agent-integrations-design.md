# Conservative Agent Integrations

## Goal

Make ach-memory an always-available, explicitly used capability in Codex,
Claude Code, OpenCode, and Pi. A new session and every subagent must receive a
short memory protocol without relying on semantic skill selection.

Installation must be one idempotent command:

```text
ach-memory init codex|claude|opencode|pi|all
```

The integration configures the existing HTTP MCP endpoint and references
`ACH_MEMORY_API_KEY`; it never copies or persists the credential itself.

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
remote `ach-memory` MCP entry to the global OpenCode configuration if that name
is not already configured.

The MCP entry targets the existing local endpoint,
`http://localhost:8000/mcp/`, and resolves `ACH_MEMORY_API_KEY` from the
environment. Existing same-name configuration is preserved and reported
rather than overwritten.

### Pi

`ach-memory init pi` installs `pi-mcp-adapter` through Pi's package manager,
installs the skill and a small Pi extension, and adds an `ach-memory` server to
Pi's global MCP configuration when absent. The server uses Streamable HTTP and
`bearerTokenEnv: "ACH_MEMORY_API_KEY"`.

The Pi extension injects the activation text through `before_agent_start`.
It performs no HTTP requests and registers no duplicate memory tools; MCP
remains owned by `pi-mcp-adapter`.

## Installer

The existing Python package exposes a small standard-library CLI. `init`
requires one target; `all` means exactly Codex, Claude Code, OpenCode, and Pi.

Before changing files, the installer resolves the user configuration directory
and checks that every requested agent executable exists. An `all` preflight
failure makes no changes. Explicit single-agent setup fails clearly when its
agent is unavailable.

The installer then:

1. invokes native plugin/package installation where the host provides it;
2. copies only ach-memory-owned adapter and skill files where no portable
   plugin installation exists;
3. merges the MCP entry into JSON configuration without replacing unrelated
   keys or an existing `ach-memory` entry;
4. reports files and registrations changed, plus the required agent restart.

Re-running the same command produces the same effective configuration. Owned
adapter files may be refreshed; user-owned configuration and credentials are
not overwritten. No `--force`, interactive wizard, auto-detection, or setup
state database is included in v1.

## Configuration and security

- `ACH_MEMORY_API_KEY` is referenced by name and never written into generated
  files, command arguments, logs, or hook output.
- Generated HTTP MCP configuration uses `http://localhost:8000/mcp/`, matching
  the existing Docker Compose and Codex plugin contract.
- Hooks contain no secrets and make no network calls.
- Existing agent configuration is parsed before modification. Invalid JSON or
  unsupported structure fails without replacing the file.

## Failure behavior

Activation hooks fail open and remain silent on failure. MCP connection or
authentication failures surface only when the agent explicitly tries a memory
tool. This keeps an unavailable memory service from blocking coding work.

Installation failures are explicit and non-zero. Each configuration write is
atomic. The installer preflights `all`, but it does not attempt cross-file
rollback after an unexpected filesystem or native-agent failure; its
idempotency makes re-running the recovery path.

## Verification

Automated checks cover:

- installer idempotency in isolated temporary homes;
- preservation of unrelated and same-name user configuration;
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
prompt ingestion, service auto-start, configurable remote endpoints,
status-bar integrations, and additional agent hosts require separate product
decisions. They are not scaffolding points in this implementation.
