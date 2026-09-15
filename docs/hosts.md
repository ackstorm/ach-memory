# Installing ach-memory on a host

Four supported hosts: Claude Code, Codex, opencode and pi. One command installs
the plugin (skill, session-start context, MCP stdio proxy) into any of them;
re-running it is the update path.

```bash
uvx --from git+https://github.com/ackstorm/ach-memory@v0.3.1 ach-memory init all   # or: claude | codex | opencode | pi
```

What it does per host:

| host | mechanism |
|---|---|
| claude | `claude plugin marketplace add ackstorm/ach-memory` + `claude plugin install ach-memory@ach-memory` (update always runs) |
| codex | `codex plugin marketplace add ackstorm/ach-memory` + `codex plugin add ach-memory@ach-memory` (`marketplace upgrade` + re-add always run) |
| opencode | copies `plugins/ach-memory.js` + skill into `$XDG_CONFIG_HOME/opencode`, adds `mcp.ach-memory`, `plugin[]` and `skills.paths[]` to `opencode.json` |
| pi | copies `extensions/ach-memory.js` + skill into `~/.pi/agent`, adds `mcpServers.ach-memory` to `mcp.json`, installs `npm:pi-mcp-adapter` if missing |

`all` installs for every host whose CLI is on `PATH`. `--local` (opencode, pi)
points the MCP entry at this environment's `ach-memory` script instead of the
released `uvx` package, to test unreleased proxy code. The API key is never
written: configs reference `ACH_MEMORY_API_KEY` by name.

## Required environment

Set these in your shell profile so every agent process inherits them:

```bash
export ACH_MEMORY_URL=https://api.<domain>/memory/mcp/
export ACH_MEMORY_API_KEY=<token from your identity provider>
# export ACH_MEMORY_HEADER=Authorization   # only if a proxy blocks the default header
```

`ACH_MEMORY_URL` is the MCP endpoint, used verbatim by both the stdio proxy
and `ach-memory context load`. `ACH_MEMORY_API_KEY` is whatever credential
your identity provider issues; the service mints none.

## What the hooks do

- `session-start.sh` (SessionStart) — prints the activation policy, then
  loads bounded standing context for the resolved project/user scope.
- `subagent-start.sh` (SubagentStart) — announces the ach-memory skill to a
  spawned subagent; makes no network call.
- `pre-compact.sh` (PreCompact, Claude Code only) — nudges the agent to
  retain durable claims before context is compacted.
- opencode / pi have no hook system: their `ach-memory.js` adapter runs the
  same `session-start.sh` once at load and appends its output to the system prompt.

## Verify

```bash
claude mcp list          # ach-memory should show as connected
```

Then ask the agent to recall something (or run
`uvx --from git+https://github.com/ackstorm/ach-memory@v0.3.1 ach-memory context load`
directly) and confirm it returns without error.
