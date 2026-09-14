"""The MCP transport: health, protocol negotiation, advertised tool set, host allowlist."""

import asyncio

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from memory.mcp.server import create_app

_EXPECTED_TOOLS = [
    "correct",
    "delete_memory",
    "forget",
    "get_memory",
    "get_operation",
    "history",
    "list_memories",
    "load_context",
    "recall",
    "reflect",
    "restore",
    "retain",
    "working_state_delete",
    "working_state_get",
    "working_state_put",
]


def test_health_returns_ok():
    async def scenario():
        transport = httpx2.ASGITransport(app=create_app())
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    asyncio.run(scenario())


def test_initialize_without_a_protocol_version_header_succeeds():
    """LiteLLM's client sends no `mcp-protocol-version` header on `initialize`; a raw
    request omitting it (never setting it, rather than trusting the SDK client not to)
    must still be accepted."""

    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                response = await client.post(
                    "/mcp/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                    headers={"accept": "application/json, text/event-stream"},
                )
                assert response.status_code == 200
                assert "error" not in response.json()

    asyncio.run(scenario())


def test_tools_list_returns_exactly_the_advertised_set():
    async def scenario():
        app = create_app()
        transport = httpx2.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client,
            streamable_http_client("http://127.0.0.1/mcp/", http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert sorted(t.name for t in listed.tools) == _EXPECTED_TOOLS

    asyncio.run(scenario())


def test_host_outside_the_allowlist_is_rejected():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://evil.example.com") as client:
                response = await client.post(
                    "/mcp/",
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    headers={"accept": "application/json, text/event-stream"},
                )
                assert response.status_code == 421

    asyncio.run(scenario())
