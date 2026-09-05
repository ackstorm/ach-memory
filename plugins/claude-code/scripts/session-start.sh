#!/usr/bin/env sh
set -u
key="${ACH_MEMORY_API_KEY:-}"
[ -n "$key" ] || exit 0
url="${ACH_MEMORY_URL:-http://localhost:8000}"
project="${MEMORY_PROJECT:-}"
locator=""
if [ -z "$project" ] && command -v git >/dev/null 2>&1; then
  locator="$(git remote get-url origin 2>/dev/null || true)"
  locator="$(printf '%s' "$locator" | sed -E 's#^[^@]+@#git@#; s#https?://[^/@]+@#https://#; s#\.git$##; s#/$##')"
  if [ -n "$locator" ]; then
    project="$(printf '%s' "$locator" | sed -E 's#^[^:/]+://##; s#^[^@]+@##; s#^[^:]+:##; s#\.git$##; s#/$##; s#[/:]#-#g')"
  fi
fi
workspace=""
if command -v git >/dev/null 2>&1 && root=$(git rev-parse --show-toplevel 2>/dev/null); then
  digest=$(printf '%s' "$root" | sha256sum | cut -c1-32)
  workspace="ws_$digest"
fi
body="{\"project_slug\":\"$project\",\"git_locator\":\"$locator\",\"workspace_id\":\"$workspace\"}"
curl -sf --max-time 3 -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
  -d "$body" "$url/v1/context/load" 2>/dev/null || true
exit 0
