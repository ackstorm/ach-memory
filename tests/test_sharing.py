"""Sharing a project, exercised through the MCP wire protocol."""

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
# token -> (subject, groups)
_PEOPLE = {
    "sk-juan": ("juan@acme.test", frozenset({"dream"})),
    "sk-joan": ("joan@acme.test", frozenset()),
    "sk-ana": ("ana@acme.test", frozenset({"dream"})),
    "sk-eve": ("eve@acme.test", frozenset()),
}


@pytest.fixture(autouse=True)
def _schema(engine):
    return engine


@pytest.fixture(autouse=True)
def _platform_auth(monkeypatch):
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", _HEADER)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", _HEADER)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_URL", "http://resolver.test/whoami")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "groups")
    get_settings.cache_clear()
    monkeypatch.setattr(platform, "_resolve", lambda token: _PEOPLE[token])
    yield
    get_settings.cache_clear()


@contextlib.asynccontextmanager
async def _as(app, token):
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers={_HEADER: token})
    async with (
        client,
        streamable_http_client("http://127.0.0.1/mcp/", http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _claim(scope, content, slug=None):
    out = {"scope": scope, "title": "A title", "content": content, "memory_type": "fact",
           "basis": "human_explicit", "operation_id": str(uuid4()), "wait": True}
    if slug:
        out["project_slug"] = slug
    return out


def _result(response):
    return response.structured_content["result"]


def test_whoami_returns_subject_and_groups():
    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app), _as(app, "sk-juan") as juan:
            return _result(await juan.call_tool("whoami", {}))

    assert asyncio.run(scenario()) == {"subject": "juan@acme.test", "groups": ["dream"], "operator": False}


def test_a_shared_user_and_a_shared_group_reach_the_project_and_a_stranger_does_not():
    slug = f"acme-{uuid4().hex[:8]}"

    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            async with _as(app, "sk-juan") as juan:
                await juan.call_tool("retain", _claim("project", "deploys run on fridays", slug))
                shared = _result(await juan.call_tool(
                    "share_project", {"project_slug": slug, "users": ["Joan@Acme.test"], "groups": ["dream"]}))
            async with _as(app, "sk-joan") as joan:
                written = await joan.call_tool("retain", _claim("project", "joan adds a claim", slug))
                listed = _result(await joan.call_tool("list_access", {"project_slug": slug}))
            async with _as(app, "sk-ana") as ana:
                read = await ana.call_tool("list_memories", {"scope": "project", "project_slug": slug})
            async with _as(app, "sk-eve") as eve:
                refused = await eve.call_tool("list_access", {"project_slug": slug})
        return shared, written, listed, read, refused

    shared, written, listed, read, refused = asyncio.run(scenario())
    assert shared == {"users": ["joan@acme.test", "juan@acme.test"], "groups": ["dream"]}
    assert not written.is_error
    assert listed == shared
    assert len(_result(read)["items"]) == 2
    assert refused.is_error and "FORBIDDEN" in refused.content[0].text


def test_a_co_owner_removes_the_creator_but_not_the_last_one():
    slug = f"acme-{uuid4().hex[:8]}"

    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            async with _as(app, "sk-juan") as juan:
                await juan.call_tool("retain", _claim("project", "a claim", slug))
                await juan.call_tool("share_project", {"project_slug": slug, "users": ["joan@acme.test"]})
            async with _as(app, "sk-joan") as joan:
                removed = _result(await joan.call_tool(
                    "unshare_project", {"project_slug": slug, "users": ["juan@acme.test"]}))
                last = await joan.call_tool("unshare_project", {"project_slug": slug, "users": ["joan@acme.test"]})
            async with _as(app, "sk-juan") as juan:
                gone = await juan.call_tool("list_access", {"project_slug": slug})
        return removed, last, gone

    removed, last, gone = asyncio.run(scenario())
    assert removed == {"users": ["joan@acme.test"], "groups": []}
    assert last.is_error and "INVALID_REQUEST" in last.content[0].text
    assert gone.is_error and "FORBIDDEN" in gone.content[0].text


def test_sharing_a_group_the_caller_is_not_in_is_refused():
    slug = f"acme-{uuid4().hex[:8]}"

    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app), _as(app, "sk-juan") as juan:
            await juan.call_tool("retain", _claim("project", "a claim", slug))
            return await juan.call_tool("share_project", {"project_slug": slug, "groups": ["run"]})

    refused = asyncio.run(scenario())
    assert refused.is_error and "INVALID_REQUEST" in refused.content[0].text and "run" in refused.content[0].text


def test_personal_memory_stays_private_after_sharing_a_project():
    slug = f"acme-{uuid4().hex[:8]}"

    async def scenario():
        app = create_app()
        async with app.router.lifespan_context(app):
            async with _as(app, "sk-juan") as juan:
                await juan.call_tool("retain", _claim("project", "a project claim", slug))
                await juan.call_tool("retain", _claim("user", "juan's private preference"))
                await juan.call_tool("share_project", {"project_slug": slug, "users": ["joan@acme.test"]})
            async with _as(app, "sk-joan") as joan:
                return _result(await joan.call_tool("list_memories", {"scope": "user"}))["items"]

    assert asyncio.run(scenario()) == []
