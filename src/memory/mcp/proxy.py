"""Host-side stdio-to-Streamable-HTTP bridge (SPEC §6).

The remote server never sees the caller's cwd, so this stdio child -- run on
the host, next to the agent -- resolves the project once and fills it into
tool calls the caller left bare. Everything else forwards to
the remote unchanged; the `mcp` SDK owns the protocol.
"""

import os
import subprocess

import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from memory.errors import InvalidRequest
from memory.slugs import slug_from_locator


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


def fill_arguments(tool_name: str, arguments: dict, project_slug: str | None) -> dict:
    """Inject the resolved project where the caller left it out; never overwrite.
    `load_context` carries no `scope`, so it is filled unconditionally."""
    filled = dict(arguments)
    if project_slug and not filled.get("project_slug") and (
        tool_name == "load_context" or filled.get("scope") == "project"
    ):
        filled["project_slug"] = project_slug
    return filled


def _build_bridge(session: ClientSession, slug: str | None) -> Server:
    """The host-facing Server: lists the remote's tools and forwards calls to it."""

    async def _list_tools(ctx, params):
        return await session.list_tools(params=params)

    async def _call_tool(ctx, params):
        arguments = fill_arguments(params.name, params.arguments or {}, slug)
        try:
            return await session.call_tool(params.name, arguments)
        except Exception as exc:  # noqa: BLE001 -- any remote/network failure is a tool error
            return types.CallToolResult(isError=True, content=[types.TextContent(text=str(exc))])

    return Server("ach-memory", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def serve(url: str) -> None:
    """Bridge stdio to the remote MCP endpoint at url until the host closes stdin."""
    slug = resolve_project_context()
    client = create_mcp_http_client(auth_headers())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.discover()
        bridge = _build_bridge(session, slug)
        async with stdio_server() as (in_stream, out_stream):
            await bridge.run(in_stream, out_stream, bridge.create_initialization_options())


async def call_load_context(url: str, project_slug: str | None) -> str:
    """One `load_context` call to url; returns its text (used by `ach-memory context load`)."""
    client = create_mcp_http_client(auth_headers())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.discover()
        arguments = fill_arguments("load_context", {}, project_slug)
        result = await session.call_tool("load_context", arguments)
    if result.is_error:
        raise RuntimeError("load_context failed")
    # The tool answers {"result": {"text": ...}}; the hook prints only the standing context.
    structured = result.structured_content or {}
    return str(structured.get("result", {}).get("text") or "")
