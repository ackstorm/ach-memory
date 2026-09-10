#!/usr/bin/env bash
# End-to-end smoke: name two identities, retain a fact, recall it.
# Requires the compose stack to be up and migrated.
set -euo pipefail

API="${API:-http://localhost:8000}"

# Bounded wait with an explicit failure path. Never `until ...; do sleep; done`:
# if the target never appears, that loop hangs forever with no signal.
for _ in $(seq 1 30); do
  curl -sf "${API}/docs" >/dev/null && break
  sleep 2
done
curl -sf "${API}/docs" >/dev/null || { echo "FAIL: API never came up at ${API}" >&2; exit 1; }

# A fresh project slug per run. The script mints a NEW user every time, and a
# project belongs to whoever first touched its slug -- so a fixed slug means
# run 2 is a different user asking for run 1's project, which is correctly a
# 403. This only ever looked idempotent because `pytest` shared this database
# and kept dropping its tables; isolating the test database exposed it.
project_slug="smoke-project-$(date +%s)-$$"

# The token IS the identity: the stack authenticates through an external
# provider (`deploy/dev-identity/whoami.py` echoes the bearer token back as
# the user id), so there is nothing to mint here and no master credential to
# mint it with. Two tokens are two people with two banks.
#
# POST /v1/bootstrap still matters: `link_identity` deliberately does not
# provision a bank -- that would put a Hindsight round trip on the
# authentication path of every request -- so a brand-new identity has a
# `users` row and an unusable bank until this call.
user_key="smoke-user-$(date +%s)-$$"
curl -sf -X POST "${API}/v1/bootstrap" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{}' >/dev/null
echo "named and provisioned identity: ${user_key}"

# sync_retain, not retain: extraction must finish before we can recall.
curl -sf -X POST "${API}/v1/memory/sync_retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","content":"This project pins its Python dependencies with uv, never with pip."}' \
  >/dev/null
echo "retained one fact"

recalled=$(curl -sf -X POST "${API}/v1/memory/recall" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","query":"how are Python dependencies managed here"}')

echo "${recalled}" | grep -qi "uv" \
  || { echo "FAIL: recall did not return the retained fact" >&2; echo "${recalled}" >&2; exit 1; }
echo "${recalled}" | grep -q "bank_id" \
  && { echo "FAIL: bank_id leaked to the client" >&2; exit 1; }

# A second user must not see the first user's memory.
other_key="smoke-other-$(date +%s)-$$"
curl -sf -X POST "${API}/v1/bootstrap" \
  -H "Authorization: Bearer ${other_key}" -H 'Content-Type: application/json' \
  -d '{}' >/dev/null

cross=$(curl -sf -X POST "${API}/v1/memory/recall" \
  -H "Authorization: Bearer ${other_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","query":"how are Python dependencies managed here"}')
echo "${cross}" | grep -qi "uv" \
  && { echo "FAIL: a second user reached the first user's memory" >&2; exit 1; }

# Project memory: shared where authorized, denied where not.
curl -sf -X POST "${API}/v1/memory/sync_retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"project","project_slug":"'"${project_slug}"'","content":"Migrations in this project run with alembic upgrade head."}' \
  >/dev/null
echo "retained one project fact"

proj=$(curl -sf -X POST "${API}/v1/memory/recall" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"project","project_slug":"'"${project_slug}"'","query":"how do migrations run"}')
echo "${proj}" | grep -qi "alembic" \
  || { echo "FAIL: project recall did not return the retained fact" >&2; exit 1; }

denied=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${API}/v1/memory/recall" \
  -H "Authorization: Bearer ${other_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"project","project_slug":"'"${project_slug}"'","query":"how do migrations run"}')
[ "${denied}" = "403" ] \
  || { echo "FAIL: a stranger reached the project, got HTTP ${denied}" >&2; exit 1; }
echo "project memory is owner-scoped"

# Curation against real Hindsight: retain synchronously, find the memory,
# invalidate it, confirm it leaves the active set, restore it.
runbook_fact='The deploy runbook lives in docs/runbooks/deploy.md.'
runbook_marker='docs/runbooks/deploy.md'
curl -sf -X POST "${API}/v1/memory/sync_retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","content":"'"${runbook_fact}"'"}' \
  >/dev/null

listed=$(curl -sf -X POST "${API}/v1/memory/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","limit":50}')
if ! mem_id=$(printf '%s' "${listed}" | python3 "$(dirname "$0")/select_curatable_memory.py" "${runbook_marker}"); then
  echo "FAIL: no matching curatable runbook memory after sync_retain" >&2
  exit 1
fi
echo "listed a memory: ${mem_id}"

