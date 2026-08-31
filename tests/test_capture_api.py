import hashlib

import pytest

from memory.auth import keys

WS = "ws_" + "a" * 32


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


def _project(client, headers, slug: str = "acme-api"):
    return client.post("/v1/projects", json={"project_slug": slug}, headers=headers)


def _body(**overrides):
    content = overrides.pop("content", "user: hello there")
    fields = {
        "host": "claude-code",
        "session_id": "sess-1",
        "project_slug": "acme-api",
        "workspace_id": WS,
        "start_offset": 0,
        "end_offset": 100,
        "content_hash": "a" * 64,
        "sanitized_hash": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
    }
    fields.update(overrides)
    return fields


def _submit(client, headers, **overrides):
    return client.post("/v1/capture/checkpoints", json=_body(**overrides), headers=headers)


def test_submit_checkpoint_returns_202_with_a_pending_row(client, juan, tenant):
    _project(client, juan["headers"])

    response = _submit(client, juan["headers"])

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["duplicate"] is False
    assert body["checkpoint_seq"] == 100
    assert body["project_slug"] == "acme-api"
    assert body["resolved_from"] is None
    assert body["session_epoch"] >= 0


def test_a_duplicate_submission_returns_the_same_capture_id(client, juan, tenant):
    _project(client, juan["headers"])
    first = _submit(client, juan["headers"]).json()

    second = _submit(client, juan["headers"])

    assert second.status_code == 202
    body = second.json()
    assert body["duplicate"] is True
    assert body["capture_id"] == first["capture_id"]


def test_conflicting_offsets_are_a_409(client, juan, tenant):
    _project(client, juan["headers"])
    _submit(client, juan["headers"])

    response = _submit(client, juan["headers"], content_hash="f" * 64)

    assert response.status_code == 409


def test_a_sanitized_hash_that_does_not_match_content_is_rejected(client, juan, tenant):
    _project(client, juan["headers"])

    response = _submit(client, juan["headers"], sanitized_hash="0" * 64)

    assert response.status_code == 400


def test_oversized_content_is_rejected(client, juan, tenant, monkeypatch):
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    _project(client, juan["headers"])
    content = "x" * 1000

    response = _submit(
        client,
        juan["headers"],
        content=content,
        sanitized_hash=hashlib.sha256(content.encode()).hexdigest(),
    )

    assert response.status_code == 413


def test_a_master_credential_is_rejected(client, master_headers, tenant):
    response = client.post(
        "/v1/capture/checkpoints", json=_body(), headers=master_headers
    )

    assert response.status_code == 403


def test_an_unknown_project_is_not_found_and_creates_no_project(client, juan, tenant, session):
    from memory.models import Project

    before = session.query(Project).count()

    response = _submit(client, juan["headers"], project_slug="no-such-project")

    assert response.status_code == 404
    assert session.query(Project).count() == before


def test_a_retired_slug_returns_the_current_slug_and_rename_metadata(client, juan, tenant):
    _project(client, juan["headers"], slug="acme-api")
    rename = client.patch(
        "/v1/projects/acme-api", json={"project_slug": "acme-api-v2"}, headers=juan["headers"]
    )
    assert rename.status_code == 200, rename.text

    response = _submit(client, juan["headers"], project_slug="acme-api")

    assert response.status_code == 202
    body = response.json()
    assert body["project_slug"] == "acme-api-v2"
    assert body["resolved_from"] == "acme-api"


def test_a_malformed_workspace_id_is_a_422(client, juan, tenant):
    _project(client, juan["headers"])

    response = _submit(client, juan["headers"], workspace_id=WS + "\n")

    assert response.status_code == 422


def test_a_blank_session_id_is_a_422(client, juan, tenant):
    _project(client, juan["headers"])

    response = _submit(client, juan["headers"], session_id="   ")

    assert response.status_code == 422


def test_an_end_offset_not_after_start_offset_is_a_422(client, juan, tenant):
    _project(client, juan["headers"])

    response = _submit(client, juan["headers"], start_offset=100, end_offset=100)

    assert response.status_code == 422


def test_extra_forbid_rejects_a_misspelled_field(client, juan, tenant):
    _project(client, juan["headers"])
    body = _body()
    body["hsot"] = "claude-code"

    response = client.post("/v1/capture/checkpoints", json=body, headers=juan["headers"])

    assert response.status_code == 422


def test_another_tenant_cannot_submit(client, juan, tenant, session):
    """Same project_slug, a genuinely different tenant's own key."""
    from memory import ids
    from memory.models import ApiKey, Tenant, User

    _project(client, juan["headers"])

    other_tenant = "other-tenant"
    session.add(Tenant(id=other_tenant))
    other_user = User(
        id=ids.new_user_id(), tenant_id=other_tenant, bank_id=ids.new_user_bank_id()
    )
    session.add(other_user)
    session.flush()
    plaintext = keys.generate_key()
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=other_tenant,
            user_id=other_user.id,
            secret_hash=keys.hash_key(plaintext),
        )
    )
    session.commit()
    other_headers = {"Authorization": f"Bearer {plaintext}"}

    response = _submit(client, other_headers)

    assert response.status_code in (403, 404)
