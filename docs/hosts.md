# Installing ach-memory on a host

Two supported hosts: Claude Code and Codex. Both install the same committed
plugin (skill, hooks, MCP stdio proxy config) straight from this repository;
no separate installer.

## Claude Code

```bash
claude plugin marketplace add ackstorm/ach-memory
claude plugin install ach-memory@ach-memory
```

## Codex

```bash
codex plugin marketplace add ackstorm/ach-memory
codex plugin add ach-memory@ach-memory
```

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

## Verify

```bash
claude mcp list          # ach-memory should show as connected
```

Then ask the agent to recall something (or run
`uvx --from git+https://github.com/ackstorm/ach-memory@v0.2.0 ach-memory context load`
directly) and confirm it returns without error.
