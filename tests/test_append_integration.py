"""Append against the real engine.

SPEC §11.4 blesses `document_id` + `update_mode="append"` as the interactive
coding-session shape. Until Plan 6 it could never succeed, because the wrapper
set `store_document_text: false` and hindsight-api rejects appends against
that. Nothing but a live run can catch that class of defect: the unit test
mocked a 200 and stayed green throughout.
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.integration

API = os.environ.get("API", "http://localhost:8000")
# No default. scripts/smoke.sh and scripts/e2e.py both refuse to run without an
# explicit MEMORY_MASTER_KEY; this test silently fell back to the literal the
# README used to publish, which meant it would happily run against a stack
# still using that compromised key. Checked in the fixture, not at import, so
# collection still works when this module is deselected.
MASTER = os.environ.get("MEMORY_MASTER_KEY")


@pytest.fixture
def live_client():
    import httpx

    if not MASTER:
        pytest.fail("set MEMORY_MASTER_KEY to the plaintext master key")

    with httpx.Client(base_url=API) as client:
        yield client


@pytest.fixture
def live_user_key(live_client) -> str:
    user_id = f"append-int-{uuid.uuid4().hex[:10]}"
    resp = live_client.post(
        "/v1/users", json={"id": user_id}, headers={"Authorization": f"Bearer {MASTER}"}
    )
    assert resp.status_code == 201, resp.text
    resp = live_client.post(
        f"/v1/users/{user_id}/keys",
        json={},
        headers={"Authorization": f"Bearer {MASTER}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


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
