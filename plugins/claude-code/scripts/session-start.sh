#!/usr/bin/env sh
set -u
key="${ACH_MEMORY_API_KEY:-}"
[ -n "$key" ] || exit 0
url="${ACH_MEMORY_URL:-http://localhost:8000}"
curl -sf --max-time 3 -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
  -d '{}' "$url/v1/context/load" 2>/dev/null || true
exit 0
