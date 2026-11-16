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
if [ -n "${ACH_MEMORY_API_KEY:-}${ACH_MEMORY_TOKEN_COMMAND:-}" ] && command -v uvx >/dev/null 2>&1 \
  && uvx --from "$plugin_root" ach-memory context load 2>/dev/null; then
  exit 0
fi
echo "ach-memory is not reachable in this session: ACH_MEMORY_URL or a credential is missing from" \
  "the environment, or the service did not answer. The session start announced above did not load and its tools will fail." \
  "Tell the user once."
exit 0
