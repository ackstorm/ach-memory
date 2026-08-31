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

# ws_ + first 32 hex chars of SHA-256 over the canonical absolute worktree
# root -- never the raw path, which must not cross the network or land in a
# cache filename. Fails open (empty) on missing git or a non-worktree cwd:
# Working State is simply omitted rather than guessed.
workspace_id=""
if worktree_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  canonical_root="$(cd "$worktree_root" 2>/dev/null && pwd -P)"
  if [ -n "$canonical_root" ]; then
    workspace_id="ws_$(printf '%s' "$canonical_root" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-32)"
  fi
fi

# Keyed on service and repository, never on the credential: a filename is
# observable metadata. workspace_id joins the digest only when resolved, so
# the existing no-workspace cache file stays readable unchanged and two git
# worktrees of the same repository never share one.
digest_key="$url|$locator"
[ -n "$workspace_id" ] && digest_key="$digest_key|$workspace_id"
digest="$(printf '%s' "$digest_key" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-16)"
cache="$cache_dir/full-$digest.txt"
tmp="$cache.$$"
cache_tmp="$cache.cache.$$"
# The filename deliberately names only the service and repository. The
# private record still binds its content to the current API key, so switching
# identities under one Unix account cannot replay another user's brief.
owner="$(printf '%s' "$key" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -d ' ' -f1)"

show_cache() {
  [ -s "$cache" ] || return 1
  [ "$(head -n 1 "$cache" 2>/dev/null || true)" = "$owner" ] || return 1
  second="$(sed -n '2p' "$cache" 2>/dev/null || true)"
  case "$second" in
    ''|*[!0-9]*) start=2; stored="$(date -r "$cache" '+%s' 2>/dev/null || printf '0')" ;;
    *) start=3; stored="$second" ;;
  esac
  now="$(date '+%s' 2>/dev/null || printf '0')"
  age=$((now > stored ? now - stored : 0))
  [ "$age" -le 9999999999 ] || age=9999999999
  padded="$(printf '%010d' "$age")"
  if tail -n +"$start" "$cache" | grep -Eq 'cache-age [0-9]{10}s'; then
    tail -n +"$start" "$cache" | sed -E "s/cache-age [0-9]{10}s/cache-age ${padded}s/"
  else
    printf '[ach-memory] cached Full tier; age unknown\n'
    tail -n +"$start" "$cache" 2>/dev/null
  fi
}

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
if [ -n "$workspace_id" ]; then
  curl_args+=(--data-urlencode "workspace_id=$workspace_id")
fi

if curl "${curl_args[@]}" 2>/dev/null; then
  cat "$tmp" 2>/dev/null || true
  stored_at="$(date '+%s' 2>/dev/null || printf '0')"
  { printf '%s\n' "$owner"; printf '%s\n' "$stored_at"; cat "$tmp"; } > "$cache_tmp" 2>/dev/null && \
    mv -f "$cache_tmp" "$cache" 2>/dev/null || true
  chmod 0600 "$cache" 2>/dev/null || true
  rm -f "$tmp" "$cache_tmp" 2>/dev/null || true
else
  rm -f "$tmp" "$cache_tmp" 2>/dev/null || true
  if show_cache >/dev/null; then
    printf '[ach-memory] service unreachable; cached brief from %s\n' \
      "$(date -r "$cache" '+%Y-%m-%d %H:%M' 2>/dev/null || echo unknown)"
    show_cache || true
  else
    printf '%s\n' \
      '[ach-memory] Full tier unavailable; this session has only the MCP Index if the MCP server started.'
  fi
fi

exit 0
