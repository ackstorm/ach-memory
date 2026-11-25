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
printf '\n'
if command -v uvx >/dev/null 2>&1 \
  && uvx --from "$plugin_root" python -m memory.cli context load 2>/dev/null; then
  exit 0
fi
echo "ach-memory is not reachable in this session: the service did not answer, or no credential reached" \
  "it (set ACH_MEMORY_URL and a credential, or sign in with ach-cli login). The session start announced above did not load and its tools will fail." \
  "Tell the user once."
exit 0
