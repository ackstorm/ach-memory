"""The MCP surface must not tell the calling model things that are untrue.

readOnlyHint is defined as "the tool does not modify its environment" and is
what clients use to skip confirmation and auto-approve inside an agent loop.
`recall`/`reflect` claimed it while running with create=True, which mints a
Project row per unseen slug -- permanently, since invariant 8 makes a slug
unique across live AND retired names (measured live at 80 projects in 5.1s).

Separately the SDK derives each tool's advertised JSON Schema from the
function SIGNATURE, so bounds living only on the pydantic models never reached
the model calling the tool: SPEC §11.4 blesses update_mode="append" and
nothing in the schema said it existed.
"""

from typing import ClassVar

import pytest


def _manager():
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    return mcp._tool_manager


# tool -> may it advertise readOnlyHint?
READONLY = {
    "retain": False, "sync_retain": False, "reflect": False,
    "recall": True, "memory_history": True,
    "list_memories": True, "get_memory": True, "forget": False, "correct": False,
    "restore": False, "list_documents": True, "get_document": True,
    "delete_document": False, "get_operation": True, "list_operations": True,
    "cancel_operation": False, "start_working_session": False,
    "set_working_state": False,
}

WORKING_STATE_TOOLS = {"start_working_session", "set_working_state"}


def test_product_registrars_own_disjoint_tool_sets():
    """A product move must not leave either registrar owning the other's tools."""
    from memory.mcp import memory_tools, working_state_tools
    from memory.mcp.server import build_mcp

    memory_mcp = build_mcp()
    memory_tools.register(memory_mcp)
    memory_names = {tool.name for tool in memory_mcp._tool_manager.list_tools()}

    state_mcp = build_mcp()
    working_state_tools.register(state_mcp)
    state_names = {tool.name for tool in state_mcp._tool_manager.list_tools()}

    assert memory_names == set(READONLY) - WORKING_STATE_TOOLS
    assert state_names == WORKING_STATE_TOOLS
    assert memory_names.isdisjoint(state_names)


def test_the_readonly_table_covers_every_registered_tool():
    """An eighteenth tool landing without a row here must fail loudly."""
    from memory.mcp.tools import REGISTRY

    _manager()  # registers the tools, which is what populates REGISTRY
    assert set(READONLY) == set(REGISTRY)


@pytest.mark.parametrize("name", sorted(READONLY))
def test_no_tool_claims_readonly_while_it_creates_or_writes(name):
    from tests.test_mcp_tools import MCP_CREATE_TABLE, MCP_IS_WRITE_TABLE

    # snake_case on the SDK's ToolAnnotations, not the wire's camelCase --
    # tests/test_mcp_tools.py:609 uses the same spelling.
    ann = _manager().get_tool(name).annotations
    advertised = bool(ann and getattr(ann, "read_only_hint", None))
    assert advertised == READONLY[name], f"{name}: readOnlyHint={advertised}"
    if advertised:
        assert not MCP_CREATE_TABLE[name], f"{name} creates but claims read-only"
        assert not MCP_IS_WRITE_TABLE[name], f"{name} writes but claims read-only"


def test_the_advertised_schema_carries_the_vocabulary_the_models_enforce():
    mgr = _manager()
    schemas = {n: mgr.get_tool(n).parameters for n in READONLY}

    state = schemas["list_memories"]["properties"]["state"]
    assert "valid" in str(state) and "invalidated" in str(state), state

    for tool in ("list_memories", "list_documents", "list_operations"):
        limit = schemas[tool]["properties"]["limit"]
        assert "500" in str(limit), (tool, limit)


@pytest.mark.parametrize("name", ["retain", "sync_retain"])
def test_retain_exposes_type_basis_trigger_expiry_and_evidence(name):
    """v0.4.0's typed retain contract: the calling model must see the real
    required vocabulary, and no trace of the removed metadata/update_mode
    channel."""
    mgr = _manager()
    properties = mgr.get_tool(name).parameters["properties"]

    assert {"memory_type", "basis", "trigger", "valid_until", "evidence"} <= properties.keys()
    assert "metadata" not in properties
    assert "update_mode" not in properties


@pytest.mark.parametrize("name", ["retain", "sync_retain"])
def test_retain_tools_advertise_that_they_capture_evidence_not_profile_truth(name):
    """SPEC Phase 3: explicit retain is for an explicit human "remember
    this" request and captures evidence, not guaranteed profile truth --
    only the automatic capture pipeline classifies and promotes candidates.
    The advertised description is the only place a calling model learns
    that distinction."""
    mgr = _manager()
    description = (mgr.get_tool(name).description or "").lower()

    assert "evidence" in description


def test_working_state_schemas_advertise_the_bounds_the_models_enforce():
    """Phase 2 review finding #2: WorkingStateWrite/StartSessionRequest
    enforced these bounds at runtime already, but the two tool functions took
    bare `str`/`int`/`list[str]` -- the SDK's advertised schema (built from
    the function SIGNATURE) never showed a calling model any of it.
    """
    mgr = _manager()
    start = mgr.get_tool("start_working_session").parameters["properties"]
    write = mgr.get_tool("set_working_state").parameters["properties"]

    workspace_schema = str(start["workspace_id"])
    assert "pattern" in workspace_schema and "ws_" in workspace_schema

    session_schema = str(start["session_id"])
    assert "minLength" in session_schema
    assert "maxLength" in session_schema and "128" in session_schema

    assert "minimum" in str(write["session_epoch"])
    assert "minimum" in str(write["checkpoint_seq"])

    objective_schema = str(write["objective"])
    assert "maxLength" in objective_schema and "512" in objective_schema

    list_schema = str(write["recent_decisions"])
    assert "maxItems" in list_schema and "10" in list_schema
    assert "maxLength" in list_schema and "512" in list_schema


def test_a_malformed_upstream_body_is_internal_error_not_invalid_request(
    client, master_headers, tenant
):
    """SPEC §18 defines INVALID_REQUEST as input that "failed validation before
    anything was resolved or written". By the time ToolResult is built the bank
    is resolved, the row is committed and the upstream call has happened -- so
    a non-object upstream 200 is a backend fault, not a caller mistake.
    Reporting it as INVALID_REQUEST would blame the caller for Hindsight's
    response shape.
    """
    import httpx
    import respx

    from memory.mcp.tools import REGISTRY

    uid = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    secret = client.post(
        f"/v1/users/{uid}/keys", json={}, headers=master_headers
    ).json()["key"]

    class Ctx:
        headers: ClassVar[dict[str, str]] = {"Authorization": f"Bearer {secret}"}

    with respx.mock:
        # A JSON array, not an object -- ToolResult.result is dict[str, Any].
        respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
            return_value=httpx.Response(200, json=["not", "an", "object"])
        )
        result = REGISTRY["recall"](scope="user", query="x", ctx=Ctx())

    assert result.result["hits"] == ()
    assert "an object" not in str(result)
