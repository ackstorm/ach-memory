# Plan 6 — Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 2 critical and 9 important findings from the 2026-08-22
whole-codebase review, and land the bank-configuration decision that makes
`update_mode="append"` work.

**Architecture:** The decision driving Task 1 is the user's: stop relying on
Hindsight's Memory Defense (content screening moves to a LiteLLM
`pre_mcp_call` guardrail, outside this repo) and leave `store_document_text`
at Hindsight's default of `True`. Both fields `ensure_bank` used to PATCH are
therefore gone, which deletes the config PATCH, the `_materialized` TTL cache
built to skip it, and the whole per-process-cache finding (I3) with them. The
remaining tasks are independent fixes; only Task 2 depends on Task 1.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sync SQLAlchemy 2.0,
psycopg 3, sync httpx, `mcp==2.0.0`, pytest + respx, `uv`.

## Global Constraints

- All output in English: code, comments, docs, commits.
- Dependency management is `uv` only. Never `pip install`. Never run
  `uv add` without checking `pyproject.toml`'s existing groups first — a
  production import declared in a dev group ships a container that cannot
  start (this happened; see `docs/PROJECT-STATE.md`).
- Tests run with `uv run pytest -m "not integration"`. Integration tests
  (`-m integration`) need the live stack: `docker compose up -d`.
- Never run a naked polling loop. Every wait needs an upper bound and an
  explicit failure path.
- `ruff check src tests scripts` must be clean before every commit.
- Error codes are a CLOSED list in `SPEC-v1.md` §18. Any new code must be
  added to that list in the same commit that introduces it.
- Never echo an upstream response body to a caller: it can name the bank id.
  Bank ids never cross the API boundary (invariant 29).
- When behaviour the SPEC describes changes, update `SPEC-v1.md` in the SAME
  commit. Drift is a bug.
- Docstrings in this codebase record *why*, and cite the SPEC section and the
  measurement behind a decision. Match that. Do not write comments that
  restate the code.

---

## File Structure

| File | Responsibility in this plan |
|---|---|
| `src/memory/hindsight/client.py` | Bank upsert (no config), upstream error mapping, operation status derivation |
| `src/memory/hindsight/paths.py` | Hindsight's `{tenant}` path segment becomes a constant |
| `src/memory/api/memory.py` | Drop the retain-path `ensure_bank` call |
| `src/memory/mcp/tools.py` | Drop the retain-path `ensure_bank` call |
| `src/memory/api/users.py` | Key lifecycle: list users, list keys, revoke a key |
| `src/memory/api/projects.py` | `git_locator` repair path; reject unknown fields |
| `src/memory/api/admin.py` | Erasure audit ordering |
| `src/memory/api/mental_models.py` | Bound `max_tokens` and `trigger.mode` at the boundary |
| `src/memory/api/curation.py` | Reject blank `correct` content at the boundary |
| `src/memory/errors.py` | New typed errors |
| `src/memory/ratelimit.py` | Lock the read-check-append sequence |
| `src/memory/db.py` | `ensure_tenant` savepoint |
| `src/memory/config.py` | `tenant_id` stops feeding the Hindsight URL |
| `SPEC-v1.md` | §11.4, §16.3, §18, §19.1, §19.5, §20.2 |
| `scripts/e2e.py` | New scenarios for append, key revocation, locator repair |

---

### Task 1: Bank materialization — drop the config PATCH and the TTL cache

This is the decision task. `ensure_bank` currently does a PUT plus a config
PATCH setting `memory_defense` (accepted and ignored by hindsight-api 0.9.1)
and `store_document_text: false`. Both go away: screening moves to LiteLLM's
`pre_mcp_call` guardrail outside this repo, and `store_document_text` stays at
Hindsight's default so `update_mode="append"` works.

Measured facts you can rely on (verified live against hindsight-api 0.9.1 on
2026-08-22, do not re-derive):
- `DEFAULT_STORE_DOCUMENT_TEXT` is `True`.
- A bank auto-creates on first `retain` — POST to `.../banks/<never-created>/memories`
  returns 200.
- `create_directive` on a bank nothing ever retained into 500s upstream. This
  is the ONLY reason the PUT upsert must stay.

