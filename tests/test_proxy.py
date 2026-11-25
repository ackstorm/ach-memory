"""The stdio bridge: header/context resolution, argument filling, and one live round-trip."""

import asyncio
import contextlib
import subprocess

import httpx2
import mcp_types as types
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from mcp.server.lowlevel import Server

from memory.mcp import proxy
from memory.mcp.proxy import (
    TOOL_NAMES,
    FreshCredential,
    _build_bridge,
    _handshake,
    api_key,
    auth_headers,
    call_load_context,
    canonical_tool_name,
    fill_arguments,
    find_marker,
    personal_tools,
    repo_slug_spellings,
    resolve_project_context,
    retain_stamp,
    with_session,
    without_session,
)


@pytest.fixture(autouse=True)
def _no_token_helper(monkeypatch):
    """A developer shell running the token-helper setup must not leak into tests."""
    monkeypatch.delenv("ACH_MEMORY_TOKEN_COMMAND", raising=False)


@pytest.fixture(autouse=True)
def _host_session(monkeypatch):
    """A fixed host session, whatever host runs the suite."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ses-test")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)


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


def test_api_key_prefers_the_token_command_over_the_static_key(monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "static")
    monkeypatch.setenv("ACH_MEMORY_TOKEN_COMMAND", "echo '  fresh-jwt  '")
    assert api_key() == "fresh-jwt"
    monkeypatch.setenv("ACH_MEMORY_TOKEN_COMMAND", "  ")
    assert api_key() == "static"


def test_api_key_is_empty_when_the_token_command_fails(monkeypatch):
    """A failed helper must not fall back to a stale static key."""
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "static")
    monkeypatch.setenv("ACH_MEMORY_TOKEN_COMMAND", "echo partial; exit 3")
    assert api_key() == ""


def _ach_cli(tmp_path, monkeypatch, profile_show: str) -> None:
    cli = tmp_path / "ach-cli"
    cli.write_text(f'#!/bin/sh\n[ "$1" = token ] && echo jwt-1 && exit 0\n{profile_show}\n')
    cli.chmod(0o755)
    monkeypatch.setattr(proxy, "which", lambda name: str(cli))
    monkeypatch.setenv("ACH_MEMORY_TOKEN_COMMAND", "")  # restored after the test


def test_endpoint_is_the_configured_url_even_with_ach_cli_installed(tmp_path, monkeypatch):
    _ach_cli(tmp_path, monkeypatch, """echo '{"url": "https://hub.example"}'""")
    monkeypatch.setenv("ACH_MEMORY_URL", "https://mine.example/mcp/")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "static")
    assert proxy.endpoint() == "https://mine.example/mcp/"
    assert api_key() == "static"


def test_endpoint_without_a_url_is_the_hub_ach_cli_is_signed_in_to(tmp_path, monkeypatch):
    """A plugin installed by ACH works with nothing in the environment: the Hub of ach-cli's
    default profile, and `ach-cli token` as the credential."""
    _ach_cli(tmp_path, monkeypatch, """echo '{"name": "default", "url": "https://hub.example/"}'""")
    monkeypatch.setenv("ACH_MEMORY_URL", "")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "static")
    assert proxy.endpoint() == "https://hub.example/mcp/ach-memory"
    assert api_key() == "jwt-1"


def test_endpoint_without_a_url_or_an_ach_profile_is_the_local_stack(tmp_path, monkeypatch):
    monkeypatch.delenv("ACH_MEMORY_URL", raising=False)
    assert proxy.endpoint() == "http://localhost:8000/mcp/"
    _ach_cli(tmp_path, monkeypatch, "echo 'no profile' >&2; exit 1")
    assert proxy.endpoint() == "http://localhost:8000/mcp/"
    assert api_key() == ""


def test_fresh_credential_asks_the_helper_on_every_request(tmp_path, monkeypatch):
    """Short-lived tokens: each request carries what the helper prints now."""
    counter = tmp_path / "n"
    counter.write_text("0")
    monkeypatch.delenv("ACH_MEMORY_HEADER", raising=False)
    monkeypatch.setenv(
        "ACH_MEMORY_TOKEN_COMMAND",
        f"n=$(($(cat {counter}) + 1)); echo $n > {counter}; echo t$n",
    )
    auth = FreshCredential()
    seen = [
        next(auth.auth_flow(httpx2.Request("POST", "http://x/mcp"))).headers["authorization"]
        for _ in range(2)
    ]
    assert seen == ["Bearer t1", "Bearer t2"]


def test_handshake_falls_back_to_discover_when_initialize_is_refused():
    """`initialize` first: LiteLLM's MCP gateway answers `server/discover` with -32603."""
    from mcp.shared.exceptions import MCPError

    steps = []

    class Session:
        async def initialize(self):
            steps.append("initialize")
            raise MCPError(code=-32600, message="initialize not supported")

        async def discover(self):
            steps.append("discover")

    asyncio.run(_handshake(Session()))
    assert steps == ["initialize", "discover"]


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


