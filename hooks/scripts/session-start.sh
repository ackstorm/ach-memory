#!/usr/bin/env sh
# SessionStart emits fixed policy plus one fresh, authorized context load.
# It never reads stdin, passes a credential in argv or blocks host startup.
#
# The plugin root is a checkout of this repository at the installed version, so
# `uvx --from "$plugin_root"` always runs the proxy that matches the plugin. A
# pinned git source would freeze into the host config at install time instead.
set -u
plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cat "$plugin_root/hooks/activation.txt" 2>/dev/null || exit 0
if [ -n "${ACH_MEMORY_API_KEY:-}" ] && command -v uvx >/dev/null 2>&1; then
  printf '\n'
  uvx --from "$plugin_root" ach-memory context load 2>/dev/null || true
fi
exit 0