**Files:**
- Modify: `src/memory/hindsight/client.py` (`MATERIALIZATION_TTL_SECONDS`,
  `__init__`'s `_materialized`, `delete_bank`, `ensure_bank`)
- Modify: `src/memory/api/memory.py:268` (drop the call)
- Modify: `src/memory/mcp/tools.py:584` (drop the call)
- Modify: `SPEC-v1.md` §19.5, §20.2
- Test: `tests/test_hindsight_client.py` (replace
  `test_ensure_bank_sets_memory_defense_and_store_document_text`)

**Interfaces:**
- Produces: `HindsightClient.ensure_bank(bank_id: str) -> None` — still exists,
  still idempotent, now a single PUT. Task 2 relies on append working after
  this task.

- [ ] **Step 1: Write the failing tests**

Replace `test_ensure_bank_sets_memory_defense_and_store_document_text` in
`tests/test_hindsight_client.py` with these three:

```python
def test_ensure_bank_is_a_bare_upsert_with_no_config_patch(client, respx_mock):
    """The bank upsert survives; the config PATCH does not.

    Both fields v1 used to set are gone: `memory_defense` was accepted and
    ignored by hindsight-api 0.9.1 (measured, SPEC §20.2) and screening moved
    to the LiteLLM pre_mcp_call guardrail; `store_document_text` now stays at
    Hindsight's default of True so `update_mode="append"` works (SPEC §11.4).
    A PATCH here would be a no-op round trip on every cold bank.
    """
    put = respx_mock.put("/v1/default/banks/user_abc").respond(200, json={})
    patch = respx_mock.patch("/v1/default/banks/user_abc/config")

    client.ensure_bank("user_abc")

    assert put.called
    assert not patch.called


def test_ensure_bank_does_not_cache_across_calls(client, respx_mock):
    """No TTL cache: every call issues the PUT.

    The cache existed only to skip the config PATCH. It was per process, so
    with replicaCount>1 a delete_bank served by one pod left another pod's
    entry live and that pod then skipped re-materialization (review finding
    I3). With nothing left to skip, the cache is pure liability.
    """
    put = respx_mock.put("/v1/default/banks/user_abc").respond(200, json={})

    client.ensure_bank("user_abc")
    client.ensure_bank("user_abc")

    assert put.call_count == 2


def test_delete_bank_needs_no_cache_eviction(client, respx_mock):
    """delete_bank is a plain DELETE now.

    Its eviction existed to stop a stale cache entry from skipping the config
    PATCH after the bank was torn down. There is no cache and no PATCH.
    """
    route = respx_mock.delete("/v1/default/banks/user_abc").respond(
        200, json={"message": "Bank 'user_abc' and all associated data deleted successfully"}
    )

    client.delete_bank("user_abc")

    assert route.called
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_hindsight_client.py -k "ensure_bank or delete_bank_needs" -v`

Expected: FAIL — `test_ensure_bank_is_a_bare_upsert_with_no_config_patch`
fails because the PATCH is still sent, and
`test_ensure_bank_does_not_cache_across_calls` fails with `call_count == 1`.

- [ ] **Step 3: Delete the cache and the PATCH**

In `src/memory/hindsight/client.py`, delete the module constant
`MATERIALIZATION_TTL_SECONDS = 900.0` (line 27) and the
`self._materialized: dict[str, float] = {}` line in `__init__` (line 67).

Replace the whole `ensure_bank` method (lines 454-503) with:

```python
    def ensure_bank(self, bank_id: str) -> None:
        """Create the bank upstream. Idempotent, and cheap enough to repeat.

        Only `create_directive` needs this. Banks auto-create on first use --
        measured live: POST to `.../banks/<never-created>/memories` returns
        200 -- so retain, recall, reflect and create_mental_model must NOT pay
        for this round trip. A directive POST on a bank nothing has ever
        retained into 500s upstream (also measured), which is the one case
        left that needs the row to exist first.

        No config PATCH: as of Plan 6 there is nothing to configure.
        `memory_defense` was accepted and enforced-by-nothing in
        hindsight-api 0.9.1 (SPEC §20.2) and screening moved to the LiteLLM
        `pre_mcp_call` guardrail outside this service;
        `store_document_text` now stays at Hindsight's default (True, verified
        via `DEFAULT_STORE_DOCUMENT_TEXT`) so `update_mode="append"` works
        (SPEC §11.4) -- setting it explicitly would send the default back.

        No TTL cache either. It existed to skip that PATCH, and it was per
        process: with replicaCount>1 a `delete_bank` served by pod B left pod
        A's entry live, and pod A then skipped re-materialization.
        """
        self._request("PUT", paths.bank(self._tenant, bank_id), {})
```

In `delete_bank` (around line 437-452), delete the
`self._materialized.pop(bank_id, None)` line and rewrite the paragraph of its
docstring that explains the eviction, so no docstring claims a cache exists.

- [ ] **Step 4: Drop the retain-path calls**

In `src/memory/api/memory.py`, delete the `client.ensure_bank(bank_id)` line
(268). In `src/memory/mcp/tools.py`, delete the `client.ensure_bank(bank_id)`
line (584). In both files, adjust the surrounding comment so it no longer
refers to `ensure_bank` ordering — what must still hold is that
`provenance.build` runs before the upstream `retain`.

- [ ] **Step 5: Remove the now-unused import**

`time` may now be unused in `client.py`. Run
`cd /home/coder/workspace/local/ach-memory && uv run ruff check src` and remove
whatever it reports. Do not remove anything it does not report.

- [ ] **Step 6: Run the unit suite**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest -m "not integration" -q`

Expected: PASS. Tests asserting the old two-field PATCH will fail — those are
the ones you replaced in Step 1; if any others fail, they were pinning the old
behaviour and must be updated to the new one, not deleted.

- [ ] **Step 7: Update the SPEC**

In `SPEC-v1.md` §19.5, replace the description of what materialization sets:
the wrapper now only upserts the bank, sets no configuration, and does so only
where a bank must pre-exist.

In `SPEC-v1.md` §20.2, keep the measurement table (it is evidence and stays
true) and replace the closing paragraphs. The new position: Memory Defense is
not used, content screening is a LiteLLM `pre_mcp_call` guardrail outside this
service, `store_document_text` is left at Hindsight's default so a retained
document's text IS stored and IS retrievable via `get_document`. State plainly
that this re-opens the raw-text retrieval path the earlier measurement closed,
that it is a deliberate trade for `update_mode="append"` (§11.4), and that the
guardrail is an input-side control: it screens what enters on retain and does
nothing for text already stored, nor for a REST caller that does not traverse
the MCP path.

Add one paragraph recording what was measured on 2026-08-22 while deciding, so
nobody re-derives it: Memory Defense's `sensitive_data` stage IS enforced in
the MIT build (unlike the `block` stages) — a GitHub token was replaced with
`[REDACTED:github_token]` in the stored `original_text` — but its
`private_key_pem` pattern is
`-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY( BLOCK)?-----`, which
matches only the header: an RSA key was stored with the marker in place of its
BEGIN line and the entire base64 body and END line intact. A redaction marker
next to live key material is worse than no marker, so this stage is not
treated as a control.

- [ ] **Step 8: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/hindsight/client.py src/memory/api/memory.py src/memory/mcp/tools.py tests/test_hindsight_client.py SPEC-v1.md
git commit -m "stop configuring banks: no Memory Defense, no TTL cache"
```

---

### Task 2: Prove `update_mode="append"` actually works

The review's Critical. Before Task 1, append could never succeed: our own
`store_document_text: false` made Hindsight reject it. The sync path returned
502 `HINDSIGHT_ERROR` (a retryable code for a permanent failure) and the
default async path returned 200 plus an `operation_id` whose parent operation
stayed `pending` forever, with the real error only inside
`child_operations[0].error_message`.

The reason it survived to production: the only test covering append,
`tests/test_memory_api.py:630 test_append_with_a_document_id_still_reaches_hindsight`,
mocks a 200 and asserts `route.called`. It pins that the request was *sent*,
never that Hindsight accepts it. Do not write another test of that shape.

**Files:**
- Modify: `tests/test_memory_api.py` (the vacuous test)
- Create: `tests/test_append_integration.py`
- Modify: `scripts/e2e.py` (new scenario)

**Interfaces:**
- Consumes: Task 1's `ensure_bank` (no config PATCH), which is what makes
  append succeed upstream.

- [ ] **Step 1: Replace the vacuous unit test**

In `tests/test_memory_api.py`, replace
`test_append_with_a_document_id_still_reaches_hindsight` with a test that pins
the request body rather than the fact a call happened:

```python
def test_append_sends_update_mode_verbatim_to_hindsight(client, user_key, respx_mock):
    """Pins the wire body, not that a call happened.

    The predecessor asserted `route.called` against a mocked 200 and so stayed
    green for the entire period `append` could not work at all (Plan 6, review
    Critical C1). A mock cannot tell you the backend accepts something; that
    is what tests/test_append_integration.py is for.
    """
    route = respx_mock.post(url__regex=r".*/memories$").respond(
        200, json={"success": True, "items_count": 1, "async": False}
    )

    response = client.post(
        "/v1/memory/sync_retain",
        json={
            "scope": "user",
            "content": "second line",
            "document_id": "session:abc",
            "update_mode": "append",
        },
        headers={"Authorization": f"Bearer {user_key}"},
    )

    assert response.status_code == 200
    sent = json.loads(route.calls.last.request.content)
    assert sent["items"][0]["update_mode"] == "append"
    assert sent["items"][0]["document_id"] == "session:abc"
```

If `json` is not already imported at the top of that file, add `import json`.

- [ ] **Step 2: Write the integration test**

Create `tests/test_append_integration.py`:

```python
"""Append against the real engine.

SPEC §11.4 blesses `document_id` + `update_mode="append"` as the interactive
coding-session shape. Until Plan 6 it could never succeed, because the wrapper
set `store_document_text: false` and hindsight-api rejects appends against
that. Nothing but a live run can catch that class of defect: the unit test
mocked a 200 and stayed green throughout.
"""

import pytest

pytestmark = pytest.mark.integration


def test_append_accumulates_document_text(live_client, live_user_key):
    doc = "session:append-int"

    first = live_client.post(
        "/v1/memory/sync_retain",
        json={
            "scope": "user",
            "content": "the first line of the session",
            "document_id": doc,
            "update_mode": "replace",
        },
        headers={"Authorization": f"Bearer {live_user_key}"},
    )
    assert first.status_code == 200, first.text

    second = live_client.post(
        "/v1/memory/sync_retain",
        json={
            "scope": "user",
            "content": "the second line of the session",
            "document_id": doc,
            "update_mode": "append",
        },
        headers={"Authorization": f"Bearer {live_user_key}"},
    )
    assert second.status_code == 200, second.text

    fetched = live_client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": doc},
        headers={"Authorization": f"Bearer {live_user_key}"},
    )
    assert fetched.status_code == 200, fetched.text
    text = fetched.json()["result"].get("original_text") or ""
    assert "the first line of the session" in text
    assert "the second line of the session" in text
```

If `live_client` / `live_user_key` fixtures do not exist, read
`tests/conftest.py` and follow whatever the existing integration tests use;
do not invent a second fixture style.

- [ ] **Step 3: Run both**

```bash
cd /home/coder/workspace/local/ach-memory
uv run pytest tests/test_memory_api.py -k append -v
docker compose up -d && uv run pytest -m integration -k append -v
```

Expected: both PASS. If the integration test fails with 502, Task 1 is not
complete — check that no config PATCH is being sent.

- [ ] **Step 4: Add the e2e scenario**

`scripts/e2e.py` has no append scenario at all (`grep -n append scripts/e2e.py`
returns only unrelated `list.append`). Add one following the file's existing
scenario style, named `retain.append_accumulates_document_text`, that does the
same three calls as the integration test and asserts both lines are present.

- [ ] **Step 5: Run the e2e**

Run: `cd /home/coder/workspace/local/ach-memory && uv run python scripts/e2e.py`

Expected: all scenarios pass, count is one higher than before.

- [ ] **Step 6: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add tests/test_memory_api.py tests/test_append_integration.py scripts/e2e.py
git commit -m "test(append): pin the wire body and prove append against the real engine"
```

---

### Task 3: Surface an async operation whose children all failed

Discovered through append but not specific to it: when an async `retain`'s
work fails, Hindsight leaves the PARENT operation at `status: "pending"`
indefinitely and records the reason only in
`child_operations[N].error_message`. Measured live: polled 15 times over 30s,
parent never left `pending`. A caller following the documented contract —
poll `get_operation` until a terminal status — waits forever.

We cannot fix the upstream. We can stop reporting `pending` for an operation
whose every child has already errored.

**Files:**
- Modify: `src/memory/hindsight/client.py` (`get_operation`)
- Test: `tests/test_hindsight_client.py`

**Interfaces:**
- Produces: `HindsightClient.get_operation` returns the upstream record with
  `status` derived to `"failed"` when every child carries an `error_message`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_hindsight_client.py`:

```python
def test_get_operation_reports_failed_when_every_child_errored(client, respx_mock):
    """Upstream leaves the parent `pending` forever when the work failed.

    Measured live 2026-08-22 against hindsight-api 0.9.1: an async retain
    whose child failed sat at `pending` for 30s of polling, with the reason
    only in child_operations[0].error_message. A caller polling for a terminal
    status never gets one, so we derive it.
    """
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={
            "operation_id": "op-1",
            "status": "pending",
            "child_operations": [
                {"operation_id": "c-1", "status": "pending", "error_message": "ValueError: nope"}
            ],
        },
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "failed"


def test_get_operation_leaves_a_partially_failed_parent_alone(client, respx_mock):
    """One failed child among several is not a failed operation.

    Work may still be in flight; reporting `failed` would stop a caller
    polling for the rest.
    """
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={
            "operation_id": "op-1",
            "status": "pending",
            "child_operations": [
                {"operation_id": "c-1", "status": "pending", "error_message": "ValueError: nope"},
                {"operation_id": "c-2", "status": "pending"},
            ],
        },
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "pending"


def test_get_operation_does_not_touch_a_terminal_status(client, respx_mock):
    """`completed` stays `completed`, children or not."""
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={
            "operation_id": "op-1",
            "status": "completed",
            "child_operations": [
                {"operation_id": "c-1", "status": "completed", "error_message": "warn"}
            ],
        },
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "completed"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_hindsight_client.py -k get_operation -v`

Expected: the first FAILS with `assert 'pending' == 'failed'`; the other two
already pass (they pin behaviour you must not break).

- [ ] **Step 3: Derive the status**

In `src/memory/hindsight/client.py`, find `get_operation` and wrap its return:

```python
_NON_TERMINAL = ("pending", "running")


def _derive_failed(record: dict) -> dict:
    """Report `failed` for an operation whose every child already errored.

    Hindsight 0.9.1 never transitions the parent: measured live 2026-08-22, an
    async retain with a failed child sat at `pending` through 30s of polling,
    the reason readable only in child_operations[N].error_message. A caller
    polling for a terminal status would wait forever, which is how a silently
    lost write looks from the outside.

    Deliberately narrow: only when the parent is non-terminal AND there is at
    least one child AND every child carries an error_message. A partial
    failure leaves the status alone -- work may still be in flight, and
    stopping a caller's poll early would lose the rest of it.
    """
    children = record.get("child_operations") or []
    if record.get("status") not in _NON_TERMINAL or not children:
        return record
    if all(child.get("error_message") for child in children):
        return {**record, "status": "failed"}
    return record
```

Then have `get_operation` return `_derive_failed(self._request(...))` instead
of the raw result.

- [ ] **Step 4: Run the tests**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_hindsight_client.py -k get_operation -v`

Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/hindsight/client.py tests/test_hindsight_client.py
git commit -m "fix(operations): report failed instead of polling pending forever"
```

---

### Task 4: Key lifecycle — list users, list keys, revoke a key

The review's second Critical, and the one I escalated: a leaked user key
cannot be revoked through the API. `grep -rn "revok" src/` returns exactly one
hit today, the rejection message in `auth/principal.py:42`. The `api_keys.status`
column is read on every authentication and written by nothing.

SPEC §5.3 says user keys are "created **and revoked** through the API" and
§16.3 lists three routes that do not exist:

```text
GET    /v1/users
GET    /v1/users/{user_id}/keys
DELETE /v1/users/{user_id}/keys/{key_id}
```

All three are master-gated, like every other route in `users.py`.

**Files:**
- Modify: `src/memory/api/users.py`
- Modify: `src/memory/errors.py` (add `KeyNotFound`)
- Modify: `SPEC-v1.md` §18 (add `KEY_NOT_FOUND`)
- Test: `tests/test_users_api.py`
- Modify: `scripts/e2e.py`

**Interfaces:**
- Produces: `KeyNotFound` (code `KEY_NOT_FOUND`, status 404) in
  `src/memory/errors.py`; response models `ListUsersResponse`,
  `KeySummary`, `ListKeysResponse` in `src/memory/api/users.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_users_api.py`:

```python
def test_revoking_a_key_stops_it_authenticating(client, master_key):
    """The whole point: a leaked key must die on request.

    Before Plan 6 the only way was `UPDATE api_keys SET status='revoked'` in
    Postgres -- the status column was read on every auth and written by
    nothing (review Critical C2, SPEC §5.3).
    """
    user_id = client.post(
        "/v1/users", json={}, headers={"Authorization": f"Bearer {master_key}"}
    ).json()["user_id"]
    minted = client.post(
        f"/v1/users/{user_id}/keys", json={},
        headers={"Authorization": f"Bearer {master_key}"},
    ).json()

    before = client.get("/v1/projects", headers={"Authorization": f"Bearer {minted['key']}"})
    assert before.status_code == 200

    revoked = client.delete(
        f"/v1/users/{user_id}/keys/{minted['key_id']}",
        headers={"Authorization": f"Bearer {master_key}"},
    )
    assert revoked.status_code == 204

    after = client.get("/v1/projects", headers={"Authorization": f"Bearer {minted['key']}"})
    assert after.status_code == 401


def test_listing_keys_never_returns_the_secret(client, master_key):
    """A list route that leaks the plaintext would be worse than no route.

    The plaintext exists exactly once, in the mint response (SPEC §5.3), and
    only the hash is stored -- so this pins that nothing added a secret or
    secret_hash field to the summary.
    """
    user_id = client.post(
        "/v1/users", json={}, headers={"Authorization": f"Bearer {master_key}"}
    ).json()["user_id"]
    minted = client.post(
        f"/v1/users/{user_id}/keys", json={},
        headers={"Authorization": f"Bearer {master_key}"},
    ).json()

    listed = client.get(
        f"/v1/users/{user_id}/keys", headers={"Authorization": f"Bearer {master_key}"}
    )

    assert listed.status_code == 200
    body = listed.json()
    assert [k["key_id"] for k in body["keys"]] == [minted["key_id"]]
    assert body["keys"][0]["status"] == "active"
    assert minted["key"] not in listed.text
    assert "secret_hash" not in listed.text


def test_revoking_a_key_twice_is_not_found_the_second_time(client, master_key):
    user_id = client.post(
        "/v1/users", json={}, headers={"Authorization": f"Bearer {master_key}"}
    ).json()["user_id"]
    key_id = client.post(
        f"/v1/users/{user_id}/keys", json={},
        headers={"Authorization": f"Bearer {master_key}"},
    ).json()["key_id"]
    path = f"/v1/users/{user_id}/keys/{key_id}"

    assert client.delete(path, headers={"Authorization": f"Bearer {master_key}"}).status_code == 204
    second = client.delete(path, headers={"Authorization": f"Bearer {master_key}"})
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "KEY_NOT_FOUND"


def test_a_key_of_another_user_cannot_be_revoked_through_this_user(client, master_key):
    """The key id is addressed under a user id; the pair must match.

    Otherwise the user segment is decoration and any key id revokes.
    """
    headers = {"Authorization": f"Bearer {master_key}"}
    victim = client.post("/v1/users", json={}, headers=headers).json()["user_id"]
    other = client.post("/v1/users", json={}, headers=headers).json()["user_id"]
    victim_key = client.post(f"/v1/users/{victim}/keys", json={}, headers=headers).json()["key_id"]

    response = client.delete(f"/v1/users/{other}/keys/{victim_key}", headers=headers)

    assert response.status_code == 404


def test_listing_users_is_master_only(client, user_key):
    response = client.get("/v1/users", headers={"Authorization": f"Bearer {user_key}"})
    assert response.status_code == 403
```

If the fixture names `master_key` / `user_key` / `client` differ in this
repo's `tests/conftest.py`, use the existing ones.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_users_api.py -v`

Expected: FAIL with 405 or 404 on the new paths.

- [ ] **Step 3: Add the error**

In `src/memory/errors.py`, beside the other not-found errors:

```python
class KeyNotFound(DomainError):
    code = "KEY_NOT_FOUND"
    status = 404
```

- [ ] **Step 4: Add the three routes**

In `src/memory/api/users.py`, add the response models next to the existing
ones:

```python
class UserSummary(BaseModel):
    user_id: str
    created_at: str


class ListUsersResponse(BaseModel):
    users: list[UserSummary]


class KeySummary(BaseModel):
    # No secret, no secret_hash: the plaintext exists once, in the mint
    # response (SPEC §5.3), and the hash is what protects it.
    key_id: str
    status: str
    created_at: str


class ListKeysResponse(BaseModel):
    keys: list[KeySummary]
```

and the routes at the end of the file:

```python
@router.get("", response_model=ListUsersResponse)
def list_users(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> ListUsersResponse:
    """SPEC §16.3. Tenant-scoped, like every read in this service."""
    rows = (
        db.query(User)
        .filter(User.tenant_id == principal.tenant_id)
        .order_by(User.created_at)
        .all()
    )
    return ListUsersResponse(
        users=[UserSummary(user_id=r.id, created_at=r.created_at.isoformat()) for r in rows]
    )


@router.get("/{user_id}/keys", response_model=ListKeysResponse)
def list_keys(
    user_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
) -> ListKeysResponse:
    """SPEC §16.3. Revoked keys stay listed: an operator auditing a leak needs
    to see that the revocation happened, not find the row gone."""
    user = db.get(User, user_id)
    if user is None or user.tenant_id != principal.tenant_id:
        raise UserNotFound(user_id=user_id)
    rows = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user.id, ApiKey.tenant_id == principal.tenant_id)
        .order_by(ApiKey.created_at)
        .all()
    )
    return ListKeysResponse(
        keys=[
            KeySummary(key_id=r.id, status=r.status, created_at=r.created_at.isoformat())
            for r in rows
        ]
    )


@router.delete("/{user_id}/keys/{key_id}", status_code=204)
def revoke_key(
    user_id: str,
    key_id: str,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> None:
    """SPEC §5.3, §16.3.

    Status flip, not a DELETE: the audit trail must keep showing the key
    existed and when it stopped working. `principal` resolution already
    refuses a non-active key, so this takes effect on the next request with
    no cache to invalidate.

    Filtered on user_id AND tenant AND active: revoking an already-revoked key
    is a 404 rather than a silent success, so an operator racing a colleague
    learns which of them did it.
    """
    row = (
        db.query(ApiKey)
        .filter(
            ApiKey.id == key_id,
            ApiKey.user_id == user_id,
            ApiKey.tenant_id == principal.tenant_id,
            ApiKey.status == "active",
        )
        .one_or_none()
    )
    if row is None:
        raise KeyNotFound("no such active key for that user", key_id=key_id)
    row.status = "revoked"
    audit.record(db, principal, "key.revoke", row.id, on_behalf_of=on_behalf_of)
    db.commit()
```

Add `KeyNotFound` to the `from memory.errors import ...` line.

- [ ] **Step 5: Verify principal resolution really refuses a revoked key**

Read `src/memory/auth/principal.py` and confirm the key lookup filters on
`status == "active"`. If it does not, the revoke route is decorative — fix the
filter and say so in the commit message.

- [ ] **Step 6: Run the tests**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_users_api.py -v`

Expected: PASS.

- [ ] **Step 7: Update the SPEC**

Add `KEY_NOT_FOUND` to §18's closed list.

- [ ] **Step 8: Add an e2e scenario**

In `scripts/e2e.py`, add `keys.revocation_stops_authentication`: mint a user
key, call an authenticated route with it (expect 200), revoke it, call again
(expect 401).

- [ ] **Step 9: Run the e2e**

Run: `cd /home/coder/workspace/local/ach-memory && uv run python scripts/e2e.py`

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/api/users.py src/memory/errors.py tests/test_users_api.py scripts/e2e.py SPEC-v1.md
git commit -m "feat(keys): list users, list keys, and revoke a leaked key"
```

---

### Task 5: `git_locator` repair path, and stop ignoring unknown fields

Review finding I1, found independently by two reviewers. `git_locator` is
one-shot enrichment: the first caller to present one fixes it on the project
(`projects.py:157`), and every caller presenting a different one afterwards
gets `PROJECT_LOCATOR_MISMATCH` before reaching the bank. Over MCP the value
comes from the model, on all 15 tools.

SPEC §8.3 and §8.4 both promise repair "through the Project API". No route
writes the column. Worse, pydantic's default `extra="ignore"` means a caller
who follows §8.4 literally and PATCHes `{"git_locator": "..."}` gets **200 OK
with nothing changed** — a silent no-op on the documented recovery path.

**Files:**
- Modify: `src/memory/api/projects.py`
- Test: `tests/test_projects_api.py`
- Modify: `scripts/e2e.py`

**Interfaces:**
- Produces: `UpdateProjectRequest` replaces `RenameProjectRequest` as the
  `PATCH /v1/projects/{slug}` body. Both fields optional; `model_fields_set`
  distinguishes "absent" from "explicit null", so `{"git_locator": null}`
  clears and an omitted key leaves it alone.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_projects_api.py`:

```python
def test_patch_updates_the_git_locator(client, user_key):
    """SPEC §8.4's promised recovery path.

    A poisoned locator otherwise locks the whole owning group out of the
    project's memory with no API repair (review finding I1).
    """
    headers = {"Authorization": f"Bearer {user_key}"}
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/wrong"},
        headers=headers,
    )

    response = client.patch(
        "/v1/projects/payments-api",
        json={"git_locator": "github.com/acme/payments-api"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["git_locator"] == "github.com/acme/payments-api"


def test_patch_with_an_explicit_null_clears_the_git_locator(client, user_key):
    """§8.4 says "clear or update". Clearing re-opens first-toucher
    enrichment, which is the escape hatch when nobody knows the right value."""
    headers = {"Authorization": f"Bearer {user_key}"}
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/wrong"},
        headers=headers,
    )

    response = client.patch(
        "/v1/projects/payments-api", json={"git_locator": None}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["git_locator"] is None


def test_patch_without_git_locator_leaves_it_alone(client, user_key):
    """An omitted key is not the same as null -- a rename must not wipe it."""
    headers = {"Authorization": f"Bearer {user_key}"}
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api", "git_locator": "github.com/acme/payments-api"},
        headers=headers,
    )

    response = client.patch(
        "/v1/projects/payments-api", json={"project_slug": "payments"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["project_slug"] == "payments"
    assert response.json()["git_locator"] == "github.com/acme/payments-api"


def test_patch_rejects_an_unknown_field(client, user_key):
    """The silent no-op is the bug behind I1: a caller following the SPEC got
    200 OK and no change. An unknown key must be a typed 422."""
    headers = {"Authorization": f"Bearer {user_key}"}
    client.post("/v1/projects", json={"project_slug": "payments-api"}, headers=headers)

    response = client.patch(
        "/v1/projects/payments-api", json={"gti_locator": "typo"}, headers=headers
    )

    assert response.status_code == 422
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_projects_api.py -k "git_locator or unknown_field" -v`

Expected: FAIL — the update returns the old value, and the unknown field is
silently accepted with 200.

- [ ] **Step 3: Replace the request model**

In `src/memory/api/projects.py`, replace `RenameProjectRequest` with:

```python
class UpdateProjectRequest(BaseModel):
    # extra="forbid": the silent no-op this replaces IS review finding I1 --
    # a caller following SPEC §8.4 PATCHed git_locator, got 200 OK, and
    # nothing changed, because the model ignored the field it did not declare.
    model_config = ConfigDict(extra="forbid")

    project_slug: str | None = None
    # Bounded to match the projects.git_locator column (String(512)) so an
    # oversize value is a typed 422 at the boundary, not a 500 from the DB.
    git_locator: str | None = Field(default=None, max_length=512)
```

Add `ConfigDict` to the pydantic import.

- [ ] **Step 4: Rewrite the route**

Replace `rename_project` with:

```python
@router.patch("/{project_slug}", response_model=ProjectResponse)
def update_project(
    project_slug: str,
    body: UpdateProjectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    """Rename, repair the locator, or both (SPEC §8.4, §9).

    `model_fields_set`, not a None check: §8.4 says "clear or update", so an
    explicit null must clear the column while an omitted key must leave it
    alone. A rename that silently wiped the locator would hand the next
    caller the first-toucher enrichment all over again.
    """
    result = domain.resolve(db, principal, project_slug, create=False)
    project = result.project

    if body.project_slug is not None:
        project = domain.rename(
            db, principal, project, body.project_slug, on_behalf_of=on_behalf_of
        )

    if "git_locator" in body.model_fields_set:
        project.git_locator = (
            domain.canonical_locator(body.git_locator) if body.git_locator else None
        )
        audit.record(
            db, principal, "project.locator.update", project.project_slug,
            on_behalf_of=on_behalf_of,
        )

    db.commit()
    return _response(project)
```

Make sure `audit` and `domain.canonical_locator` are imported. Check
`src/memory/projects.py` for the exact exported name of the locator
canonicaliser (it is called at `projects.py:184`) and use that name.

- [ ] **Step 5: Run the tests**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_projects_api.py -v`

Expected: PASS. Any existing test posting an unknown key to this route will
now 422 — that is the fix working; update the test.

- [ ] **Step 6: Add an e2e scenario**

Add `projects.poisoned_locator_can_be_repaired`: create a project with one
locator, confirm a retain with a different locator gets
`PROJECT_LOCATOR_MISMATCH`, PATCH the locator to the second value, confirm the
same retain now succeeds.

- [ ] **Step 7: Run the e2e**

Run: `cd /home/coder/workspace/local/ach-memory && uv run python scripts/e2e.py`

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/api/projects.py tests/test_projects_api.py scripts/e2e.py
git commit -m "fix(projects): make SPEC 8.4's locator repair path real"
```

---

### Task 6: Do not audit an erasure before it happens

Review finding I2. `clear_memories` and `delete_bank` in `admin.py` call
`_resolve_bank` (which writes the master-key audit row), then `db.commit()`,
then the destructive upstream call. If Hindsight fails at that point the
caller gets a 502 and the audit row survives, claiming
`admin.memory.delete` against that bank forever — indistinguishable from a
real erasure. SPEC §12.3 calls this the only complete erasure path, so the
`action` string IS the compliance claim.

The commit-before-upstream ordering is deliberate and correct everywhere else
(a retain must not lose the project it just created). These two routes are the
exception because what they record is a claim about the upstream call itself.

**Files:**
- Modify: `src/memory/api/admin.py` (`clear_memories`, `delete_bank`)
- Test: `tests/test_admin_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_admin_api.py`:

```python
def test_a_failed_delete_leaves_no_audit_row(client, master_key, respx_mock, db_session):
    """An audit row is a claim that the erasure happened (SPEC §12.3).

    The existing coverage mocks a 200 and asserts the row appears, so nothing
    pinned the failure path (review finding I2).
    """
    respx_mock.delete(url__regex=r".*/banks/.*").respond(503, json={})
    user_id = client.post(
        "/v1/users", json={}, headers={"Authorization": f"Bearer {master_key}"}
    ).json()["user_id"]

    response = client.request(
        "DELETE",
        f"/v1/admin/memory/user?user_id={user_id}",
        headers={"Authorization": f"Bearer {master_key}"},
    )

    assert response.status_code == 502
    audit_rows = client.get(
        "/v1/admin/audit", headers={"Authorization": f"Bearer {master_key}"}
    ).json()
    assert not [e for e in audit_rows["events"] if e["action"] == "admin.memory.delete"]


def test_a_failed_clear_leaves_no_audit_row(client, master_key, respx_mock):
    respx_mock.post(url__regex=r".*/memories/clear.*").respond(503, json={})
    user_id = client.post(
        "/v1/users", json={}, headers={"Authorization": f"Bearer {master_key}"}
    ).json()["user_id"]

    response = client.post(
        f"/v1/admin/memory/user/clear?user_id={user_id}",
        headers={"Authorization": f"Bearer {master_key}"},
    )

    assert response.status_code == 502
    audit_rows = client.get(
        "/v1/admin/audit", headers={"Authorization": f"Bearer {master_key}"}
    ).json()
    assert not [e for e in audit_rows["events"] if e["action"] == "admin.memory.clear"]
```

Check `tests/test_admin_api.py` for the exact URL shapes and the audit
response key (`events` vs something else) and match them.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_admin_api.py -k "failed_delete or failed_clear" -v`

Expected: FAIL — the audit row is present despite the 502.

- [ ] **Step 3: Move the commit after the upstream call**

In `src/memory/api/admin.py`, in BOTH `clear_memories` and `delete_bank`, move
`db.commit()` to after the `get_client()` call, and add above it:

```python
    # Commit AFTER the upstream call, unlike every other route in this
    # service. Elsewhere the committed state is "a master key touched this
    # bank" or an enriched locator -- true whatever Hindsight does next. Here
    # the audited action IS the compliance claim that SPEC §12.3's only
    # complete erasure path completed, so a 502 must not leave a row saying
    # it did. `create=False` above means resolution created nothing, so
    # there is no local state that needs to survive the failure.
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_admin_api.py -v`

Expected: PASS, including the existing success-path audit tests.

- [ ] **Step 5: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/api/admin.py tests/test_admin_api.py
git commit -m "fix(admin): do not record an erasure the backend refused"
```

---

### Task 7: Stop blaming the backend for the caller's mistake

Two review findings in one boundary-validation change.

**I5:** `curate` maps EVERY upstream 400 to `MEMORY_NOT_CURATABLE`. Upstream
checks blank text *before* the record lookup, so `correct` with blank content
returns 409 "this memory cannot be curated" — a lie — and blank content on a
non-existent memory id returns 409 instead of 404.

**I6:** the upstream is FastAPI, so schema violations are **422**, not 400.
`_request` only special-cases 400, so every schema rejection becomes a 502.
Mental-model `max_tokens` (upstream `ge=256, le=8192`) and `trigger.mode`
(upstream `Literal["full","delta"]`) are unbounded on our side, so ordinary
caller values reach it. The repo already treats this as a defect elsewhere:
`documents.py`, `curation.py` and `memory.py` all bound their fields with the
comment "so an out-of-range value is a typed 422 at the boundary, not a 502
blaming the backend for the caller's typo". Mental models were missed.

**Files:**
- Modify: `src/memory/api/curation.py` (`CorrectRequest.content`)
- Modify: `src/memory/api/mental_models.py` (bounds)
- Modify: `src/memory/hindsight/client.py` (`_request` handles 422)
- Test: `tests/test_curation_api.py`, `tests/test_mental_models_api.py`,
  `tests/test_hindsight_client.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_curation_api.py`:

```python
def test_correct_rejects_blank_content_at_the_boundary(client, user_key):
    """Upstream checks blank text before the row lookup, so without this the
    reply is 409 MEMORY_NOT_CURATABLE -- a lie about the memory (finding I5)."""
    response = client.post(
        "/v1/memory/correct",
        json={
            "scope": "user",
            "memory_id": "11111111-1111-1111-1111-111111111111",
            "content": "   ",
        },
        headers={"Authorization": f"Bearer {user_key}"},
    )

    assert response.status_code == 422
```

In `tests/test_mental_models_api.py`:

```python
def test_max_tokens_below_the_upstream_floor_is_a_422(client, user_key):
    """Upstream is FastAPI: its schema rejection is a 422, which _request
    turns into a 502 blaming the backend. Bound it here (finding I6)."""
    response = client.post(
        "/v1/mental-models",
        json={"scope": "user", "name": "x", "source_query": "y", "max_tokens": 100},
        headers={"Authorization": f"Bearer {user_key}"},
    )

    assert response.status_code == 422


def test_an_unknown_trigger_mode_is_a_422(client, user_key):
    response = client.post(
        "/v1/mental-models",
        json={
            "scope": "user", "name": "x", "source_query": "y",
            "trigger": {"mode": "incremental"},
        },
        headers={"Authorization": f"Bearer {user_key}"},
    )

    assert response.status_code == 422
```

In `tests/test_hindsight_client.py`:

```python
def test_an_upstream_422_is_not_reported_as_a_backend_fault(client, respx_mock):
    """FastAPI answers a schema violation with 422. Folding it into
    HINDSIGHT_ERROR tells an agent to retry what can never succeed."""
    respx_mock.post(url__regex=r".*/memories$").respond(422, json={"detail": "nope"})

    with pytest.raises(UpstreamRejected) as excinfo:
        client.retain("user_abc", "content")

    assert excinfo.value.status == 400
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_curation_api.py tests/test_mental_models_api.py tests/test_hindsight_client.py -k "blank_content or max_tokens or trigger_mode or 422" -v`

Expected: FAIL.

- [ ] **Step 3: Bound `correct`'s content**

In `src/memory/api/curation.py`, change `CorrectRequest.content`:

```python
    # min_length=1 plus the strip check below: Hindsight rejects blank text
    # BEFORE it looks the record up, so a blank correct on a valid memory came
    # back as 409 MEMORY_NOT_CURATABLE -- telling an agent the fact is a
    # derived observation when it simply sent nothing (review finding I5).
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value
```

Add `field_validator` to the pydantic import.

- [ ] **Step 4: Bound the mental-model fields**

In `src/memory/api/mental_models.py`, in BOTH `CreateMentalModelRequest` and
`UpdateMentalModelRequest`:

```python
    # Mirrors hindsight-api 0.9.1's own Field(ge=256, le=8192). Upstream is
    # FastAPI, so its rejection is a 422 that _request cannot distinguish from
    # a backend fault -- it became a 502 (review finding I6).
    max_tokens: int | None = Field(default=None, ge=256, le=8192)
```

and replace the free-form `trigger: dict[str, Any] | None = None` with a model
that pins only the one field upstream types, leaving the rest pass-through as
SPEC §14.5 requires:

```python
class MentalModelTrigger(BaseModel):
    # Pass-through by design (SPEC §14.5) EXCEPT `mode`, which upstream types
    # as Literal["full","delta"]: an unknown value was a 422 upstream and a
    # 502 here. Everything else stays unvalidated on purpose -- Hindsight's
    # own defaults already mean "no automatic refresh".
    model_config = ConfigDict(extra="allow")

    mode: Literal["full", "delta"] | None = None
```

and use `trigger: MentalModelTrigger | None = None` in both request models.
Where the value is forwarded to the client, send
`body.trigger.model_dump(exclude_none=True) if body.trigger else None` so the
pass-through keys survive.

- [ ] **Step 5: Handle 422 in the client**

In `src/memory/errors.py` add:

```python
class UpstreamRejected(DomainError):
    code = "UPSTREAM_REJECTED"
    status = 400
```

In `src/memory/hindsight/client.py`, add to `_request` immediately before the
`>= 400` branch:

```python
        if response.status_code == 422:
            # The upstream is FastAPI: a schema violation is a 422, never a
            # 400. Folding it into HINDSIGHT_ERROR told an agent to retry a
            # request that can never succeed (review finding I6). The body is
            # still never echoed -- it can name the bank.
            logger.warning("hindsight 422: request shape rejected upstream")
            raise UpstreamRejected("the memory backend rejected this request shape")
```

- [ ] **Step 6: Run the tests**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest -m "not integration" -q`

Expected: PASS.

- [ ] **Step 7: Update the SPEC**

Add `UPSTREAM_REJECTED` to §18's closed list. In §14.5, note that `mode` is
the one validated key of `trigger`, and why.

- [ ] **Step 8: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/api/curation.py src/memory/api/mental_models.py src/memory/hindsight/client.py src/memory/errors.py tests/ SPEC-v1.md
git commit -m "fix(errors): stop reporting caller mistakes as backend faults"
```

---

### Task 8: `MEMORY_TENANT_ID` must stop feeding the Hindsight URL

Review finding I4. `MEMORY_TENANT_ID` is documented as configurable and used
for two unrelated things: our own DB tenancy, and Hindsight's `{tenant}` path
segment. hindsight-api 0.9.1 hardcodes `default` in all 83 bank routes
(`grep -c '"/v1/default' http.py` → 83; upstream tenancy comes from the
`Authorization` header, never the URL). Any other value makes every upstream
URL match no route, so reads 404 — and our `not_found=` mappings turn that
into `DOCUMENT_NOT_FOUND`, `MEMORY_NOT_FOUND` and friends. The service half
lies and half 502s with no symptom pointing at the setting.

Keep the setting for our own tenancy. Sever it from the upstream path.

**Files:**
- Modify: `src/memory/hindsight/paths.py`
- Modify: `src/memory/hindsight/client.py` (`self._tenant`)
- Modify: `deploy/helm/ach-memory/values.yaml` (the comment)
- Modify: `SPEC-v1.md` §19.1
- Test: `tests/test_hindsight_paths.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_upstream_tenant_segment_is_always_default():
    """hindsight-api 0.9.1 hardcodes /v1/default in all 83 bank routes; its
    own tenancy comes from the Authorization header. Deriving this segment
    from MEMORY_TENANT_ID made a plausible config value 404 every read and
    surface as DOCUMENT_NOT_FOUND (review finding I4)."""
    assert paths.bank("ignored-by-design", "user_abc") == "/v1/default/banks/user_abc"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_hindsight_paths.py -k tenant_segment -v`

Expected: FAIL — the path is `/v1/ignored-by-design/banks/user_abc`.

- [ ] **Step 3: Pin the segment**

In `src/memory/hindsight/paths.py`, add the constant and use it in `bank()`:

```python
# hindsight-api 0.9.1 registers all 83 bank routes under the literal segment
# `default`; multi-tenancy upstream is resolved from the Authorization header
# into a Postgres schema, never from the URL. The `tenant` parameter is kept
# so the signature does not churn across the 20-odd helpers built on bank(),
# and so this constant is the single place to change if that ever moves.
HINDSIGHT_TENANT = "default"


def bank(tenant: str, bank_id: str) -> str:
    return f"/v1/{HINDSIGHT_TENANT}/banks/{bank_id}"
```

- [ ] **Step 4: Run the tests**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest -m "not integration" -q`

Expected: PASS.

- [ ] **Step 5: Correct the documentation**

In `deploy/helm/ach-memory/values.yaml`, change the `MEMORY_TENANT_ID` comment
so it no longer claims the value is "also Hindsight's `{tenant}` path
segment". In `SPEC-v1.md` §19.1, replace the claim that the tenant segment is
configurable with what is true, and cite the measurement.

- [ ] **Step 6: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/hindsight/paths.py tests/test_hindsight_paths.py deploy/helm/ach-memory/values.yaml SPEC-v1.md
git commit -m "fix(paths): pin Hindsight's tenant segment, it was never configurable"
```

---

### Task 9: Close §18's closed list

Review finding I7, found by two reviewers and confirmed by a direct diff of
`errors.py` against the SPEC. Five reachable codes are absent from a list the
repo treats as closed (`paths.py:40` reasons about a code being "not in SPEC
§18's closed list" as a defect to avoid). The converse was verified clean:
every §18 entry is raisable.

**Files:**
- Modify: `SPEC-v1.md` §18
- Test: `tests/test_errors.py`

- [ ] **Step 1: Write the failing test**

Create or extend `tests/test_errors.py`:

```python
def test_every_error_code_is_in_the_spec_closed_list():
    """§18 is declared CLOSED and the codebase reasons about it that way.
    Five codes were missing when this test was written (review finding I7);
    the check runs both ways so neither side can drift again."""
    import re
    from pathlib import Path

    from memory import errors

    spec = Path(__file__).resolve().parents[1] / "SPEC-v1.md"
    spec_codes = set(re.findall(r"\b[A-Z][A-Z_]{4,}\b", spec.read_text()))

    declared = {
        obj.code
        for obj in vars(errors).values()
        if isinstance(obj, type) and issubclass(obj, errors.DomainError)
    }

    assert declared <= spec_codes, f"missing from SPEC §18: {sorted(declared - spec_codes)}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_errors.py -v`

Expected: FAIL listing `GROUP_ALREADY_EXISTS`, `GROUP_NOT_FOUND`,
`INTERNAL_ERROR`, `INVALID_OWNER_TYPE`, `USER_NOT_FOUND` — plus
`KEY_NOT_FOUND` and `UPSTREAM_REJECTED` if Tasks 4 and 7 landed first and you
forgot their SPEC steps.

- [ ] **Step 3: Add the codes to §18**

Add a row for each missing code to §18's table with its status and the
condition that raises it: `USER_NOT_FOUND` (404), `GROUP_NOT_FOUND` (404),
`GROUP_ALREADY_EXISTS` (409), `INVALID_OWNER_TYPE` (422), and `INTERNAL_ERROR`
(500, the catch-all in `app.py` and the MCP generic handler).

- [ ] **Step 4: Run the test**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_errors.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add SPEC-v1.md tests/test_errors.py
git commit -m "docs(spec): close 18's closed list, and test that it stays closed"
```

---

### Task 10: Two concurrency races

**I8:** `Limiter.check` is read-check-append with no lock. Every write route is
a sync `def`, so FastAPI runs it in Starlette's threadpool, and MCP tools run
in AnyIO's 40-thread pool. Concurrent requests on one credential can all read
`len(hits) == limit - 1` before any appends, so the configured per-credential
cap (a SPEC §20 MUST) is exceeded within a single process. This is NOT the
documented per-replica multiplier, which is about scaling across pods.

**I9:** `ensure_tenant` is the only check-then-insert in the codebase without
a savepoint. Two concurrent creates on a fresh tenant both see `None`, both
insert, and the loser gets an uncaught `IntegrityError` → bare 500. The same
race is handled correctly three times over in `create_user`, `create_group`
and `projects.create`.

**Files:**
- Modify: `src/memory/ratelimit.py`
- Modify: `src/memory/db.py`
- Test: `tests/test_ratelimit.py`, `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_ratelimit.py`:

```python
def test_concurrent_checks_never_exceed_the_limit():
    """Sync routes run in Starlette's threadpool and MCP tools in AnyIO's
    40-thread pool, so one credential really does reach check() concurrently
    (review finding I8). Distinct from the documented per-replica multiplier.
    """
    import threading

    from memory.errors import RateLimited
    from memory.ratelimit import Limiter

    limiter = Limiter(limit=10, window_seconds=60.0)
    admitted = []
    lock = threading.Lock()
    start = threading.Barrier(40)

    def attempt():
        start.wait()
        try:
            limiter.check("one-key")
        except RateLimited:
            return
        with lock:
            admitted.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a thread hung"

    assert len(admitted) == 10
```

In `tests/test_db.py`:

```python
def test_ensure_tenant_survives_a_concurrent_first_insert(db_session):
    """Two provisioning calls racing on a fresh tenant: the loser used to get
    an uncaught IntegrityError and a bare 500 (review finding I9). Every other
    uniqueness race in this codebase is handled with a savepoint."""
    from memory.db import ensure_tenant
    from memory.models import Tenant

    ensure_tenant(db_session, "fresh-tenant")
    db_session.expire_all()
    # Simulate the loser: the row now exists but this session's check already
    # returned None, which is exactly the interleaving that raised.
    ensure_tenant(db_session, "fresh-tenant")

    assert db_session.get(Tenant, "fresh-tenant") is not None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest tests/test_ratelimit.py tests/test_db.py -v`

Expected: the ratelimit test FAILS with `len(admitted)` above 10.

- [ ] **Step 3: Lock the limiter**

In `src/memory/ratelimit.py`, add `import threading`, create the lock in
`__init__`, and wrap `check`'s body:

```python
        self._lock = threading.Lock()
```

```python
    def check(self, key: str) -> None:
        # One lock for the whole map, not per key: check() is a few
        # microseconds of deque work with no I/O inside it, so contention is
        # not worth per-key bookkeeping. Without it the read-check-append is a
        # TOCTOU -- sync routes run in Starlette's threadpool and MCP tools in
        # AnyIO's 40-thread pool, so 40 concurrent calls on one credential all
        # passed a limit of 10 (review finding I8). This is separate from the
        # per-replica multiplier documented on the class.
        with self._lock:
            ...  # existing body unchanged
```

- [ ] **Step 4: Give `ensure_tenant` the savepoint**

In `src/memory/db.py`:

```python
def ensure_tenant(db: Session, tenant_id: str) -> None:
    """Create the tenant row on first use. Mono-tenant in v1, so this fires
    once per deployment and then never again.

    The savepoint matches `create_user`, `create_group` and `projects.create`:
    two provisioning calls racing on a fresh tenant both saw None, both
    inserted, and the loser's IntegrityError escaped as a bare 500 (review
    finding I9). Losing the race is success here -- the row exists either way.
    """
    if db.get(Tenant, tenant_id) is not None:
        return
    try:
        with db.begin_nested():
            db.add(Tenant(id=tenant_id))
    except IntegrityError:
        pass
```

Add `from sqlalchemy.exc import IntegrityError` to the imports.

- [ ] **Step 5: Run the tests**

Run: `cd /home/coder/workspace/local/ach-memory && uv run pytest -m "not integration" -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add src/memory/ratelimit.py src/memory/db.py tests/test_ratelimit.py tests/test_db.py
git commit -m "fix(concurrency): lock the rate limiter and savepoint ensure_tenant"
```

---

### Task 11: Reconcile the docs with what shipped

`docs/PROJECT-STATE.md:17` still says the Admin API is "not written, not yet
scheduled into a plan" — it is 193 lines with 19 passing tests, and it is the
most security-sensitive surface in the service. The file's own opening tells
readers to trust it over their recollection, so this is the drift the project's
doc-hygiene rule exists to catch.

**Files:**
- Modify: `docs/PROJECT-STATE.md`
- Modify: `README.md`

- [ ] **Step 1: Fix the status table**

Change the Admin API row to complete, with the routes it ships and a pointer
to `tests/test_admin_api.py`.

- [ ] **Step 2: Record Plan 6's decisions**

Add a design-decision entry for the bank-configuration change: Memory Defense
dropped in favour of the LiteLLM `pre_mcp_call` guardrail,
`store_document_text` left at Hindsight's default, `append` supported, and the
`sensitive_data` / `private_key_pem` measurement from Task 1 Step 7.

- [ ] **Step 3: Update the README API table**

Add the three key-lifecycle routes from Task 4 and the locator repair from
Task 5.

- [ ] **Step 4: Commit**

```bash
cd /home/coder/workspace/local/ach-memory
git add docs/PROJECT-STATE.md README.md
git commit -m "docs: reconcile the ledger with the admin API and Plan 6"
```

---

## Final gate

Run all of it, in this order, and do not report the plan complete until every
line passes:

```bash
cd /home/coder/workspace/local/ach-memory
uv run ruff check src tests scripts
uv run pytest -m "not integration" -q
docker compose up -d --build
uv run pytest -m integration -q
./scripts/smoke.sh
uv run python scripts/mcp-smoke.py
uv run python scripts/e2e.py
```

The e2e must report every scenario passing, including the three added here
(`retain.append_accumulates_document_text`,
`keys.revocation_stops_authentication`,
`projects.poisoned_locator_can_be_repaired`).

## Deliberately NOT in this plan

Recorded so nobody assumes they were missed. The ten Minor findings from the
review stay in `.superpowers/sdd/progress.md` and are unclaimed:
`readOnlyHint=True` on eight tools that all write; `correct` having no content
size cap; `ToolResult(...)` sitting outside `_run`'s `try`; the exclusion test
asserting `REGISTRY` instead of the advertised `tools/list`; the literal-only
traversal guard that `%2F` passes; `rename` lacking `create`'s savepoint; the
admin slug-release route skipping `normalize_slug`; `get_document` with an id
ending in `/chunks`; and the mental-model traversal guard living at five call
sites instead of inside the path helper.

Also out of scope: the LiteLLM `pre_mcp_call` guardrail itself, which lives
outside this repository. Note that it is an input-side control — it screens
what enters on retain, and does nothing for text already stored or for a REST
caller that never traverses the MCP path.
