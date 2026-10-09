"""Host-side stdio-to-Streamable-HTTP bridge (SPEC §6).

The remote server never sees the caller's cwd, so this stdio child -- run on
the host, next to the agent -- resolves the project once and fills it into
tool calls the caller left bare. Everything else forwards to
the remote unchanged; the `mcp` SDK owns the protocol.
"""

import os
import subprocess
import time
from pathlib import Path

import httpx2
import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from memory.errors import InvalidRequest
from memory.slugs import slug_from_locator


def api_key() -> str:
    """The credential: `ACH_MEMORY_TOKEN_COMMAND`'s stdout if set, else `ACH_MEMORY_API_KEY`.

    The command is a credential helper (e.g. `ach-cli token`) for tokens too
    short-lived to sit in a variable -- the ACH Hub's JWT lives 60 minutes.
    Run through the shell like git's credential helpers; a failing one yields
    "" rather than falling back to a stale static key.
    """
    command = os.environ.get("ACH_MEMORY_TOKEN_COMMAND", "").strip()
    if not command:
        return os.environ.get("ACH_MEMORY_API_KEY", "")
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def auth_headers() -> dict[str, str]:
    """Credential header for the remote: Bearer on Authorization, bare key otherwise.

    `ACH_MEMORY_HEADER` names the header (default Authorization) for when a
    proxy in front of the service blocks Authorization as a passthrough header.
    """
    key = api_key()
    header = (os.environ.get("ACH_MEMORY_HEADER") or "Authorization").strip() or "Authorization"
    if header.lower() == "authorization":
        return {"Authorization": f"Bearer {key}"}
    return {header: key}


class FreshCredential(httpx2.Auth):
    """Sets the credential on every request: the proxy's session outlives a helper's token.

    ponytail: the helper runs blocking inside the event loop (~12 ms for `ach-cli
    token`); move it to a thread if a slow helper ever stalls concurrent calls.
    """

    def auth_flow(self, request):
        request.headers.update(auth_headers())
        yield request


async def _handshake(session: ClientSession) -> None:
    """`server/discover`, else legacy `initialize` -- the SDK leaves the fallback to us,
    and LiteLLM's MCP gateway answers discover with -32603."""
    try:
        await session.discover()
    except MCPError:
        await session.initialize()


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
    `load_context` carries no `scope`, so it is filled unconditionally; recall's
    `scope="all"` needs the slug for the project half of its search."""
    filled = dict(arguments)
    if project_slug and not filled.get("project_slug") and (
        tool_name == "load_context" or filled.get("scope") in ("project", "all")
    ):
        filled["project_slug"] = project_slug
    return filled


_PERSONAL_DESCRIPTIONS = {
    "recall": (
        "Search memory and return bounded, grounded matching facts, most relevant first. "
        "Results below a relevance floor are withheld rather than returned as padding, so "
        "a query with no good answer returns nothing instead of a confident-looking list; "
        "each hit carries the `score` it was ranked by. Filter with `memory_types`/`basis`. "
        "Query in English whatever language the conversation uses: a translated query "
        "scores well above a Spanish one against the same claim."
    ),
    "load_context": (
        "Load the bounded standing context for this session from your own memory. "
        "Creates nothing; call once at session start."
    ),
}


def personal_tools(tools: list[types.Tool]) -> list[types.Tool]:
    """Tool list for a session with no project: user memory only, so `scope` and
    `project_slug` leave the schemas and `rename_project` disappears."""
    out = []
    for tool in tools:
        if tool.name == "rename_project":
            continue
        schema = dict(tool.input_schema)
        schema["properties"] = {
            k: v for k, v in schema.get("properties", {}).items() if k not in ("scope", "project_slug")
        }
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in ("scope", "project_slug")]
        out.append(
            tool.model_copy(
                update={
                    "input_schema": schema,
                    "description": _PERSONAL_DESCRIPTIONS.get(tool.name, tool.description),
                }
            )
        )
    return out


def retain_stamp(cwd: str | None = None) -> Path:
    """Where the last successful retain for this checkout is timestamped.

    `hooks/scripts/idle-nudge.sh` reads it with the same key: `$PWD` with slashes
    as underscores, under `${XDG_CACHE_HOME:-~/.cache}/ach-memory`. Not `$TMPDIR`:
    each host hands its children a different one (Claude Code sets
    `~/.claude/tmp`), and the clock has to be one per checkout across hosts --
    three sessions on one repo share it, which is what "did we save anything
    lately" means.
    """
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ach-memory"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / ("retain-" + (cwd or os.getcwd()).replace("/", "_"))


def _build_bridge(session: ClientSession, slug: str | None) -> Server:
    """The host-facing Server: lists the remote's tools and forwards calls to it."""

    async def _list_tools(ctx, params):
        listed = await session.list_tools(params=params)
        return listed if slug else listed.model_copy(update={"tools": personal_tools(listed.tools)})

    async def _call_tool(ctx, params):
        arguments = fill_arguments(params.name, params.arguments or {}, slug)
        if not slug and params.name != "load_context":
            arguments["scope"] = "user"
        try:
            result = await session.call_tool(params.name, arguments)
        except Exception as exc:  # noqa: BLE001 -- any remote/network failure is a tool error
            return types.CallToolResult(isError=True, content=[types.TextContent(text=str(exc))])
        if params.name == "retain" and not result.is_error:
            try:
                retain_stamp().write_text(str(int(time.time())))
            except OSError:
                pass  # a read-only tmp must not turn a stored claim into an error
        return result

    return Server("ach-memory", on_list_tools=_list_tools, on_call_tool=_call_tool)


async def serve(url: str) -> None:
    """Bridge stdio to the remote MCP endpoint at url until the host closes stdin."""
    slug = resolve_project_context()
    client = create_mcp_http_client(auth=FreshCredential())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await _handshake(session)
        bridge = _build_bridge(session, slug)
        async with stdio_server() as (in_stream, out_stream):
            await bridge.run(in_stream, out_stream, bridge.create_initialization_options())


async def call_load_context(url: str, project_slug: str | None) -> str:
    """One `load_context` call to url; returns its text (used by `ach-memory context load`)."""
    client = create_mcp_http_client(auth=FreshCredential())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await _handshake(session)
        arguments = fill_arguments("load_context", {}, project_slug)
        result = await session.call_tool("load_context", arguments)
    if result.is_error:
        raise RuntimeError("load_context failed")
    # The tool answers {"result": {"text": ...}}; the hook prints only the standing context.
    structured = result.structured_content or {}
    return str(structured.get("result", {}).get("text") or "")
