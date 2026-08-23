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
    "retain": False, "sync_retain": False, "recall": False, "reflect": False,
    "list_memories": True, "get_memory": True, "forget": False, "correct": False,
    "restore": False, "list_documents": True, "get_document": True,
    "delete_document": False, "get_operation": True, "list_operations": True,
    "cancel_operation": False,
}


def test_the_readonly_table_covers_every_registered_tool():
    """A sixteenth tool landing without a row here must fail loudly."""
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

    retain = schemas["retain"]["properties"]["update_mode"]
    assert "append" in str(retain), retain
    assert "replace" in str(retain), retain

    state = schemas["list_memories"]["properties"]["state"]
    assert "valid" in str(state) and "invalidated" in str(state), state

    for tool in ("list_memories", "list_documents", "list_operations"):
        limit = schemas[tool]["properties"]["limit"]
        assert "500" in str(limit), (tool, limit)


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

    from memory.mcp.tools import REGISTRY, MCPToolError

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
        with pytest.raises(MCPToolError) as excinfo:
            REGISTRY["recall"](scope="user", query="x", ctx=Ctx())

    assert excinfo.value.code == "INTERNAL_ERROR", excinfo.value.code
    assert "not" not in str(excinfo.value), "the upstream payload leaked to the caller"
