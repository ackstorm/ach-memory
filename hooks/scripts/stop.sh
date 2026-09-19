#!/usr/bin/env sh
# Stop (Claude Code, Codex): a Stop hook's stdout never reaches the model, so
# the nudge travels as a blocking decision, which makes the agent take one more
# turn. retain-nudge.sh throttles that; this only guards the loop and wraps.
#
# No jq: the input fields are flat strings and the nudge text carries no quote
# or backslash (tests/test_plugins.py parses the output to keep it that way).
set -u
input=$(cat)
# Already continuing because of this hook: let the turn end.
printf '%s' "$input" | grep -q '"stop_hook_active" *: *true' && exit 0
sid=$(printf '%s' "$input" | sed -n 's/.*"session_id" *: *"\([^"]*\)".*/\1/p')
nudge=$("$(dirname -- "$0")/retain-nudge.sh" "${sid:-unknown}")
[ -n "$nudge" ] || exit 0
printf '{"decision": "block", "reason": "%s"}\n' "$nudge"
