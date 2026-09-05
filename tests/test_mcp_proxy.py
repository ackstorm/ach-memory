"""The stdio bridge: protocol forwarding plus SPEC §8 client-side context."""

import asyncio
import io
import json
import subprocess

import pytest

from memory.mcp.proxy import (
    StdioHttpBridge,
    fill_project_arguments,
    fill_working_state_arguments,
    resolve_project_context,
    resolve_workspace_context,
)


def _git_repo(tmp_path, origin: str | None):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if origin:
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", origin],
            check=True,
        )
    return tmp_path


def test_memory_project_env_wins_over_git(tmp_path, monkeypatch):
    # SPEC §8: MEMORY_PROJECT is checked before Git, even inside a repo.
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.setenv("MEMORY_PROJECT", "payments-api")
    assert resolve_project_context(str(repo)) == ("payments-api", None)


def test_git_origin_becomes_a_derived_slug_and_the_locator(tmp_path, monkeypatch):
    """SPEC §8.2 derivation, pinned to its literal output.

    The digest is the point: normalize_slug collapses `/`, `.` and `-` alike,
    so acme/payments-api and acme-payments/api both flatten to
    github.com-acme-payments-api and would share one bank without it.

    Sending the locator alone instead of a slug does not work -- measured
    against production 2026-08-27, PROJECT_CONTEXT_UNAVAILABLE -- because a
    locator never resolves identity (inv. 11) and is not unique (§17).

    The locator that travels is the CANONICAL spelling, not the raw remote:
    one spelling per repository, and no userinfo to leak (§8.2). The slug is
    unaffected -- it is derived from the canonical locator either way, so no
    project changes bank.
    """
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (
        "github.com-acme-payments-api-dab6719d",
        "github.com/acme/payments-api",
    )


def test_remote_that_names_no_repository_resolves_nothing(tmp_path, monkeypatch):
    """A local-path remote identifies no project, and must not crash startup.

    canonical_locator raises ProjectInvalidSlug for a remote with no host and
    path. Letting that escape would kill the stdio server at launch -- before
    the host ever calls a tool -- over a repository the agent may never ask
    about.
    """
    repo = _git_repo(tmp_path, str(tmp_path / "bare-clone"))
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (None, None)


def test_repo_without_origin_resolves_nothing(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, None)
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (None, None)


def test_no_repo_resolves_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(tmp_path)) == (None, None)


def _committed_repo(path):
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "initial"], check=True)
    return path


def test_repeated_resolution_of_one_worktree_is_stable(tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    assert resolve_workspace_context(str(repo)) == resolve_workspace_context(str(repo))


def test_two_worktree_roots_at_the_same_commit_get_different_ids(tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree), "-b", "feature"],
        check=True,
    )
    assert resolve_workspace_context(str(repo)) != resolve_workspace_context(str(worktree))


def test_a_branch_change_does_not_change_the_workspace_id(tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    before = resolve_workspace_context(str(repo))
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "other-branch"], check=True)
    after = resolve_workspace_context(str(repo))
    assert before == after


def test_a_symlinked_cwd_resolves_to_the_same_id_as_the_real_root(tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert resolve_workspace_context(str(link)) == resolve_workspace_context(str(repo))


def test_a_non_worktree_returns_none(tmp_path):
    assert resolve_workspace_context(str(tmp_path)) is None


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # A locator with no slug is what the server rejects, so this shape is
        # never produced by resolve_project_context -- kept to pin that the
        # injector reports what it was given rather than inventing a slug.
        ({"scope": "project"}, {"scope": "project", "git_locator": "L"}),
        # Explicit values from the model are never overridden.
        (
            {"scope": "project", "project_slug": "theirs"},
            {"scope": "project", "project_slug": "theirs"},
        ),
        (
            {"scope": "project", "git_locator": "theirs"},
            {"scope": "project", "git_locator": "theirs"},
        ),
        # scope=user is untouched -- injecting here would attach a project
        # to a user-bank call the server would then reject or misroute.
        ({"scope": "user"}, {"scope": "user"}),
        # Tools with no scope argument (get_operation etc.) are untouched:
        # injecting an argument their schema lacks fails validation upstream.
        ({"operation_id": "op_1"}, {"operation_id": "op_1"}),
    ],
)
def test_fill_project_arguments_locator(arguments, expected):
    fill_project_arguments(arguments, None, "L")
    assert arguments == expected


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"session_id": "s1"},
            {"session_id": "s1", "project_slug": "acme-api", "git_locator": "L", "workspace_id": "W"},
        ),
        # Explicit values from the model are never overridden.
        (
            {"session_id": "s1", "workspace_id": "theirs"},
            {"session_id": "s1", "project_slug": "acme-api", "git_locator": "L", "workspace_id": "theirs"},
        ),
        # Phase 2 review finding #7: project_slug and git_locator are filled
        # only together, from the SAME branch as fill_project_arguments. A
        # model naming an explicit alternate project_slug (or an explicit
        # alternate git_locator) must not have this repository's OTHER half
        # silently paired in -- that would point the call at a
        # slug/locator combination the model never asked for.
        (
            {"session_id": "s1", "project_slug": "theirs"},
            {"session_id": "s1", "project_slug": "theirs", "workspace_id": "W"},
        ),
        (
            {"session_id": "s1", "git_locator": "theirs"},
            {"session_id": "s1", "git_locator": "theirs", "workspace_id": "W"},
        ),
    ],
)
def test_fill_working_state_arguments(arguments, expected):
    fill_working_state_arguments(arguments, "acme-api", "L", "W")
    assert arguments == expected


