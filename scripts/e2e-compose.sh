#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

run_id="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
project="ach-memory-e2e-${run_id}"
compose=(docker compose -f "$ROOT/docker-compose.yml" -p "$project")
stack_started=0

cleanup() {
    status=$?
    cleanup_status=0
    trap - EXIT INT TERM
    if (( stack_started )); then
        "${compose[@]}" down -v --remove-orphans || cleanup_status=$?
    fi
    if (( status == 0 && cleanup_status != 0 )); then
        status=$cleanup_status
    fi
    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Port 0 asks Docker to choose a free host port. Keeping the 127.0.0.1 bind in
# docker-compose.yml means the isolated services never become externally
# reachable, while Docker itself avoids races with the normal development
# stack or another E2E run.
export MEMORY_POSTGRES_PORT=0
export MEMORY_HINDSIGHT_PORT=0
export MEMORY_API_PORT=0
export MEMORY_MCP_ALLOWED_HOSTS="127.0.0.1:*,localhost:*"

# The full E2E exercises the real Hindsight server, worker and persistence,
# but its built-in MockLLM performs extraction/consolidation/reflect without
# any external request. Override ambient real-provider settings unconditionally
# so this gate cannot consume a developer or CI credential by accident. The
# guarded base/key settings remain local, inert placeholders for Compose.
export HINDSIGHT_LLM_PROVIDER=mock
export HINDSIGHT_LLM_MODEL=mock-model
export HINDSIGHT_LLM_BASE_URL=http://127.0.0.1:9
export HINDSIGHT_LLM_API_KEY=e2e-mock-not-a-secret

# The stack owns a fresh credential. Only its hash reaches the API container;
# neither value is printed or passed as a command-line argument.
MEMORY_MASTER_KEY="mem_e2e_$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export MEMORY_MASTER_KEY
MEMORY_MASTER_KEY_HASH="$(python3 -c \
    'import hashlib, os; print(hashlib.sha256(os.environ["MEMORY_MASTER_KEY"].encode()).hexdigest())')"
export MEMORY_MASTER_KEY_HASH

stack_started=1
"${compose[@]}" up -d --build --wait

ready_deadline=$((SECONDS + 60))
until "${compose[@]}" exec -T api python -c \
    'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/docs", timeout=2).read(1)' \
    >/dev/null 2>&1; do
    if (( SECONDS >= ready_deadline )); then
        echo "FAIL: the isolated API did not become ready within 60 seconds" >&2
        exit 1
    fi
    sleep 1
done

api_address="$("${compose[@]}" port api 8000)"
hindsight_address="$("${compose[@]}" port hindsight 8888)"

export API="http://${api_address}"
export HINDSIGHT_URL="http://${hindsight_address}"
uv run python scripts/e2e.py
