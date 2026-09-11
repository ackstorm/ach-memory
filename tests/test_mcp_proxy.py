"""The stdio bridge: protocol forwarding plus SPEC §8 client-side context."""

import asyncio
import io
import json
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from memory.mcp.proxy import (
    StdioHttpBridge,
    auth_headers,
    fill_project_arguments,
    fill_working_state_arguments,
    resolve_project_context,
    resolve_workspace_context,
)


def test_auth_headers_defaults_to_authorization_bearer(monkeypatch):
    monkeypatch.delenv("ACH_MEMORY_HEADER", raising=False)
    assert auth_headers("sk-1") == {"Authorization": "Bearer sk-1"}


def test_auth_headers_blank_falls_back_to_authorization(monkeypatch):
    """opencode's {env:ACH_MEMORY_HEADER} writes "" when the var is unset;
    that must read as the Authorization default, not a header named ""."""
    monkeypatch.setenv("ACH_MEMORY_HEADER", "  ")
    assert auth_headers("sk-1") == {"Authorization": "Bearer sk-1"}


def test_auth_headers_custom_header_sends_the_bare_key(monkeypatch):
    """A proxy that blocks Authorization (stacklok/toolhive#6394) needs the
    key on a header it passes. No Bearer prefix: the service strips one off
    whichever header it reads, and a bare key is what the gateway path uses."""
    monkeypatch.setenv("ACH_MEMORY_HEADER", "x-litellm-api-key")
    assert auth_headers("sk-1") == {"x-litellm-api-key": "sk-1"}


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


def test_a_bare_load_context_still_resolves_this_project():
    """load_context has no `scope` argument, so the scope-gated filler never
    fires for it. Left unfilled the server resolves no project and answers
    with user-only context -- silently, with an empty `omissions`. It takes
    no git_locator, so only the slug and the workspace go in."""
    arguments = {}
    fill_working_state_arguments(arguments, "acme-api", None, "W")
    assert arguments == {"project_slug": "acme-api", "workspace_id": "W"}


@pytest.mark.anyio
async def test_the_bridge_fills_a_bare_clear_working_state_call():
    """Same scope-less shape as load_context, and the same filler. This one
    takes a git_locator, so both halves go in together."""
    seen = []

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append(message)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": {}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/",
            "secret",
            slug="acme-api",
            locator="git@github.com:acme/api.git",
            workspace_id="W",
            client=client,
        )
        await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": "cws-1",
                "method": "tools/call",
                "params": {
                    "name": "clear_working_state",
                    "arguments": {"session_id": "s1"},
                },
            }
        )

    assert seen[0]["params"]["arguments"] == {
        "session_id": "s1",
        "project_slug": "acme-api",
        "git_locator": "git@github.com:acme/api.git",
        "workspace_id": "W",
    }


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
async def test_the_bridge_fills_a_bare_load_context_call():
    """The regression this guards: load_context carries no `scope`, so the
    scope-gated filler skipped it and the call reached the server with no
    slug. context_service builds the project section only when one is
    present, so the agent got user-only context back with an empty
    `omissions` -- a silent half-answer. It takes no git_locator."""
    seen = []

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append(message)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": {}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/",
            "secret",
            slug="acme-api",
            locator="git@github.com:acme/api.git",
            workspace_id="W",
            client=client,
        )
        await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": "ctx-1",
                "method": "tools/call",
                "params": {"name": "load_context", "arguments": {}},
            }
        )

    assert seen[0]["params"]["arguments"] == {
        "project_slug": "acme-api",
        "workspace_id": "W",
    }


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
async def test_stdio_bridge_serves_the_handshake_and_reuses_what_was_negotiated():
    """A handshake host must get through `initialize` and stay through it.

    Demanding the per-request `_meta` revision on every message rejected
    `initialize` by construction -- a host cannot name a revision it has not
    negotiated -- so the bridge answered -32022 to the first message of every
    MCP host there is, Claude Code and the mcp SDK's own `ClientSession`
    included. Both halves of the fix are pinned here: the handshake reaches
    the remote carrying no revision header of its own, and the revision the
    remote settled on is what every later request is sent under.
    """
    seen = []

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append((message, dict(request.headers)))
        result = (
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ach-memory", "version": "0"},
                "instructions": "REMOTE POLICY",
            }
            if message["method"] == "initialize"
            else {"tools": []}
        )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/",
            "secret",
            instructions="POLICY + BRIEF",
            client=client,
        )
        handshake = await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "host", "version": "1"},
                    "_meta": {},
                },
            }
        )
        # No `_meta` revision on this one either: a handshake host names the
        # revision once and never again.
        await bridge.forward(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )

    assert "mcp-protocol-version" not in seen[0][1]
    assert seen[0][1]["mcp-method"] == "initialize"
    assert seen[1][1]["mcp-protocol-version"] == "2025-11-25"
    # `initialize` is where a handshake host reads instructions, so the
    # fetched context has to be substituted there as well as in
    # `server/discover`.
    assert handshake[0]["result"]["instructions"] == "POLICY + BRIEF"


