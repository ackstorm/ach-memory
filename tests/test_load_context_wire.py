"""load_context through the real SDK dispatcher, not a mocked session.

Regression for QA F-24 (2026-09-11): the tool returned a plain dict, the SDK
emitted no structuredContent, and the proxy's unwrapping found nothing.
"""

import json
from types import SimpleNamespace

import pytest

from memory.mcp import proxy


@pytest.mark.anyio
async def test_load_context_reply_carries_structured_content(app, client, session, tenant, new_user):
    """The real MCPServer.call_tool path: what a client's `result.structured_content` sees.

    `tests/test_mcp_tools.py::call_tool` invokes the registered function
    directly, which is why a plain-dict return looked fine there: the SDK's
    `convert_result` -- the step that decides whether structuredContent exists
    at all -- never ran.
    """
    from mcp.server.context import ServerRequestContext
    from mcp.server.mcpserver import Context

    from memory.mcp import context_tools
    from memory.mcp.server import build_mcp

    mcp = build_mcp()
    context_tools.register(mcp)
    # The transport's Context reads headers off `request_context.request`;
    # that is the only thing `tool_session` needs from it.
    ctx = Context(
        mcp_server=mcp,
        request_context=ServerRequestContext(
            session=None,
            lifespan_context={},
            protocol_version="2025-06-18",
            method="tools/call",
            request=SimpleNamespace(headers=new_user()["headers"]),
        ),
    )

    result = await mcp.call_tool("load_context", {"scope": "user"}, context=ctx)

    assert not result.is_error, result.content
    structured = result.structured_content
    assert isinstance(structured, dict), "load_context must return a BaseModel so the SDK emits structuredContent"
    assert isinstance(structured.get("result"), dict)
    assert isinstance(structured["result"].get("text"), str)


def _reply(*, structured, text):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(is_error=False, structured_content=structured, content=content)


def test_unwrap_accepts_the_toolresult_envelope():
    payload = {"text": "ctx", "omissions": []}
    reply = _reply(structured={"result": payload}, text=json.dumps({"result": payload}))
    assert proxy._unwrap_load_context(reply) == payload


def test_unwrap_accepts_a_bare_structured_payload():
    payload = {"text": "ctx", "omissions": []}
    assert proxy._unwrap_load_context(_reply(structured=payload, text=json.dumps(payload))) == payload


def test_unwrap_falls_back_to_the_json_text_block_a_0_7_1_server_sends():
    payload = {"text": "ctx", "omissions": []}
    assert proxy._unwrap_load_context(_reply(structured=None, text=json.dumps(payload))) == payload


def test_unwrap_returns_none_for_garbage():
    assert proxy._unwrap_load_context(_reply(structured=None, text="not json")) is None
    assert proxy._unwrap_load_context(_reply(structured=None, text=None)) is None
