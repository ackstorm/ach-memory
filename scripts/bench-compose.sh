#!/usr/bin/env bash
# Bring up an isolated stack and run a benchmark script against BOTH arms:
# ach-memory (`api`) and the unmodified engine underneath it (`hindsight`).
#
#   scripts/bench-compose.sh scripts/bench.py            # MockLLM, deterministic
#   BENCH_REAL_LLM=1 scripts/bench-compose.sh scripts/bench_quality.py
#
# Structured after scripts/e2e-compose.sh, which is the tested pattern for
# this stack; the one real difference is BENCH_REAL_LLM.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET="${1:?usage: bench-compose.sh <python script>}"

run_id="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
project="ach-memory-bench-${run_id}"
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

export MEMORY_POSTGRES_PORT=0
export MEMORY_HINDSIGHT_PORT=0
export MEMORY_API_PORT=0
export MEMORY_MCP_ALLOWED_HOSTS="127.0.0.1:*,localhost:*"

if [[ "${BENCH_REAL_LLM:-0}" == "1" ]]; then
    # Retrieval quality cannot be measured on MockLLM: extraction and reflect
    # would be synthetic, so every number would describe the mock, not the
    # engine. Require the real wiring explicitly rather than falling back to
    # a developer's ambient OPENAI_* variables -- docker-compose.yml records
    # at length how that produced green runs that meant nothing.
    : "${HINDSIGHT_LLM_BASE_URL:?BENCH_REAL_LLM=1 needs HINDSIGHT_LLM_BASE_URL}"
    : "${HINDSIGHT_LLM_API_KEY:?BENCH_REAL_LLM=1 needs HINDSIGHT_LLM_API_KEY}"
    # Two models on purpose: retain wants message content, reflect wants a
    # tool call, and no single available model does both (docker-compose.yml).
    export HINDSIGHT_LLM_MODEL="${HINDSIGHT_LLM_MODEL:-bedrock.openai.gpt-oss-20b-1-0}"
    export HINDSIGHT_REFLECT_LLM_MODEL="${HINDSIGHT_REFLECT_LLM_MODEL:-gemini.gemini-3.7-flash}"
    echo "bench: REAL LLM stack (retain=${HINDSIGHT_LLM_MODEL}, reflect=${HINDSIGHT_REFLECT_LLM_MODEL})"
else
    export HINDSIGHT_LLM_PROVIDER=mock
    export HINDSIGHT_LLM_MODEL=mock-model
    export HINDSIGHT_REFLECT_LLM_MODEL=mock-model
    export HINDSIGHT_LLM_BASE_URL=http://127.0.0.1:9
    export HINDSIGHT_LLM_API_KEY=bench-mock-not-a-secret
    echo "bench: MockLLM stack (deterministic, no external model calls)"
fi

MEMORY_MASTER_KEY="mem_bench_$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
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

API="http://$("${compose[@]}" port api 8000)"
HINDSIGHT_URL="http://$("${compose[@]}" port hindsight 8888)"
export API HINDSIGHT_URL

uv run python "$TARGET"