def _marker(folder, text='project = "Design MD2"\n'):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".ach-memory").write_text(text)


def test_a_marker_names_the_project_of_a_folder_without_origin(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    _marker(tmp_path / "docs")
    (tmp_path / "docs" / "deep" / "er").mkdir(parents=True)
    assert resolve_project_context(str(tmp_path / "docs" / "deep" / "er")) == "design-md2"
    assert resolve_project_context(str(tmp_path)) is None


def test_a_marker_counts_in_a_repo_without_origin_and_never_against_one(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    _marker(tmp_path / "a")
    _marker(tmp_path / "b")
    assert resolve_project_context(str(_git_repo(tmp_path / "a", None))) == "design-md2"
    repo = _git_repo(tmp_path / "b", "git@github.com:acme/payments-api.git")
    assert resolve_project_context(str(repo)) == "github.com-acme-payments-api-dab6719d"
    monkeypatch.setenv("MEMORY_PROJECT", "from-env")
    assert resolve_project_context(str(tmp_path / "a")) == "from-env"


def test_the_marker_search_stops_below_home(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    _marker(tmp_path / "home")
    (tmp_path / "home" / "work").mkdir()
    assert find_marker(str(tmp_path / "home" / "work")) is None
    assert find_marker(str(tmp_path / "home")) is None


@pytest.mark.parametrize("text", ["project = 3\n", "not toml at all", "other = \"x\"\n", 'project = "--"\n'])
def test_an_unreadable_marker_resolves_nothing(tmp_path, monkeypatch, text):
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    _marker(tmp_path, text)
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


def test_fill_arguments_fills_a_memory_id_call_without_scope():
    filled = fill_arguments("correct", {"memory_id": "mem_1"}, "acme-1")
    assert filled == {"memory_id": "mem_1", "project_slug": "acme-1"}


def test_fill_arguments_leaves_scopeless_calls_without_memory_id_alone():
    assert fill_arguments("rename_project", {"new_slug": "b"}, "acme-1") == {"new_slug": "b"}


def test_fill_arguments_does_not_mutate_the_caller_dict():
    original = {"scope": "project"}
    fill_arguments("recall", original, "acme-1")
    assert original == {"scope": "project"}


def test_personal_tools_drop_scope_project_slug_and_rename_project():
    schema = {
        "type": "object",
        "properties": {"scope": {}, "project_slug": {}, "query": {}},
        "required": ["scope", "query"],
    }
    tools = [
        types.Tool(name="recall", description="mentions project", inputSchema=schema),
        types.Tool(name="rename_project", inputSchema=schema),
    ]
    (recall,) = personal_tools(tools)
    assert recall.name == "recall"
    assert list(recall.input_schema["properties"]) == ["query"]
    assert recall.input_schema["required"] == ["query"]
    assert "project" not in recall.description


async def _remote_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[types.Tool(name="recall", inputSchema={"type": "object", "properties": {}})]
    )


def test_bridge_does_not_demand_the_project_slug_it_fills():
    """OpenCode and pi check `required` before calling: a slug the proxy fills must not be required."""
    families = {"type": "object", "properties": {"project_slug": {"type": "string"}}, "required": ["project_slug"]}
    renamed = {
        "type": "object",
        "properties": {"project_slug": {"type": "string"}, "new_slug": {"type": "string"}},
        "required": ["project_slug", "new_slug"],
    }

    async def _list(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name="list_families", inputSchema=families),
            types.Tool(name="rename_project", inputSchema=renamed),
        ])

    async def scenario():
        async with (
            InMemoryTransport(Server("fake-remote", on_list_tools=_list)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.discover()
            bridge = _build_bridge(session, "acme-1")
            async with (
                InMemoryTransport(bridge) as (h_read, h_write),
                ClientSession(h_read, h_write) as host,
            ):
                await host.discover()
                return {t.name: t.input_schema for t in (await host.list_tools()).tools}

    listed = asyncio.run(scenario())
    assert listed["list_families"]["required"] == []
    assert "project_slug" in listed["list_families"]["properties"]
    assert listed["rename_project"]["required"] == ["project_slug", "new_slug"]  # names a project: never filled


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

        assert calls == [{"query": "x", "scope": "project", "project_slug": "acme-1", "session_id": "ses-test"}]

    asyncio.run(scenario())


def test_bridge_stamps_a_successful_retain_and_nothing_else(tmp_path, monkeypatch):
    """idle-nudge.sh reads this stamp; a failed retain or a recall must not reset its clock."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    stamp = retain_stamp()
    assert stamp.parent == tmp_path / "ach-memory"
    assert stamp.name == "retain-" + str(tmp_path).replace("/", "_")

    async def _remote_call_tool(ctx, params):
        if params.arguments.get("fail"):
            return types.CallToolResult(isError=True, content=[types.TextContent(text="no")])
        return types.CallToolResult(content=[types.TextContent(text="ok")])

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
                await host.call_tool("recall", {})
                assert not stamp.exists()
                await host.call_tool("retain", {"fail": True})
                assert not stamp.exists()
                await host.call_tool("retain", {})
                assert stamp.read_text().isdigit()

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

    def fake_create_mcp_http_client(*, auth, timeout=None):
        request = next(auth.auth_flow(httpx2.Request("POST", "http://x/mcp")))
        captured_headers.update(
            {k: v for k, v in request.headers.items() if k.lower() == "authorization"}
        )
        return _DummyHttpClient()

    @contextlib.asynccontextmanager
    async def fake_streamable_http_client(url, *, http_client=None, terminate_on_close=True):
        async with InMemoryTransport(remote) as (read, write):
            yield read, write

    monkeypatch.setattr(proxy_module, "create_mcp_http_client", fake_create_mcp_http_client)
    monkeypatch.setattr(proxy_module, "streamable_http_client", fake_streamable_http_client)


def test_call_load_context_fills_arguments_and_sends_the_auth_header(monkeypatch):
    async def _remote_call_tool(ctx, params):
        assert params.arguments == {"project_slug": "acme-1", "session_id": "ses-test"}
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
    assert captured == {"authorization": "Bearer sk-1"}


def test_call_load_context_does_not_list_tools(monkeypatch):
    """Through a gateway that prefixes names, the SDK's output check re-listed every call."""
    listed = []

    async def _list(ctx, params):
        listed.append(1)
        return await _remote_list_tools(ctx, params)

    async def _remote_call_tool(ctx, params):
        return types.CallToolResult(content=[types.TextContent(text="{}")],
                                    structured_content={"result": {"text": "ctx"}})

    _stub_transport(monkeypatch, Server("fake-remote", on_list_tools=_list, on_call_tool=_remote_call_tool), {})
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "sk-1")

    assert asyncio.run(call_load_context("http://x/mcp", "acme-1")) == "ctx"
    assert listed == []


def test_call_load_context_uses_a_read_timeout_a_hook_can_wait_for(monkeypatch):
    """The SDK's default 300 s read timeout outlives the host's 10 s session-start hook."""
    import memory.mcp.proxy as proxy_module

    async def _remote_call_tool(ctx, params):
        return types.CallToolResult(content=[types.TextContent(text="{}")],
                                    structured_content={"result": {"text": "ctx"}})

    _stub_transport(monkeypatch, Server("fake-remote", on_list_tools=_remote_list_tools, on_call_tool=_remote_call_tool), {})
    stubbed, seen = proxy_module.create_mcp_http_client, {}

    def spy(*, auth, timeout=None):
        seen["timeout"] = timeout
        return stubbed(auth=auth)

    monkeypatch.setattr(proxy_module, "create_mcp_http_client", spy)
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "sk-1")

    assert asyncio.run(call_load_context("http://x/mcp", "acme-1")) == "ctx"
    assert seen["timeout"].read < 10


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


