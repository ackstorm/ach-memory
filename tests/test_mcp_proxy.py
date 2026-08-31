"""The stdio bridge: protocol forwarding plus SPEC §8 client-side context."""

import asyncio
import io
import json
import os
import subprocess
from datetime import UTC, datetime

import pytest

from memory.mcp.proxy import (
    StdioHttpBridge,
    fill_project_arguments,
    resolve_project_context,
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
    """
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (
        "github.com-acme-payments-api-dab6719d",
        "git@github.com:acme/payments-api.git",
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
        "git_locator": "git@github.com:acme/api.git",
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


import httpx
import respx

from memory.mcp import proxy
from memory.mcp.proxy import fetch_brief


@respx.mock
def test_fetch_brief_sends_the_resolved_project_context():
    route = respx.get("https://memory.test/v1/session-brief").mock(
        return_value=httpx.Response(
            200,
            json={
                "instructions": "POLICY + BRIEF",
                "generated_at": None,
                "sections": {"user": True, "project": False},
            },
        )
    )

    result = fetch_brief("https://memory.test", "k", "acme-api", "git@host:acme/api.git")

    assert result["instructions"] == "POLICY + BRIEF"
    request = route.calls.last.request
    assert request.url.params["project_slug"] == "acme-api"
    assert request.url.params["git_locator"] == "git@host:acme/api.git"
    assert request.headers["authorization"] == "Bearer k"


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(401),
        httpx.Response(200, text="not json"),
        httpx.ConnectError("down"),
    ],
)
def test_a_brief_that_cannot_be_fetched_is_simply_absent(failure):
    """Every failure is silent and returns None for the caller to handle."""
    if isinstance(failure, Exception):
        respx.get("https://memory.test/v1/session-brief").mock(side_effect=failure)
    else:
        respx.get("https://memory.test/v1/session-brief").mock(return_value=failure)

    assert fetch_brief("https://memory.test", "k", None, None) is None


def test_the_proxy_serves_a_cached_index_without_waiting(tmp_path, monkeypatch):
    """Startup must not depend on the network once a cache exists.

    The previous pre-``run()`` fetch made a slow service delay every MCP
    session, despite having a usable brief from the prior session on disk.
    """
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    proxy.store_cached_index("https://memory.test", "k", "acme-api", None, "INDEX rev 42")

    def _never_called(*args, **kwargs):
        raise AssertionError("startup must not block on a fetch when a cache exists")

    monkeypatch.setattr(proxy.httpx, "get", _never_called)

    assert "rev 42" in proxy.startup_instructions(
        "https://memory.test", "k", "acme-api", None, refresh=False
    )


def test_a_cached_index_exposes_its_revision_and_age(tmp_path, monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    index = (
        "-- ach-memory brief rev 42 / protocol 2 / "
        "cache-age 0000000000s / project acme-api --\n\n"
        "-- What else memory holds --"
    )
    stored = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    now = datetime(2026, 8, 29, 10, 2, 3, tzinfo=UTC)
    proxy.store_cached_index("https://memory.test", "k", "acme-api", None, index, stored_at=stored)
    text = proxy.startup_instructions(
        "https://memory.test", "k", "acme-api", None, refresh=False, now=now
    )
    assert "brief rev 42" in text
    assert "cache-age 0000000123s" in text
    assert len(text) == len(index)


def test_a_legacy_index_cache_uses_its_file_mtime_for_age(tmp_path, monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    index = "-- ach-memory brief rev 42 / protocol 2 / cache-age 0000000000s --"
    path = proxy._cache_path("https://memory.test", "acme-api", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"owner": proxy._cache_owner("k"), "instructions": index}))
    legacy_time = datetime(2026, 8, 29, 10, 0, tzinfo=UTC).timestamp()
    os.utime(path, (legacy_time, legacy_time))
    cached = proxy.load_cached_index("https://memory.test", "k", "acme-api", None)
    assert cached is not None
    assert cached.instructions == index
    assert cached.stored_at.timestamp() == legacy_time


def test_with_no_cache_and_no_service_the_proxy_still_starts(tmp_path, monkeypatch):
    """A broken service costs a session its brief, never its startup."""
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(proxy, "fetch_brief", lambda *a, **k: None)

    text = proxy.startup_instructions("https://memory.test", "k", None, None, refresh=False)

    assert "unavailable" in text.lower()


def test_a_cached_index_never_crosses_api_key_identities(tmp_path, monkeypatch):
    """The cache holds user memory, while one Unix account may switch keys."""
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    proxy.store_cached_index(
        "https://memory.test", "key-for-alice", "acme-api", None, "ALICE INDEX"
    )
    monkeypatch.setattr(proxy, "fetch_brief", lambda *a, **k: None)

    text = proxy.startup_instructions(
        "https://memory.test", "key-for-bob", "acme-api", None, refresh=False
    )

    assert "ALICE INDEX" not in text
    assert "unavailable" in text.lower()


def test_a_corrupt_cache_is_a_miss_not_a_startup_failure(tmp_path, monkeypatch):
    """A killed or manually edited cache must not prevent MCP startup."""
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    path = proxy._cache_path("https://memory.test", None, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff")
    monkeypatch.setattr(proxy, "fetch_brief", lambda *a, **k: None)

    text = proxy.startup_instructions("https://memory.test", "k", None, None, refresh=False)

    assert "unavailable" in text.lower()


def test_a_cache_hit_refreshes_the_index_for_the_next_session(tmp_path, monkeypatch):
    """The served cache is immediate; its replacement is an index-tier fetch."""
    monkeypatch.setenv("ACH_MEMORY_CACHE_DIR", str(tmp_path))
    proxy.store_cached_index("https://memory.test", "k", None, None, "OLD INDEX")
    calls = []

    def fake_fetch(*args, **kwargs):
        calls.append(kwargs)
        return {"instructions": "NEW INDEX"}

    class ImmediateThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(proxy, "fetch_brief", fake_fetch)
    monkeypatch.setattr(proxy.threading, "Thread", ImmediateThread)

    assert "OLD INDEX" in proxy.startup_instructions("https://memory.test", "k", None, None)
    assert calls == [{"tier": "index"}]
    assert (
        proxy.load_cached_index("https://memory.test", "k", None, None).instructions == "NEW INDEX"
    )