curl -sf -X POST "${API}/v1/memory/forget" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d "{\"scope\":\"user\",\"memory_id\":\"${mem_id}\",\"reason\":\"smoke\"}" >/dev/null

after=$(curl -sf -X POST "${API}/v1/memory/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","limit":50}')
echo "${after}" | grep -q "${mem_id}" \
  && { echo "FAIL: forget left the memory in the active set" >&2; exit 1; }
echo "forget retired it from the active set"

curl -sf -X POST "${API}/v1/memory/restore" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d "{\"scope\":\"user\",\"memory_id\":\"${mem_id}\"}" >/dev/null
restored=$(curl -sf -X POST "${API}/v1/memory/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","limit":50}')
if ! printf '%s' "${restored}" | python3 "$(dirname "$0")/verify_active_memory.py" "${mem_id}"; then
  echo "FAIL: restore did not return the memory to the active set" >&2
  exit 1
fi
echo "restore brought it back"

# reflect, which is a different Hindsight endpoint from recall. Bounded
# retry, not a naked poll loop: reflect can run moments after sync_retain
# before Hindsight's post-write consolidation has finished and answer "no
# information" even though the fact is already searchable via recall
# (scripts/e2e.py's reflect_with_retry hits the same race).
reflect_attempts=4
reflect_ok=0
for _ in $(seq 1 "${reflect_attempts}"); do
  reflected=$(curl -sf -X POST "${API}/v1/memory/reflect" \
    -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
    -d '{"scope":"user","query":"where is the deploy runbook"}')
  echo "${reflected}" | grep -qi "runbook" && { reflect_ok=1; break; }
  sleep 3
done
[ "${reflect_ok}" = "1" ] \
  || { echo "FAIL: reflect did not use the retained fact after ${reflect_attempts} attempts" >&2; echo "${reflected}" >&2; exit 1; }
echo "reflect answered from memory"

# SPEC §24 scenario P, the async retain lifecycle: retain (not sync_retain)
# returns an operation_id, and the caller may follow up with get_operation.
# Nothing before this chained the two together -- retain's operation_id and
# operations/get's acceptance of one were each tested in isolation, but the
# sequence itself was never exercised against the real thing.
op_retain=$(curl -sf -X POST "${API}/v1/memory/retain" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user","content":"Scenario P: the async retain lifecycle."}')
operation_id=$(echo "${op_retain}" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["result"]["operation_id"])')
[ -n "${operation_id}" ] \
  || { echo "FAIL: retain did not return an operation_id" >&2; echo "${op_retain}" >&2; exit 1; }
echo "async retain returned operation: ${operation_id}"

op_got=$(curl -sf -X POST "${API}/v1/memory/operations/get" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d "{\"scope\":\"user\",\"operation_id\":\"${operation_id}\"}")
echo "${op_got}" | grep -q "${operation_id}" \
  || { echo "FAIL: operations/get did not recognize retain's operation_id" >&2; echo "${op_got}" >&2; exit 1; }
echo "operations/get resolved the operation retain returned"

# documents/list and operations/list: neither was covered by any call in this
# script before, so a leak on either surface had nothing here to catch it.
docs_listed=$(curl -sf -X POST "${API}/v1/memory/documents/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user"}')
echo "${docs_listed}" | python3 -c 'import json,sys; assert json.load(sys.stdin)["result"]["items"]' \
  || { echo "FAIL: documents/list returned nothing after sync_retain" >&2; echo "${docs_listed}" >&2; exit 1; }
echo "documents/list saw the retained document"

ops_listed=$(curl -sf -X POST "${API}/v1/memory/operations/list" \
  -H "Authorization: Bearer ${user_key}" -H 'Content-Type: application/json' \
  -d '{"scope":"user"}')
echo "${ops_listed}" | python3 -c 'import json,sys; assert json.load(sys.stdin)["result"]["operations"]' \
  || { echo "FAIL: operations/list returned nothing after sync_retain" >&2; echo "${ops_listed}" >&2; exit 1; }
echo "operations/list saw the retain's operation"

# No bank id anywhere in any of it. Uses scripts/leakscan.py, the same pattern
# e2e.py and mcp-smoke.py use, so the three cannot drift: this loop's inline
# regex and e2e.py's disagreed on whether an embedded bank id counts (it does)
# and on whether prj_ counts (it does).
for body in "${recalled}" "${cross}" "${proj}" "${listed}" "${after}" \
            "${restored}" "${reflected}" "${docs_listed}" "${ops_listed}" "${op_got}"; do
  echo "${body}" | python3 "$(dirname "$0")/leakscan.py" \
    || { echo "FAIL: leak scan rejected a response body" >&2; exit 1; }
done
echo "no bank_id in any response this script collected"

echo "PASS: user and project memory, curation, reflect, isolated, no bank_id leak"
