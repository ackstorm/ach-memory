#!/usr/bin/env bash
# ach-memory - Stop/PreCompact hook: silent transcript checkpoint.
#
# Stdout becomes hook feedback Claude can act on -- Stop hook output can
# re-enter the agent loop (SPEC Phase 3 non-negotiable contract: this
# capture path is plumbing, never a decision surface, and only SessionStart
# may emit context). Every stream is redirected here and the command always
# exits 0, whatever happens inside.
#
# The hook JSON Claude Code writes to this script's stdin is passed straight
# through to `ach-memory capture-checkpoint`, unmodified.
set -u

key="${ACH_MEMORY_API_KEY:-}"
[ -n "$key" ] || exit 0

# Same release-pinned uvx source as .mcp.json: one place names the version,
# so a hook checkpointing against a different ach-memory build than the MCP
# server is not something an upgrade can silently introduce.
if command -v timeout >/dev/null 2>&1; then
  timeout 8 uvx --from git+https://github.com/ackstorm/ach-memory@v0.3.5 \
    ach-memory capture-checkpoint >/dev/null 2>&1
else
  uvx --from git+https://github.com/ackstorm/ach-memory@v0.3.5 \
    ach-memory capture-checkpoint >/dev/null 2>&1
fi

exit 0