def test_tool_names_match_what_the_server_registers():
    """The proxy's list is only right because it ships with the server: pin that."""
    from memory.mcp.server import build_mcp

    async def registered():
        return {tool.name for tool in await build_mcp().list_tools()}

    assert asyncio.run(registered()) == TOOL_NAMES


def test_canonical_tool_name_sees_through_any_gateway_prefix():
    assert canonical_tool_name("retain") == "retain"
    assert canonical_tool_name("ach-memory.retain") == "retain"
    assert canonical_tool_name("mem__get_memory") == "get_memory"
    assert canonical_tool_name("ach-memory.list_memories") == "list_memories"
    assert canonical_tool_name("other.search") is None
    assert canonical_tool_name("unretain") is None


def test_bridge_unprefixes_gateway_tools_and_calls_them_by_their_remote_name(tmp_path, monkeypatch):
    """LiteLLM lists `ach-memory.retain`: the host sees `retain`, name-keyed logic
    (here the retain stamp) fires, and the remote is called by the name it listed."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    called: list[str] = []

    async def _list(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name="ach-memory.retain", inputSchema={"type": "object", "properties": {}}),
            types.Tool(name="other.search", inputSchema={"type": "object", "properties": {}}),
        ])

    async def _call(ctx, params):
        called.append(params.name)
        return types.CallToolResult(content=[types.TextContent(text="ok")])

    remote = Server("fake-gateway", on_list_tools=_list, on_call_tool=_call)

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
                assert [t.name for t in listed.tools] == ["retain", "other.search"]
                await host.call_tool("retain", {"content": "x"})

    asyncio.run(scenario())
    assert called == ["ach-memory.retain"]
    assert retain_stamp().exists()


SPELLINGS = frozenset({"ach", "ackstorm-ach", "github.com-ackstorm-ach"})


@pytest.mark.parametrize("typed", ["ach", "ackstorm/ach", "Ackstorm-ACH", "github.com/ackstorm/ach"])
@pytest.mark.parametrize("tool", ["retain", "recall", "reflect", "list_memories", "load_context"])
def test_fill_arguments_resolves_a_spelling_of_this_repo(tool, typed):
    filled = fill_arguments(tool, {"scope": "project", "project_slug": typed}, "canon-1", SPELLINGS)
    assert filled["project_slug"] == "canon-1"


def test_fill_arguments_leaves_another_repo_alone():
    filled = fill_arguments("retain", {"project_slug": "ach-memory"}, "canon-1", SPELLINGS)
    assert filled["project_slug"] == "ach-memory"


@pytest.mark.parametrize("tool", ["forget", "correct", "get_memory", "rename_project"])
def test_fill_arguments_keeps_the_slug_on_calls_that_address_one_bank(tool):
    filled = fill_arguments(tool, {"project_slug": "ach"}, "canon-1", SPELLINGS)
    assert filled["project_slug"] == "ach"


def test_repo_slug_spellings_come_from_origin(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert "acme-payments-api" in repo_slug_spellings(str(repo))


def test_memory_project_turns_spellings_off(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.setenv("MEMORY_PROJECT", "payments-api")
    assert repo_slug_spellings(str(repo)) == frozenset()


def test_personal_tools_drop_the_project_only_tools():
    schema = {"type": "object", "properties": {"project_slug": {}}}
    tools = [types.Tool(name=n, inputSchema=schema)
             for n in ("list_families", "set_families", "rename_project", "load_context", "list_projects")]
    assert [t.name for t in personal_tools(tools)] == ["load_context", "list_projects"]


def test_without_a_project_list_projects_goes_out_bare_and_the_rest_as_user():
    calls: list[tuple[str, dict]] = []

    async def _remote_call_tool(ctx, params):
        calls.append((params.name, dict(params.arguments or {})))
        return types.CallToolResult(content=[types.TextContent(text="ok")])

    remote = Server("fake-remote", on_list_tools=_remote_list_tools, on_call_tool=_remote_call_tool)

    async def scenario():
        async with InMemoryTransport(remote) as (read, write), ClientSession(read, write) as session:
            await session.discover()
            bridge = _build_bridge(session, None)
            async with InMemoryTransport(bridge) as (h_read, h_write), ClientSession(h_read, h_write) as host:
                await host.discover()
                await host.call_tool("list_projects", {})
                await host.call_tool("recall", {"query": "x"})

    asyncio.run(scenario())
    assert calls == [("list_projects", {"session_id": "ses-test"}),
                     ("recall", {"query": "x", "scope": "user", "session_id": "ses-test"})]


def test_reads_carry_the_host_session_and_writes_do_not(monkeypatch):
    from memory.mcp import proxy as proxy_module

    assert with_session("recall", {"query": "x", "session_id": "typed"}) == {"query": "x", "session_id": "ses-test"}
    assert with_session("retain", {"title": "t"}) == {"title": "t"}
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    assert proxy_module.host_session_id() == "thread-1"
    monkeypatch.delenv("CODEX_THREAD_ID")
    assert proxy_module.host_session_id() == proxy_module._PROCESS_SESSION


def test_the_agent_never_sees_session_id_in_a_schema():
    tool = types.Tool(name="recall", input_schema={
        "type": "object", "properties": {"query": {"type": "string"}, "session_id": {"type": "string"}}})
    assert set(without_session(tool).input_schema["properties"]) == {"query"}
