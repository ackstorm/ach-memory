#!/usr/bin/env sh
# PreCompact: prefer the live nudge from the installed package, but never
# leave the hook silent if uvx (or the network) is unavailable.
exec uvx --from git+https://github.com/ackstorm/ach-memory@v0.1.2 \
  ach-memory hook pre-compact 2>/dev/null || printf '%s\n' 'Before context is compacted, retain any durable decision, constraint, convention, fact or verified gotcha that is not yet in ach-memory. If project work is incomplete, update Working State. Do not retain the transcript or a generic session summary.'