def test_fill_sends_the_slug_and_the_locator_together():
    """Both, because they do different jobs: the slug resolves the project and
    the locator binds it to the repository on first touch (§8.3) and refuses a
    mismatch later (§8.4)."""
    arguments = {"scope": "project"}
    fill_project_arguments(arguments, "payments-api", "L")
    assert arguments == {
        "scope": "project",
        "project_slug": "payments-api",
        "git_locator": "L",
    }


@pytest.mark.anyio
async def test_stdio_http_bridge_forwards_protocol_and_injects_project_context():
    seen = []
    tool_names = {
        "retain",
        "sync_retain",
        "recall",
        "reflect",
        "list_memories",
        "get_memory",
        "forget",
        "correct",
        "restore",
        "list_documents",
        "get_document",
        "delete_document",
        "get_operation",
        "list_operations",
        "cancel_operation",
    }

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append((message, dict(request.headers)))
        if message.get("method") == "server/discover":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "resultType": "complete",
                        "supportedVersions": ["2026-07-28"],
                        "capabilities": {"tools": {}},
                        "instructions": "REMOTE POLICY",
                    },
                },
            )
        if message.get("method") == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"tools": [{"name": name} for name in tool_names]},
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": message["id"], "result": {}},
        )

    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/",
            "secret",
            slug="acme-api",
            locator="git@github.com:acme/api.git",
            instructions="POLICY + BRIEF",
            client=client,
        )
        discovered = await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": "discover-1",
                "method": "server/discover",
                "params": {"_meta": meta},
            }
        )
        listed = await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": "list-1",
                "method": "tools/list",
                "params": {"_meta": meta},
            }
        )
        await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {
                    "name": "recall",
                    "arguments": {"scope": "project", "query": "decisions"},
                    "_meta": meta,
                },
            }
        )

    assert discovered[0]["result"]["instructions"] == "POLICY + BRIEF"
    assert {tool["name"] for tool in listed[0]["result"]["tools"]} == tool_names
    assert {headers["mcp-protocol-version"] for _, headers in seen} == {
        "2026-07-28"
    }
    assert seen[2][0]["params"]["arguments"] == {
        "scope": "project",
        "query": "decisions",
        "project_slug": "acme-api",
    }
    assert seen[2][1]["authorization"] == "Bearer secret"
    assert "application/json" in seen[2][1]["accept"]
    assert "text/event-stream" in seen[2][1]["accept"]


@pytest.mark.anyio
async def test_stdio_http_bridge_decodes_every_sse_message():
    async def remote(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                'data: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n\n'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/", "secret", client=client)
        messages = await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28"
                    }
                },
            }
        )

    assert messages == [
        {"jsonrpc": "2.0", "method": "notifications/progress"},
        {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}},
    ]


