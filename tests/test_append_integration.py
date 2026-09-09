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


@pytest.fixture
def live_client():
    import httpx

    with httpx.Client(base_url=API) as client:
        yield client


@pytest.fixture
def live_identity(live_client) -> str:
    """A fresh caller on the live stack, with nothing minted anywhere.

    There is no master key and no `POST /v1/users` to call: the token IS the
    identity (deploy/dev-identity/whoami.py, which the Compose stack wires
    through the ordinary platform provider), so a value nobody has used before
    is a person nobody has been before. `POST /v1/bootstrap` is what gives
    them a bank -- `link_identity` deliberately does not provision one on the
    authentication path, so without this the first retain has nowhere to go.
    """
    token = f"append-int-{uuid.uuid4().hex[:10]}"
    resp = live_client.post(
        "/v1/bootstrap", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    return token


def test_append_accumulates_document_text(live_client, live_identity):
    doc = "session:append-int"

    first = live_client.post(
        "/v1/memory/sync_retain",
        json={
            "scope": "user",
            "content": "the first line of the session",
            "document_id": doc,
            "update_mode": "replace",
        },
        headers={"Authorization": f"Bearer {live_identity}"},
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
        headers={"Authorization": f"Bearer {live_identity}"},
    )
    assert second.status_code == 200, second.text

    fetched = live_client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": doc},
        headers={"Authorization": f"Bearer {live_identity}"},
    )
    assert fetched.status_code == 200, fetched.text
    text = fetched.json()["result"].get("original_text") or ""
    assert "the first line of the session" in text
    assert "the second line of the session" in text
