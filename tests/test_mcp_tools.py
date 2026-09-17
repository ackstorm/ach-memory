"""The registered tools, exercised end-to-end through the MCP wire protocol."""

import asyncio
import contextlib
from uuid import uuid4

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from memory.auth.providers import platform
from memory.config import get_settings
from memory.mcp.server import create_app

_HEADER = "x-litellm-api-key"


@pytest.fixture(autouse=True)
def _schema(engine):
    """Force the session-scoped schema fixture to run before this module's tests."""
    return engine


@pytest.fixture(autouse=True)
def _platform_auth(monkeypatch):
    """Real platform-auth wiring, with the HTTP resolver call itself stubbed out: the
    incoming header's value becomes the resolved subject, one principal per token."""
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", _HEADER)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", _HEADER)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_URL", "http://resolver.test/whoami")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "groups")
    get_settings.cache_clear()
    monkeypatch.setattr(platform, "_resolve", lambda token: (f"user-{token}", frozenset()))
    yield
    get_settings.cache_clear()


@contextlib.asynccontextmanager
async def _session(app, headers=None):
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=headers or {})
    async with (
        client,
        streamable_http_client("http://127.0.0.1/mcp/", http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _auth_headers(token: str = "sk-1") -> dict[str, str]:
    return {_HEADER: token}


def _text(result) -> str:
    return result.content[0].text


def test_retain_then_recall_round_trips_with_project_created_notice():
    async def scenario():
        app = create_app()
        slug = f"acme/{uuid4().hex}"
        async with app.router.lifespan_context(app), _session(app, _auth_headers()) as session:
            retained = await session.call_tool(
                "retain",
                {
                    "scope": "project",
                    "project_slug": slug,
                    "content": "export retries three times before failing",
                    "memory_type": "convention",
                    "basis": "human_explicit",
                    "operation_id": str(uuid4()),
                    "wait": True,
                },
            )
            assert not retained.is_error
            assert retained.structured_content["notice"] == "PROJECT_CREATED"
            memory_id = retained.structured_content["result"]["memory_id"]

            # The fake backend scores by plain token overlap; querying with the retained
            # text itself clears the recall floor (SPEC I5) regardless of its exact value.
            recalled = await session.call_tool(
                "recall",
                {
                    "scope": "project",
                    "project_slug": slug,
                    "query": "export retries three times before failing",
                },
            )
            assert not recalled.is_error
            items = recalled.structured_content["result"]["items"]
            assert any(item["memory_id"] == memory_id for item in items)

    asyncio.run(scenario())


def test_unauthenticated_call_is_refused():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app), _session(app) as session:
            result = await session.call_tool("load_context", {})
            assert result.is_error
            # The SDK prefixes every tool error with "Error executing tool <name>: ";
            # what follows is our own code:message text (SPEC §3.5).
            assert _text(result).removeprefix("Error executing tool load_context: ").startswith(
                "UNAUTHORIZED"
            )

    asyncio.run(scenario())


def test_invalid_request_is_reported_as_such():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app), _session(app, _auth_headers()) as session:
            result = await session.call_tool(
                "retain",
                {
                    "scope": "user",
                    "content": "",
                    "memory_type": "fact",
                    "basis": "human_explicit",
                    "operation_id": str(uuid4()),
                },
            )
            assert result.is_error
            assert _text(result).removeprefix("Error executing tool retain: ").startswith(
                "INVALID_REQUEST"
            )

    asyncio.run(scenario())


def test_forget_then_list_invalid_memories_shows_it():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app), _session(app, _auth_headers()) as session:
            retained = await session.call_tool(
                "retain",
                {
                    "scope": "user",
                    "content": "the staging DB uses a read replica",
                    "memory_type": "fact",
                    "basis": "human_explicit",
                    "operation_id": str(uuid4()),
                    "wait": True,
                },
            )
            memory_id = retained.structured_content["result"]["memory_id"]

            forgotten = await session.call_tool(
                "forget", {"scope": "user", "memory_id": memory_id, "reason": "no longer true"}
            )
            assert not forgotten.is_error

            listed = await session.call_tool("list_memories", {"scope": "user", "state": "invalid"})
            assert not listed.is_error
            items = listed.structured_content["result"]["items"]
            assert any(item["memory_id"] == memory_id and item["state"] == "invalid" for item in items)

    asyncio.run(scenario())


