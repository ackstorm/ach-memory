#!/usr/bin/env sh
# Idle retain nudge for the host's "user submitted a prompt" event, printed at
# most once per ACH_MEMORY_IDLE_INTERVAL seconds (default 1800) per session
# ($1), and only when nothing has been retained for this checkout in that long.
# The stdio proxy stamps every successful retain (memory.mcp.proxy.retain_stamp,
# same key: $PWD with slashes as underscores, under the ach-memory cache dir --
# not $TMPDIR, which differs per host), so an agent that saves on its own never
# sees this. The only retain nudge: Stop cannot reach the model without a
# blocking decision, which the host reports as a hook error.
set -u
# Session id as $1 (opencode, pi) or from the host's JSON on stdin (Claude Code, Codex).
sid=${1:-}
[ -n "$sid" ] || sid=$(sed -n 's/.*"session_id" *: *"\([^"]*\)".*/\1/p' | head -n 1)
sid=${sid:-unknown}
interval=${ACH_MEMORY_IDLE_INTERVAL:-1800}
cache="${XDG_CACHE_HOME:-$HOME/.cache}/ach-memory"
mkdir -p "$cache" 2>/dev/null || exit 0
now=$(date +%s)

retain="$cache/retain-$(printf '%s' "$PWD" | tr '/' '_')"
# No stamp yet: start the clock here rather than nag a fresh install on its first prompt.
[ -f "$retain" ] || printf '%s' "$now" > "$retain"
[ $((now - $(cat "$retain" 2>/dev/null || echo 0))) -lt "$interval" ] && exit 0

sent="$cache/idle-$sid"
[ $((now - $(cat "$sent" 2>/dev/null || echo 0))) -lt "$interval" ] && exit 0
printf '%s' "$now" > "$sent"

plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
nudge=$(uvx --from "$plugin_root" ach-memory hook idle-nudge 2>/dev/null) || nudge=
[ -n "$nudge" ] || nudge='Nothing has been retained in ach-memory for over 30 minutes. If a durable decision with its rationale, a constraint, a convention, a verified gotcha, or a dated landmark has landed in that time, retain it now - one claim per call, in English. If nothing has, carry on; do not retain session progress or summaries to fill the gap. Internal nudge: act on it, say nothing about it.'
printf '%s\n' "$nudge"
