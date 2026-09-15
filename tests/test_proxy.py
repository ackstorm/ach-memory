"""The stdio bridge: header/context resolution, argument filling, and one live round-trip."""

import asyncio
import contextlib
import subprocess

import mcp_types as types
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from mcp.server.lowlevel import Server

from memory.mcp.proxy import (
    _build_bridge,
    auth_headers,
    call_load_context,
    fill_arguments,
    resolve_project_context,
)


def test_auth_headers_defaults_to_authorization_bearer(monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "sk-1")
    monkeypatch.delenv("ACH_MEMORY_HEADER", raising=False)
    assert auth_headers() == {"Authorization": "Bearer sk-1"}


def test_auth_headers_blank_header_falls_back_to_authorization(monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "sk-1")
    monkeypatch.setenv("ACH_MEMORY_HEADER", "  ")
    assert auth_headers() == {"Authorization": "Bearer sk-1"}


def test_auth_headers_custom_header_sends_the_bare_key(monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "sk-1")
    monkeypatch.setenv("ACH_MEMORY_HEADER", "x-litellm-api-key")
    assert auth_headers() == {"x-litellm-api-key": "sk-1"}


def _git_repo(tmp_path, origin: str | None):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if origin:
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", origin], check=True
        )
    return tmp_path


def test_memory_project_env_wins_over_git(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.setenv("MEMORY_PROJECT", "payments-api")
    assert resolve_project_context(str(repo)) == "payments-api"


def test_git_origin_becomes_a_derived_slug(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == "github.com-acme-payments-api-dab6719d"


def test_remote_that_names_no_repository_resolves_nothing(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, str(tmp_path / "bare-clone"))
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) is None


def test_repo_without_origin_resolves_nothing(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, None)
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) is None


def test_no_repo_resolves_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(tmp_path)) is None


def test_fill_arguments_injects_project_slug_when_scope_is_project():
    filled = fill_arguments("recall", {"scope": "project"}, "acme-1")
    assert filled == {"scope": "project", "project_slug": "acme-1"}


def test_fill_arguments_never_overwrites_a_caller_supplied_slug():
    filled = fill_arguments(
        "recall", {"scope": "project", "project_slug": "other"}, "acme-1"
    )
    assert filled["project_slug"] == "other"


def test_fill_arguments_ignores_non_project_scope():
    filled = fill_arguments("recall", {"scope": "user"}, "acme-1")
    assert "project_slug" not in filled


def test_fill_arguments_fills_load_context():
    filled = fill_arguments("load_context", {}, "acme-1")
    assert filled == {"project_slug": "acme-1"}


def test_fill_arguments_does_not_mutate_the_caller_dict():
    original = {"scope": "project"}
    fill_arguments("recall", original, "acme-1")
    assert original == {"scope": "project"}


async def _remote_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[types.Tool(name="recall", inputSchema={"type": "object", "properties": {}})]
    )


def test_bridge_lists_and_forwards_tool_calls_with_filled_project_slug():
    """One in-process round trip: host -> proxy Server -> remote Server, via the SDK's own
    in-memory transport (no real stdio, no real network)."""
    calls: list[dict] = []

    async def _remote_call_tool(ctx, params):
        calls.append(dict(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(text="hit")])

    remote = Server(
        "fake-remote", on_list_tools=_remote_list_tools, on_call_tool=_remote_call_tool
    )

    async def scenario():
        async with (
            InMemoryTransport(remote) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.discover()
            bridge = _build_bridge(session, "acme-1")

            async with (
                InMemoryTransport(bridge) as (h_read, h_write),
                ClientSession(h_read, h_write) as host,
            ):
                await host.discover()
                listed = await host.list_tools()
                assert [t.name for t in listed.tools] == ["recall"]

                result = await host.call_tool("recall", {"query": "x", "scope": "project"})
                assert result.content[0].text == "hit"

        assert calls == [{"query": "x", "scope": "project", "project_slug": "acme-1"}]

    asyncio.run(scenario())


def test_bridge_turns_a_remote_failure_into_a_tool_error_not_a_crash():
    async def _remote_call_tool(ctx, params):
        raise RuntimeError("remote is down")

    remote = Server(
        "fake-remote", on_list_tools=_remote_list_tools, on_call_tool=_remote_call_tool
    )

    async def scenario():
        async with (
            InMemoryTransport(remote) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.discover()
            bridge = _build_bridge(session, None)

            async with (
                InMemoryTransport(bridge) as (h_read, h_write),
                ClientSession(h_read, h_write) as host,
            ):
                await host.discover()
                result = await host.call_tool("recall", {})
                assert result.is_error

    asyncio.run(scenario())


class _DummyHttpClient:
    """Stands in for the httpx2 client `create_mcp_http_client` would build."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _stub_transport(monkeypatch, remote: Server, captured_headers: dict):
    """Stub the two SDK entry points call_load_context uses, backed by an in-process
    fake remote Server via InMemoryTransport -- one test with a stubbed ClientSession
    transport, per the task's fallback, since call_load_context hardcodes the URL-based
    streamable_http_client and offers no injection seam of its own."""
    import memory.mcp.proxy as proxy_module

    def fake_create_mcp_http_client(headers):
        captured_headers.update(headers)
        return _DummyHttpClient()

    @contextlib.asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client=None):
        async with InMemoryTransport(remote) as (read, write):
            yield read, write

    monkeypatch.setattr(proxy_module, "create_mcp_http_client", fake_create_mcp_http_client)
    monkeypatch.setattr(proxy_module, "streamable_http_client", fake_streamable_http_client)


def test_call_load_context_fills_arguments_and_sends_the_auth_header(monkeypatch):
    async def _remote_call_tool(ctx, params):
        assert params.arguments == {"project_slug": "acme-1"}
        return types.CallToolResult(content=[types.TextContent(text="{}")],
                                    structured_content={"result": {"text": "standing context"}})

    remote = Server(
        "fake-remote", on_list_tools=_remote_list_tools, on_call_tool=_remote_call_tool
    )
    captured: dict = {}
    _stub_transport(monkeypatch, remote, captured)
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "sk-1")
    monkeypatch.delenv("ACH_MEMORY_HEADER", raising=False)

    text = asyncio.run(call_load_context("http://x/mcp", "acme-1"))

    assert text == "standing context"
    assert captured == {"Authorization": "Bearer sk-1"}


def test_call_load_context_raises_on_a_remote_error(monkeypatch):
    async def _remote_call_tool(ctx, params):
        return types.CallToolResult(isError=True, content=[types.TextContent(text="boom")])

    remote = Server(
        "fake-remote", on_list_tools=_remote_list_tools, on_call_tool=_remote_call_tool
    )
    _stub_transport(monkeypatch, remote, {})
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "sk-1")

    with pytest.raises(RuntimeError):
        asyncio.run(call_load_context("http://x/mcp", None))