def test_load_context_returns_text():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app), _session(app, _auth_headers()) as session:
            result = await session.call_tool("load_context", {})
            assert not result.is_error
            assert isinstance(result.structured_content["result"]["text"], str)

    asyncio.run(scenario())


def test_reflect_without_synthesis_is_unsupported():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app), _session(app, _auth_headers()) as session:
            result = await session.call_tool("reflect", {"scope": "user", "query": "anything"})
            assert result.is_error
            assert "UNSUPPORTED" in _text(result)

    asyncio.run(scenario())


def test_rename_project_forwards_the_old_slug_to_the_same_memories():
    async def scenario():
        app = create_app()
        old_slug = f"acme-{uuid4().hex}"
        new_slug = f"acme-renamed-{uuid4().hex}"
        async with app.router.lifespan_context(app), _session(app, _auth_headers()) as session:
            retained = await session.call_tool(
                "retain",
                {
                    "scope": "project", "project_slug": old_slug,
                    "content": "deploys are gated on a green pipeline",
                    "memory_type": "convention", "basis": "human_explicit",
                    "operation_id": str(uuid4()), "wait": True,
                },
            )
            memory_id = retained.structured_content["result"]["memory_id"]

            renamed = await session.call_tool(
                "rename_project", {"project_slug": old_slug, "new_slug": new_slug}
            )
            assert not renamed.is_error
            assert renamed.structured_content["result"]["project_slug"] == new_slug

            # The new slug reaches the same bank -- the memory was not orphaned.
            listed = await session.call_tool(
                "list_memories", {"scope": "project", "project_slug": new_slug}
            )
            assert any(item["memory_id"] == memory_id
                        for item in listed.structured_content["result"]["items"])

            # The old slug still resolves to the same bank, and a write through it
            # says so -- reads carry no notice field (only retain and curation do).
            forwarded = await session.call_tool(
                "list_memories", {"scope": "project", "project_slug": old_slug}
            )
            assert any(item["memory_id"] == memory_id
                        for item in forwarded.structured_content["result"]["items"])

            through_old = await session.call_tool(
                "retain",
                {
                    "scope": "project", "project_slug": old_slug,
                    "content": "rollbacks are gated on the same pipeline",
                    "memory_type": "convention", "basis": "human_explicit",
                    "operation_id": str(uuid4()), "wait": True,
                },
            )
            assert through_old.structured_content["notice"] == "PROJECT_RENAMED"

    asyncio.run(scenario())


def test_rename_project_refuses_a_slug_another_project_holds():
    async def scenario():
        app = create_app()
        mine = f"acme-{uuid4().hex}"
        theirs = f"acme-{uuid4().hex}"
        async with app.router.lifespan_context(app), _session(app, _auth_headers()) as session:
            for slug in (mine, theirs):
                await session.call_tool(
                    "retain",
                    {
                        "scope": "project", "project_slug": slug, "content": f"a fact about {slug}",
                        "memory_type": "fact", "basis": "human_explicit",
                        "operation_id": str(uuid4()), "wait": True,
                    },
                )

            result = await session.call_tool(
                "rename_project", {"project_slug": mine, "new_slug": theirs}
            )

            assert result.is_error
            assert "INVALID_REQUEST" in _text(result)

    asyncio.run(scenario())


def test_rename_project_denies_a_caller_who_does_not_own_it():
    async def scenario():
        app = create_app()
        slug = f"acme/{uuid4().hex}"
        async with app.router.lifespan_context(app):
            async with _session(app, _auth_headers("sk-owner")) as owner:
                await owner.call_tool(
                    "retain",
                    {
                        "scope": "project", "project_slug": slug, "content": "owned by the first toucher",
                        "memory_type": "fact", "basis": "human_explicit",
                        "operation_id": str(uuid4()), "wait": True,
                    },
                )
            async with _session(app, _auth_headers("sk-stranger")) as stranger:
                result = await stranger.call_tool(
                    "rename_project", {"project_slug": slug, "new_slug": f"acme/{uuid4().hex}"}
                )

            assert result.is_error
            assert "FORBIDDEN" in _text(result)

    asyncio.run(scenario())
