#!/usr/bin/env sh
# PreCompact: prefer the live nudge from the installed plugin, but never leave
# the hook silent if uvx (or the network) is unavailable.
#
# Not `exec`: `exec cmd || fallback` only reaches the fallback when the shell
# cannot launch `cmd` at all. uvx launching and then failing would print nothing.
set -u
plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
nudge=$(uvx --from "$plugin_root" ach-memory hook pre-compact 2>/dev/null) || nudge=
[ -n "$nudge" ] || nudge='Before context is compacted, retain any durable decision, constraint, convention, fact or verified gotcha that is not yet in ach-memory. Do not retain the transcript or a generic session summary.'
printf '%s\n' "$nudge"
