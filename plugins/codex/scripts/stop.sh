#!/usr/bin/env bash
# ach-memory - Stop hook.
#
# Fires when the agent finishes responding, which is the only moment a durable
# fact from this turn actually exists yet. Stop discards plain stdout, so the
# text ships as a JSON envelope.
#
# Never exit non-zero here: exit code 2 on Stop means "do not stop" and would
# put the agent in a loop it cannot leave.
set -u
cat "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/retain-hint.json" 2>/dev/null || true
exit 0
