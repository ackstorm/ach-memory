import httpx
import pytest
import respx

from memory.models import ActivityEvent

BASE = "http://hindsight.test"


@pytest.fixture
def user_key(client, master_headers, tenant) -> tuple[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return user_id, key


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _mock_hindsight() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_a_retain_records_one_row(client, session, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True, "operation_id": "op-1"})
    )
    _, key = user_key

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "hello", "metadata": {"agent": "claude-code"}},
        headers=_headers(key),
    )

    row = session.query(ActivityEvent).one()
    assert (row.action, row.surface, row.scope, row.outcome) == (
        "memory.retain", "rest", "user", "ok",
    )
    assert row.content_bytes == len("hello")
    assert row.agent == "claude-code"
    assert row.bank_fingerprint and len(row.bank_fingerprint) == 12


@respx.mock
def test_an_upstream_failure_is_recorded_as_an_error(client, session, user_key, tenant):
    """The row must not claim the write landed. This is the whole reason the
    INSERT happens at the end instead of at resolution."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    _, key = user_key

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "hello"},
        headers=_headers(key),
    )

    row = session.query(ActivityEvent).one()
    assert (row.outcome, row.error_code) == ("error", "HINDSIGHT_ERROR")


def test_a_rejected_credential_records_no_row(client, session, tenant):
    client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "x"},
        headers={"Authorization": "Bearer mem_nope"},
    )

    assert session.query(ActivityEvent).count() == 0