@pytest.mark.anyio
async def test_an_empty_context_fetch_leaves_the_remote_instructions_alone():
    """Fail-open context must not cost the host the remote's own guidance.

    `fetch_context` is bounded and silent, so the CLI hands the bridge `""`
    whenever it times out or the workspace has nothing standing -- and
    `initialize` is the only place a handshake host reads instructions.
    """

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "instructions": "REMOTE POLICY",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge(
            "https://memory.test/mcp/", "secret", instructions="", client=client
        )
        handshake = await bridge.forward(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            }
        )

    assert handshake[0]["result"]["instructions"] == "REMOTE POLICY"


@pytest.mark.anyio
async def test_stdio_bridge_leaves_notifications_unanswered():
    """`notifications/initialized` follows every handshake and has no id.

    Answering it -- which is what treating every line as a request did --
    puts a JSON-RPC error on stdout for a message that must produce no
    response at all, immediately after the host connects.
    """

    async def remote(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
        )

    lines = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    ]
    source = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in lines))
    destination = io.BytesIO()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/", "secret", client=client)
        await bridge.serve(source, destination)

    replies = [json.loads(line) for line in destination.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1]


@pytest.mark.anyio
async def test_stdio_bridge_rejects_a_revision_the_sdk_cannot_serve():
    """A typo or a future revision still fails loudly, without a round trip."""
    contacted = False

    async def remote(_request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(500)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2099-01-01"}
        },
    }
    source = io.BytesIO(json.dumps(request).encode() + b"\n")
    destination = io.BytesIO()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/", "secret", client=client)
        await bridge.serve(source, destination)

    error = json.loads(destination.getvalue())["error"]
    assert error["code"] == -32022
    assert error["data"]["requested"] == "2099-01-01"
    assert "2026-07-28" in error["data"]["supported"]
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

from memory.mcp import proxy


def test_fetch_context_is_fail_open_when_the_endpoint_cannot_be_reached():
    """The host's startup may not depend on this. Port 1 refuses instantly, so
    this asserts the fail-open contract without waiting out the bound."""
    assert proxy.fetch_context("http://127.0.0.1:1/mcp/", "k", None) is None


def test_fetch_context_rejects_a_payload_without_text(monkeypatch):
    """`instructions` is handed to the bridge as `fetched["text"]`, so a
    payload missing it must read as no context at all, never as a KeyError
    moments before the host's first prompt."""
    async def _no_text(*_args):
        return {"omissions": []}

    monkeypatch.setattr(proxy, "call_load_context", _no_text)
    assert proxy.fetch_context("https://memory.test/mcp/", "k", None) is None


def test_fetch_context_returns_the_unwrapped_payload(monkeypatch):
    async def _payload(*_args):
        return {"text": "standing context", "omissions": []}

    monkeypatch.setattr(proxy, "call_load_context", _payload)
    assert proxy.fetch_context("https://memory.test/mcp/", "k", None) == {
        "text": "standing context",
        "omissions": [],
    }


@pytest.mark.anyio
async def test_load_context_is_called_with_the_resolved_project_and_workspace():
    """Bare, `load_context` resolves no project and returns user-only standing
    context -- silently, with nothing in `omissions` to say so. The resolved
    slug and workspace are this process's to supply (it has the cwd; the
    service does not), so they have to reach the tool call itself."""
    calls: list[tuple[str, dict]] = []

    class _Session:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def discover(self):
            return None

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            # The real 0.7.2 envelope (`ToolResult` -> structuredContent
            # {"result": payload}). A 0.7.1 server never produced it, and this
            # mock kept the suite green while every session started without
            # context (QA F-24); tests/test_load_context_wire.py now proves the
            # server emits this shape.
            return SimpleNamespace(
                is_error=False,
                structured_content={"result": {"text": "ctx"}},
            )

    @asynccontextmanager
    async def _transport(_url, http_client=None):
        yield (None, None)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    import mcp.client.session
    import mcp.client.streamable_http
    import mcp.shared._httpx_utils

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp.client.session, "ClientSession", _Session)
        mp.setattr(mcp.client.streamable_http, "streamable_http_client", _transport)
        mp.setattr(mcp.shared._httpx_utils, "create_mcp_http_client", lambda *_a: _Client())
        payload = await proxy.call_load_context(
            "https://memory.test/mcp/", "k", "acme-api", "ws_" + "a" * 32
        )

    assert payload == {"text": "ctx"}
    assert calls == [
        ("load_context", {"project_slug": "acme-api", "workspace_id": "ws_" + "a" * 32}),
    ]



















@pytest.mark.anyio
async def test_a_remote_failure_names_the_endpoint_and_status_it_got():
    """-32000 alone cannot be acted on, and stderr is not shown by hosts.

    One code covers a wrong endpoint, DNS, TLS, 401, 404 and every 5xx, so
    "Remote MCP request failed" was the entire diagnosis a host could offer:
    measured against a Codex install whose `--url` carried the /mcp/ mount
    twice, where the 404 behind it was invisible from inside the host.
    """

    async def remote(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    }
    source = io.BytesIO(json.dumps(request).encode() + b"\n")
    destination = io.BytesIO()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        bridge = StdioHttpBridge("https://memory.test/mcp/mcp/", "secret", client=client)
        await bridge.serve(source, destination)

    error = json.loads(destination.getvalue())["error"]
    assert error["code"] == -32000
    assert error["data"]["status"] == 404
    assert error["data"]["url"] == "https://memory.test/mcp/mcp/"
    assert error["data"]["reason"] == "HTTPStatusError"
    # The credential travels in a header and must not be echoed back.
    assert "secret" not in json.dumps(error)