@pytest.mark.anyio
async def test_modern_mcp_request_metadata_is_mirrored_into_http_headers():
    seen = []

    async def remote(request: httpx.Request) -> httpx.Response:
        seen.append((json.loads(request.content), dict(request.headers)))
        message = seen[-1][0]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "resultType": "complete",
                    "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {}},
                    "instructions": "REMOTE POLICY",
                },
            },
        )

    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/",
            "secret",
            instructions="POLICY + BRIEF",
            client=client,
        )
        discovered = await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": "discover-1",
                "method": "server/discover",
                "params": {"_meta": meta},
            }
        )
        await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {
                    "name": "recall",
                    "arguments": {"scope": "user", "query": "decisions"},
                    "_meta": meta,
                },
            }
        )

    assert discovered[0]["result"]["instructions"] == "POLICY + BRIEF"
    assert seen[0][1]["mcp-protocol-version"] == "2026-07-28"
    assert seen[0][1]["mcp-method"] == "server/discover"
    assert "mcp-name" not in seen[0][1]
    assert seen[1][1]["mcp-protocol-version"] == "2026-07-28"
    assert seen[1][1]["mcp-method"] == "tools/call"
    assert seen[1][1]["mcp-name"] == "recall"


@pytest.mark.anyio
async def test_stdio_serve_frames_one_modern_request_and_response_per_line():
    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}},
        )

    request = {
        "jsonrpc": "2.0",
        "id": "list-1",
        "method": "tools/list",
        "params": {
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
        },
    }
    source = io.BytesIO(json.dumps(request).encode() + b"\n")
    destination = io.BytesIO()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/", "secret", client=client)
        await bridge.serve(source, destination)

    assert json.loads(destination.getvalue()) == {
        "jsonrpc": "2.0",
        "id": "list-1",
        "result": {"tools": []},
    }


@pytest.mark.anyio
async def test_stdio_bridge_rejects_initialize_without_contacting_remote():
    contacted = False

    async def remote(_request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(500)

    source = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
    )
    destination = io.BytesIO()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/", "secret", client=client)
        await bridge.serve(source, destination)

    error = json.loads(destination.getvalue())
    assert error["error"] == {
        "code": -32022,
        "message": "Unsupported protocol version",
        "data": {"supported": ["2026-07-28"], "requested": None},
    }
    assert contacted is False


@pytest.mark.anyio
async def test_stdio_cancellation_closes_the_matching_http_exchange():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def remote(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    request = {
        "jsonrpc": "2.0",
        "id": "call-1",
        "method": "tools/call",
        "params": {
            "name": "recall",
            "arguments": {"scope": "user", "query": "decisions"},
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
        },
    }
    cancellation = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "call-1", "reason": "test"},
    }

    class Input:
        def __init__(self) -> None:
            self.index = 0

        async def readline(self) -> bytes:
            self.index += 1
            if self.index == 1:
                return json.dumps(request).encode() + b"\n"
            if self.index == 2:
                await started.wait()
                return json.dumps(cancellation).encode() + b"\n"
            return b""

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/", "secret", client=client)
        await bridge.serve(Input(), io.BytesIO())

    assert cancelled.is_set()


@pytest.mark.anyio
async def test_tool_schema_header_annotations_are_mirrored_on_calls():
    seen = []

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append(dict(request.headers))
        if message["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "recall",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "region": {
                                    "type": "string",
                                    "x-mcp-header": "Region",
                                }
                            },
                        },
                    }
                ]
            }
        else:
            result = {}
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
        )

    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/", "secret", client=client)
        await bridge.forward(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
        )
        await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "recall",
                    "arguments": {"region": "eu-west-1"},
                    "_meta": meta,
                },
            }
        )

    assert seen[1]["mcp-param-region"] == "eu-west-1"


async def _forwarded_arguments(request_params: dict, *, slug=None, locator=None, workspace_id=None):
    """Forward one tools/call through StdioHttpBridge and return the
    arguments the remote actually received, proving the bridge's own
    injection (no more Middleware class -- see stream())."""
    seen: list[dict | None] = []

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        params = message.get("params", {})
        seen.append(params.get("arguments"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {}})

    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/", "secret",
            slug=slug, locator=locator, workspace_id=workspace_id, client=client,
        )
        await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {**request_params, "_meta": meta},
            }
        )
    return seen[0]


@pytest.mark.anyio
async def test_bridge_injects_project_locator_and_workspace_into_working_state_tools():
    result = await _forwarded_arguments(
        {"name": "start_working_session", "arguments": {"session_id": "sess-1"}},
        slug="acme-api", locator="L", workspace_id="ws_" + "a" * 32,
    )

    assert result == {
        "session_id": "sess-1",
        "project_slug": "acme-api",
        "git_locator": "L",
        "workspace_id": "ws_" + "a" * 32,
    }


