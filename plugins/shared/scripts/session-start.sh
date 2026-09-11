#!/usr/bin/env sh
# SessionStart emits fixed policy plus one fresh, authorized context load.
# It never reads stdin, passes a credential in argv or blocks host startup.
set -u
plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cat "$plugin_root/activation.txt" 2>/dev/null || exit 0
if [ -n "${ACH_MEMORY_API_KEY:-}" ] && command -v uvx >/dev/null 2>&1; then
  printf '\n'
  uvx --from git+https://github.com/ackstorm/ach-memory@v0.7.0 \
    ach-memory context load 2>/dev/null || true
fi
exit 0
