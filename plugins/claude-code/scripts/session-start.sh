#!/usr/bin/env bash
# ach-memory - SessionStart hook.
#
# Stdout becomes session context. The full brief can only arrive through this
# uncapped channel, so the network dependency is bounded with a timeout and a
# last-good cache. The hook must exit 0: a non-zero hook blocks the user's
# message.
set -u

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cat "$root/activation.txt" 2>/dev/null || true

key="${ACH_MEMORY_API_KEY:-}"
[ -n "$key" ] || exit 0
url="${ACH_MEMORY_URL:-http://localhost:8000}"

locator="$(git remote get-url origin 2>/dev/null || true)"
cache_dir="${ACH_MEMORY_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME:-/tmp}/.cache}/ach-memory}"
mkdir -p "$cache_dir" 2>/dev/null || true
# Keyed on service and repository, never on the credential: a filename is
# observable metadata.
digest="$(printf '%s|%s' "$url" "$locator" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-16)"
cache="$cache_dir/full-$digest.txt"
tmp="$cache.$$"

# format=text keeps this a curl and a cat: no jq, node, or extra runtime is
# needed before memory can orient a session. -f prevents an error envelope
# from becoming fake memory in the agent context.
curl_args=(
  -sf --max-time 3 -o "$tmp"
  -H "Authorization: Bearer $key"
  -G "$url/v1/session-brief"
  --data-urlencode "scope=user"
  --data-urlencode "tier=full"
  --data-urlencode "host=claude-code"
  --data-urlencode "format=text"
)
if [ -n "$locator" ]; then
  curl_args+=(--data-urlencode "git_locator=$locator")
fi

if curl "${curl_args[@]}" 2>/dev/null; then
  mv -f "$tmp" "$cache" 2>/dev/null || true
  chmod 0600 "$cache" 2>/dev/null || true
  cat "$cache" 2>/dev/null || true
else
  rm -f "$tmp" 2>/dev/null || true
  if [ -s "$cache" ]; then
    printf '[ach-memory] service unreachable; cached brief from %s\n' \
      "$(date -r "$cache" '+%Y-%m-%d %H:%M' 2>/dev/null || echo unknown)"
    cat "$cache" 2>/dev/null || true
  fi
fi

exit 0