@pytest.mark.anyio
async def test_bridge_preserves_explicit_values_for_working_state_tools():
    result = await _forwarded_arguments(
        {
            "name": "set_working_state",
            "arguments": {
                "project_slug": "explicit-project",
                "workspace_id": "ws_" + "c" * 32,
            },
        },
        slug="acme-api", locator="L", workspace_id="ws_" + "a" * 32,
    )

    assert result["project_slug"] == "explicit-project"
    assert result["workspace_id"] == "ws_" + "c" * 32
    # Phase 2 review finding #7: an explicit alternate project_slug must not
    # get this repository's git_locator silently paired in -- that would aim
    # the call at a slug/locator combination the model never asked for.
    assert "git_locator" not in result


@pytest.mark.anyio
async def test_bridge_does_not_inject_workspace_into_other_tools():
    result = await _forwarded_arguments(
        {"name": "list_memories", "arguments": {"scope": "project"}},
        slug="acme-api", locator="L", workspace_id="ws_" + "a" * 32,
    )

    assert "workspace_id" not in result


import httpx
import respx

from memory.mcp import proxy


@respx.mock
def test_bootstrap_calls_the_endpoint_once_and_returns_none_on_success():
    route = respx.post("https://memory.test/v1/bootstrap").mock(
        return_value=httpx.Response(200, json={"user_model": None})
    )

    result = proxy.bootstrap("https://memory.test", "k", "acme-api")

    assert result is None
    assert route.call_count == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"project_slug": "acme-api"}
    assert route.calls.last.request.headers["authorization"] == "Bearer k"


@respx.mock
def test_bootstrap_with_no_project_slug_sends_an_empty_body():
    route = respx.post("https://memory.test/v1/bootstrap").mock(
        return_value=httpx.Response(200, json={})
    )

    assert proxy.bootstrap("https://memory.test", "k", None) is None
    assert json.loads(route.calls.last.request.content) == {}


@respx.mock
def test_bootstrap_returns_the_content_free_project_error_code():
    respx.post("https://memory.test/v1/bootstrap").mock(
        return_value=httpx.Response(
            409,
            json={"error": {"code": "PROJECT_SLUG_CONFLICT", "message": "bank user_abc123"}},
        )
    )

    result = proxy.bootstrap("https://memory.test", "k", "acme-api")

    assert result == "PROJECT_SLUG_CONFLICT"
    assert "user_abc123" not in (result or "")


@respx.mock
def test_bootstrap_failure_with_no_project_slug_is_not_reported():
    """Nothing project-specific to route around; a User-only failure must
    not degrade any tool."""
    respx.post("https://memory.test/v1/bootstrap").mock(
        return_value=httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR"}})
    )

    assert proxy.bootstrap("https://memory.test", "k", None) is None


@respx.mock
def test_bootstrap_network_failure_reports_a_fixed_code_only_with_a_project():
    respx.post("https://memory.test/v1/bootstrap").mock(side_effect=httpx.ConnectError("down"))

    assert proxy.bootstrap("https://memory.test", "k", "acme-api") == "BOOTSTRAP_UNAVAILABLE"
    assert proxy.bootstrap("https://memory.test", "k", None) is None


@pytest.mark.anyio
async def test_project_scope_tool_calls_are_routed_locally_after_a_bootstrap_failure():
    """SPEC §7.5: a Project bootstrap failure degrades scope="project" tool
    calls to a local, content-free error without a second round trip --
    scope="user" calls are unaffected."""
    contacted: list[str] = []

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        params = message.get("params", {})
        arguments = params.get("arguments", {})
        contacted.append(arguments.get("scope"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {}})

    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/", "secret", client=client,
            project_bootstrap_error="PROJECT_SLUG_CONFLICT",
        )
        project_reply = await bridge.forward(
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "list_memories", "arguments": {"scope": "project"},
                    "_meta": meta,
                },
            }
        )
        user_reply = await bridge.forward(
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "list_memories", "arguments": {"scope": "user"},
                    "_meta": meta,
                },
            }
        )

    assert contacted == ["user"]
    assert project_reply[0]["result"]["isError"] is True
    assert "PROJECT_SLUG_CONFLICT" in project_reply[0]["result"]["content"][0]["text"]
    assert user_reply[0]["result"] == {}

















