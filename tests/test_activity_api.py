import httpx
import pytest
import respx
from sqlalchemy import text

from memory.models import ActivityEvent
from tests.test_mcp_tools import _mock_bank, call_tool  # noqa: F401 -- reused as a fixture

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


@respx.mock
def test_an_mcp_tool_call_records_a_row(call_tool, session, tenant):  # noqa: F811
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    key = call_tool.make_user()

    call_tool("retain", key, scope="user", content="hello")

    row = session.query(ActivityEvent).one()
    assert (row.surface, row.action, row.outcome) == ("mcp", "memory.retain", "ok")


@respx.mock
def test_an_mcp_tool_error_is_recorded_with_its_code(call_tool, session, tenant):  # noqa: F811
    from memory.mcp.tools import MCPToolError

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    key = call_tool.make_user()

    with pytest.raises(MCPToolError):
        call_tool("recall", key, scope="user", query="x")

    row = session.query(ActivityEvent).one()
    assert row.outcome == "error"
    assert row.error_code


def test_activity_requires_the_master_key(client, user_key):
    _, key = user_key

    assert client.get("/v1/admin/activity", headers=_headers(key)).status_code == 403


def test_activity_lists_newest_first_and_filters_by_project(
    client, master_headers, seeded_activity
):
    body = client.get("/v1/admin/activity", headers=master_headers).json()

    # Exactly these two rows, not >=2: `seeded_activity` also inserts a row
    # for a second tenant, so this also pins tenant isolation -- relaxing it
    # to a >= or a subset check would let a missing tenant filter through.
    assert [r["action"] for r in body] == ["memory.recall", "memory.retain"]
    assert "bank_id" not in body[0]

    filtered = client.get(
        "/v1/admin/activity", params={"project_slug": "alpha"}, headers=master_headers
    ).json()
    assert {r["project_slug"] for r in filtered} == {"alpha"}


def test_an_unstorable_filter_is_an_empty_result_not_a_500(client, master_headers):
    response = client.get(
        "/v1/admin/activity", params={"action": "a\x00b"}, headers=master_headers
    )

    assert response.status_code == 200
    assert response.json() == []


def test_summary_rolls_up_per_bank(client, master_headers, seeded_activity, session):
    # SET LOCAL, not SET: its effect is scoped to the current transaction
    # (the one this test's connection is already in), so it cannot leak
    # into another test sharing that connection. A fractional-hour offset
    # (not a whole-hour one -- those agree with UTC at hour granularity)
    # is what actually makes the SQL bucket and Python's UTC `slots`
    # diverge under the old 2-arg date_trunc -- this is the proof for
    # activity.py's 3-arg fix, not decoration.
    session.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))

    body = client.get("/v1/admin/activity/summary", headers=master_headers).json()

    # Only the caller's tenant surfaces: `seeded_activity` also inserts a row
    # for a second tenant (project "beta"), and FleetRow's group key does not
    # include tenant_id, so a missing tenant filter on either of summary()'s
    # queries would leak it in here as an extra row rather than corrupt
    # `alpha`'s own counts.
    assert {r["project_slug"] for r in body} == {"alpha"}

    row = next(r for r in body if r["project_slug"] == "alpha")
    assert row["retains"] == 1
    assert row["calls"] == 2
    assert len(row["hours"]) == 24
    assert row["last_seen"]
    # Both seeded rows land in the current UTC hour -- the last slot. Fails
    # if the SQL bucket and the Python-side `slots` disagree on where an hour
    # starts (see activity.py's `bucket` comment).
    assert row["hours"][-1] >= 1
