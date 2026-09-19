#!/usr/bin/env sh
# Periodic retain nudge, throttled per session: prints the nudge text at most
# once every ACH_MEMORY_NUDGE_INTERVAL seconds (default 900) for the session id
# given as $1, and nothing otherwise. Every host's "turn finished" hook calls
# this and injects whatever comes back; the throttle lives here and nowhere else.
#
# Same uvx-with-fallback shape as pre-compact.sh: the text lives in the package,
# but a missing uvx must not silence the hook.
set -u
sid=${1:-unknown}
stamp="${TMPDIR:-/tmp}/ach-nudge-$sid"
now=$(date +%s)
last=$(cat "$stamp" 2>/dev/null || echo 0)
[ $((now - last)) -lt "${ACH_MEMORY_NUDGE_INTERVAL:-900}" ] && exit 0
printf '%s' "$now" > "$stamp"

plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
nudge=$(uvx --from "$plugin_root" ach-memory hook retain-nudge 2>/dev/null) || nudge=
[ -n "$nudge" ] || nudge='Before this turn ends, check ach-memory for anything durable that is not stored yet. Retain ONLY what a future session would need: a decision with its rationale, a constraint, a convention, a verified gotcha, a fact not cheaply rediscoverable from the repo, or a dated landmark that lets someone reconstruct what changed and when. Do NOT retain session progress, transcripts, summaries, plans, options considered, things you tried, or anything the code and git history already say. One claim per retain call, in English. If nothing qualifies - the usual case - retain nothing, say nothing, and stop.'
printf '%s\n' "$nudge"
