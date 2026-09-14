"""Host-side stdio-to-Streamable-HTTP bridge (SPEC §6).

The remote server never sees the caller's cwd, so this stdio child -- run on
the host, next to the agent -- resolves the project and workspace once and
fills them into tool calls the caller left bare. Everything else forwards to
the remote unchanged; the `mcp` SDK owns the protocol.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from memory.errors import InvalidRequest
from memory.slugs import slug_from_locator

# Tool calls that carry no `scope` argument, so they need project_slug and
# workspace_id filled in directly rather than gated on scope == "project".
_WORKSPACE_TOOLS = frozenset(
    {"working_state_get", "working_state_put", "working_state_delete", "load_context"}
)


def auth_headers() -> dict[str, str]:
    """Credential header for the remote: Bearer on Authorization, bare key otherwise.

    `ACH_MEMORY_HEADER` names the header (default Authorization) for when a
    proxy in front of the service blocks Authorization as a passthrough header.
    """
    api_key = os.environ.get("ACH_MEMORY_API_KEY", "")
    header = (os.environ.get("ACH_MEMORY_HEADER") or "Authorization").strip() or "Authorization"
    if header.lower() == "authorization":
        return {"Authorization": f"Bearer {api_key}"}
    return {header: api_key}


def resolve_project_context(cwd: str | None = None) -> str | None:
    """MEMORY_PROJECT, else the repo's origin remote slugged; None outside a repo/origin."""
    slug = os.environ.get("MEMORY_PROJECT")
    if slug:
        return slug
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd or os.getcwd(),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    locator = result.stdout.strip()
    if not locator:
        return None
    try:
        return slug_from_locator(locator)
    except InvalidRequest:
        return None


def resolve_workspace_context(cwd: str | None = None) -> str | None:
    """Opaque id for the git worktree at cwd: ws_ + sha256(canonical root)[:32].

    Never the raw path, which must not cross the network. Fails open (None)
    outside a worktree, or on any git/filesystem error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    root = result.stdout.strip()
    if result.returncode != 0 or not root:
        return None
    try:
        canonical = str(Path(root).resolve())
    except OSError:
        return None
    return f"ws_{hashlib.sha256(canonical.encode()).hexdigest()[:32]}"


def fill_arguments(
    tool_name: str,
    arguments: dict,
    project_slug: str | None,
    workspace_id: str | None,
) -> dict:
    """Inject resolved project/workspace where the caller left them out; never overwrite."""
    filled = dict(arguments)
    if tool_name in _WORKSPACE_TOOLS:
        if project_slug and not filled.get("project_slug"):
            filled["project_slug"] = project_slug
        if workspace_id and not filled.get("workspace_id"):
            filled["workspace_id"] = workspace_id
    elif filled.get("scope") == "project" and project_slug and not filled.get("project_slug"):
        filled["project_slug"] = project_slug
    return filled


def _build_bridge(session: ClientSession, slug: str | None, workspace_id: str | None) -> Server:
    """The host-facing Server: lists the remote's tools and forwards calls to it."""

    async def _list_tools(ctx, params):
        return await session.list_tools(params=params)

    async def _call_tool(ctx, params):
        arguments = fill_arguments(params.name, params.arguments or {}, slug, workspace_id)
        try:
            return await session.call_tool(params.name, arguments)
        except Exception as exc:  # noqa: BLE001 -- any remote/network failure is a tool error
            return types.CallToolResult(isError=True, content=[types.TextContent(text=str(exc))])

    return Server("ach-memory", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def serve(url: str) -> None:
    """Bridge stdio to the remote MCP endpoint at url until the host closes stdin."""
    slug = resolve_project_context()
    workspace_id = resolve_workspace_context()
    client = create_mcp_http_client(auth_headers())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.discover()
        bridge = _build_bridge(session, slug, workspace_id)
        async with stdio_server() as (in_stream, out_stream):
            await bridge.run(in_stream, out_stream, bridge.create_initialization_options())


async def call_load_context(url: str, project_slug: str | None, workspace_id: str | None) -> str:
    """One `load_context` call to url; returns its text (used by `ach-memory context load`)."""
    client = create_mcp_http_client(auth_headers())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.discover()
        arguments = fill_arguments("load_context", {}, project_slug, workspace_id)
        result = await session.call_tool("load_context", arguments)
    if result.is_error:
        raise RuntimeError("load_context failed")
    for block in result.content:
        if isinstance(block, types.TextContent):
            return block.text
    return ""
