import httpx
import pytest
import respx
from sqlalchemy import text

from memory.models import ActivityEvent

BASE = "http://hindsight.test"


def _retain_body(**overrides) -> dict:
    import uuid

    body = {
        "scope": "user",
        "content": "hello",
        "memory_type": "fact",
        "basis": "human_explicit",
        "trigger": "agent_proactive",
        "evidence": [{"kind": "user_quote", "raw": "hello"}],
        "operation_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


@pytest.fixture
def juan(new_user) -> dict:
    """An ordinary (non-operator) external user, bank pre-warmed. Nothing mints
    a user any more, so there is no key here and no route that would hand one
    back -- only the identity headers an IdP-authenticated caller sends."""
    return new_user()


def _retain_kwargs(**overrides) -> dict:
    """The minimum typed-retain shape an MCP retain call needs."""
    kwargs = {
        "memory_type": "fact",
        "basis": "human_explicit",
        "trigger": "agent_proactive",
        "evidence": [{"kind": "user_quote", "raw": "hello"}],
    }
    kwargs.update(overrides)
    return kwargs


def _call_tool(tool_name: str, headers: dict[str, str], **kwargs):
    """Invoke a registered MCP tool the way the SDK would, with real headers.

    Deliberately local rather than imported from tests/test_mcp_tools.py: the
    two MCP assertions here are about the ACTIVITY row, and a shared harness
    would couple this file to that one's fixture shape for no gain.
    """
    from memory.mcp import tools as tool_module

    ctx = type("Ctx", (), {"headers": dict(headers)})()
    return tool_module.REGISTRY[tool_name](ctx=ctx, **kwargs)


def _mock_hindsight() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_a_retain_records_one_row(client, session, juan, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )

    client.post("/v1/memory/retain", json=_retain_body(), headers=juan["headers"])

    row = session.query(ActivityEvent).one()
    assert (row.action, row.surface, row.scope, row.outcome) == (
        "memory.retain", "rest", "user", "ok",
    )
    assert row.content_bytes == len("hello")
    assert row.bank_fingerprint and len(row.bank_fingerprint) == 12


@respx.mock
def test_an_upstream_failure_is_recorded_as_an_error(client, session, juan, tenant):
    """The row must not claim the write landed. This is the whole reason the
    INSERT happens at the end instead of at resolution."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    client.post("/v1/memory/retain", json=_retain_body(), headers=juan["headers"])

    row = session.query(ActivityEvent).one()
    assert (row.outcome, row.error_code) == ("error", "HINDSIGHT_ERROR")


def test_a_rejected_credential_records_no_row(client, session, tenant):
    """No provider accepts this token -- ach-memory mints none of its own, so
    the refusal is now "nobody issued you", not "unknown key". Either way an
    unauthenticated call must leave no activity row."""
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "x"},
        headers={"Authorization": "Bearer not-a-token-any-issuer-minted"},
    )

    assert response.status_code == 401
    assert session.query(ActivityEvent).count() == 0


@respx.mock
def test_an_mcp_tool_call_records_a_row(client, new_user, session, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    headers = new_user()["headers"]

    _call_tool("retain", headers, scope="user", content="hello", **_retain_kwargs())

    row = session.query(ActivityEvent).one()
    assert (row.surface, row.action, row.outcome) == ("mcp", "memory.retain", "ok")


@respx.mock
def test_an_mcp_tool_error_is_recorded_with_its_code(client, new_user, session, tenant):
    from memory.mcp.tools import MCPToolError

    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    headers = new_user()["headers"]

    with pytest.raises(MCPToolError):
        _call_tool("recall", headers, scope="user", query="x")

    row = session.query(ActivityEvent).one()
    assert row.outcome == "error"
    assert row.error_code


def test_activity_requires_operator_authority(client, juan, master_headers):
    """Authority is configuration over an external identity now, not a
    credential: `juan` is an ordinary user the IdP asserts and
    MEMORY_MASTER_USERS does not name, so the operator plane is closed to
    them -- and open to the subject that IS named."""
    assert client.get("/v1/admin/activity", headers=juan["headers"]).status_code == 403
    assert client.get("/v1/admin/activity", headers=master_headers).status_code == 200


def test_a_group_named_in_master_groups_grants_the_operator_plane(
    client, new_user, tenant, monkeypatch
):
    """The MEMORY_MASTER_GROUPS half of the same rule: authority can arrive
    through a group the IdP asserts, with nothing about the user configured.
    Membership is re-read from the token on every request, so the IdP dropping
    the group closes this plane on the very next call."""
    from memory.config import get_settings

    outsider = new_user(groups=("sre",))
    assert client.get("/v1/admin/activity", headers=outsider["headers"]).status_code == 403

    monkeypatch.setenv("MEMORY_MASTER_GROUPS", "sre")
    get_settings.cache_clear()

    assert client.get("/v1/admin/activity", headers=outsider["headers"]).status_code == 200


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


def test_an_unhandled_500_is_recorded_as_an_error(client, session, juan, tenant, monkeypatch):
    """Starlette puts ServerErrorMiddleware -- which owns the catch-all
    `@app.exception_handler(Exception)` -- OUTSIDE user middleware, so that
    handler runs AFTER ObservabilityMiddleware's `finally`. Its set_error()
    therefore landed on a record already written and cleared, and every
    unhandled 500 was recorded as outcome="ok" with a NULL error_code: the one
    outcome an operator most needs to see, reported as success."""
    from memory.api import memory as memory_routes

    def _boom(*_args, **_kwargs):
        raise RuntimeError("something internal broke")

    # No respx mock, and none needed: submit_retain raises before any
    # upstream call. Calling _mock_hindsight() here without @respx.mock
    # registered a PUT on the GLOBAL respx router and leaked a 200 into
    # every later test file -- which made ensure_bank's 500 unreachable and
    # broke three tests in test_hindsight_client.py.
    monkeypatch.setattr(memory_routes, "submit_retain", _boom)

    response = client.post(
        "/v1/memory/retain", json=_retain_body(), headers=juan["headers"]
    )

    assert response.status_code == 500
    row = session.query(ActivityEvent).one()
    assert (row.outcome, row.error_code) == ("error", "INTERNAL_ERROR")
