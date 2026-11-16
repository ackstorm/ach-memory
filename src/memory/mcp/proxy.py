"""Host-side stdio-to-Streamable-HTTP bridge (SPEC §6).

The remote server never sees the caller's cwd, so this stdio child -- run on
the host, next to the agent -- resolves the project once and fills it into
tool calls the caller left bare. Everything else forwards to
the remote unchanged; the `mcp` SDK owns the protocol.
"""

import os
import subprocess
import time
import tomllib
from pathlib import Path

import httpx2
import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from memory.errors import InvalidRequest
from memory.slugs import collapse_slug, normalize_slug, repo_spellings, slug_from_locator

#: Names the project of a folder with no git origin: one line, `project = "<slug>"`.
MARKER = ".ach-memory"


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


def _origin_url(cwd: str | None) -> str:
    """The repo's origin remote; "" outside a repo or without one."""
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
        return ""
    return result.stdout.strip()


def find_marker(cwd: str | None = None) -> Path | None:
    """The nearest `.ach-memory` from `cwd` upwards, the way git finds `.git`. The search
    stops below the home directory, or below `/` outside it, so one file there cannot claim
    every folder under it."""
    start = Path(cwd or os.getcwd()).resolve()
    home = Path.home().resolve()
    for folder in (start, *start.parents):
        if folder == home or folder == folder.parent:
            return None
        if (folder / MARKER).is_file():
            return folder / MARKER
    return None


def marker_slug(cwd: str | None = None) -> str | None:
    """The slug the nearest marker names; None without one, or when it cannot be read."""
    marker = find_marker(cwd)
    if marker is None:
        return None
    try:
        value = tomllib.loads(marker.read_text()).get("project")
        return normalize_slug(value) if isinstance(value, str) else None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, InvalidRequest):
        return None


def resolve_project_context(cwd: str | None = None) -> str | None:
    """MEMORY_PROJECT, else the repo's origin remote slugged, else the `.ach-memory` marker.
    The marker counts only without an origin, so it can never split a repository that
    already has memory; None when none of the three is there."""
    slug = os.environ.get("MEMORY_PROJECT")
    if slug:
        return slug
    locator = _origin_url(cwd)
    if not locator:
        return marker_slug(cwd)
    try:
        return slug_from_locator(locator)
    except InvalidRequest:
        return None


def repo_slug_spellings(cwd: str | None = None) -> frozenset[str]:
    """Hand-typed names for this repo; none under MEMORY_PROJECT, which is the way to reach
    a project split off under such a name."""
    locator = "" if os.environ.get("MEMORY_PROJECT") else _origin_url(cwd)
    try:
        return repo_spellings(locator) if locator else frozenset()
    except InvalidRequest:
        return frozenset()


# Tools that pick a bank by slug alone. A call with a `memory_id` must keep the slug it was
# given: the memory may live in a project split off under that spelling.
_ROUTED_BY_SLUG = frozenset({
    "retain", "recall", "reflect", "list_memories", "load_context", "list_families", "set_families",
})
# Project tools without a `scope`: the slug is filled whenever the caller left it out.
_UNSCOPED = frozenset({"load_context", "list_families", "set_families"})


def fill_arguments(
    tool_name: str, arguments: dict, project_slug: str | None,
    spellings: frozenset[str] = frozenset(),
) -> dict:
    """Inject the resolved project where the caller left it out, and where the caller spelled
    this repository by hand, which would otherwise split it into a second project. Any other
    slug addresses another repository and stands. `load_context` and the family tools carry no
    `scope`, so they are filled unconditionally; recall's `scope="all"` needs the slug for the project half of
    its search; a call by `memory_id` without `scope` needs it in case the server infers
    project scope."""
    filled = dict(arguments)
    typed = filled.get("project_slug")
    scope = filled.get("scope")
    if project_slug and not typed and (
        tool_name in _UNSCOPED or scope in ("project", "all")
        or (scope is None and "memory_id" in filled)
    ) or project_slug and typed and tool_name in _ROUTED_BY_SLUG and collapse_slug(typed) in spellings:
        filled["project_slug"] = project_slug
    return filled


_PERSONAL_DESCRIPTIONS = {
    "recall": (
        "Search memory and return matching facts, most relevant first. The first hits carry "
        "`relevance`, the probability that each helps answer the query; when even the best "
        "is low, `note` says so first: then the hits are only the nearest memories, not an "
        "answer, so check one before relying on it. Filter with `memory_types`/`basis`. "
        "Query in English whatever language the conversation uses: it finds the right "
        "memory more often than a Spanish one."
    ),
    "load_context": (
        "Load this session's standing context again: your pinned memories in full. "
        "Creates nothing."
    ),
}


def personal_tools(tools: list[types.Tool]) -> list[types.Tool]:
    """Tool list for a session with no project: user memory only, so `scope` and
    `project_slug` leave the schemas, and the project-only tools disappear."""
    out = []
    for tool in tools:
        if tool.name in ("rename_project", "list_families", "set_families"):
            continue
        schema = dict(tool.input_schema)
        schema["properties"] = {
            k: v for k, v in schema.get("properties", {}).items()
            if k not in ("scope", "project_slug")
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


#: The tools this release's server registers -- the proxy ships in the same
#: package, so it knows them; tests/test_proxy.py pins this to the live registry.
TOOL_NAMES = frozenset({
    "correct", "delete_memory", "forget", "get_memory", "get_operation", "history",
    "list_families", "list_memories", "list_projects", "load_context", "pin", "recall", "reflect",
    "rename_project", "restore", "retain", "set_families",
})


def canonical_tool_name(listed: str) -> str | None:
    """Which of our tools a remote-listed name is, whatever a gateway prefixed onto it.

    LiteLLM lists `ach-memory.retain`; other gateways use other separators. Matching
    against the known names instead of parsing the prefix survives any of them: the
    name is ours if it IS a tool or ends with one after a non-alphanumeric boundary.
    """
    if listed in TOOL_NAMES:
        return listed
    matches = [
        name for name in TOOL_NAMES
        if listed.endswith(name) and not listed[-len(name) - 1].isalnum()
    ]
    return max(matches, key=len, default=None)


def _build_bridge(
    session: ClientSession, slug: str | None, spellings: frozenset[str] = frozenset()
) -> Server:
    """The host-facing Server: lists the remote's tools and forwards calls to it.

    The host sees our canonical tool names; calls go out under the name the remote
    listed, so a gateway's prefix never reaches the logic keyed on tool names.
    """
    remote_names: dict[str, str] = {}

    async def _list_tools(ctx, params):
        listed = await session.list_tools(params=params)
        tools = []
        for tool in listed.tools:
            name = canonical_tool_name(tool.name)
            if name is not None:
                remote_names[name] = tool.name
                tool = tool.model_copy(update={"name": name})
            tools.append(tool)
        return listed.model_copy(update={"tools": tools if slug else personal_tools(tools)})

    async def _call_tool(ctx, params):
        arguments = fill_arguments(params.name, params.arguments or {}, slug, spellings)
        if not slug and params.name not in ("load_context", "list_projects"):
            arguments["scope"] = "user"
        try:
            result = await session.call_tool(remote_names.get(params.name, params.name), arguments)
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
        bridge = _build_bridge(session, slug, repo_slug_spellings())
        async with stdio_server() as (in_stream, out_stream):
            await bridge.run(in_stream, out_stream, bridge.create_initialization_options())


async def call_tool(url: str, name: str, arguments: dict, project_slug: str | None) -> dict:
    """One tool call to url with the project filled in; returns its `result` (used by the CLI)."""
    client = create_mcp_http_client(auth=FreshCredential())
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await _handshake(session)
        result = await session.call_tool(name, fill_arguments(name, arguments, project_slug))
    if result.is_error:
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise RuntimeError(text or f"{name} failed")
    # Every tool answers {"result": {...}}.
    return (result.structured_content or {}).get("result") or {}


async def call_load_context(url: str, project_slug: str | None) -> str:
    """The standing context text (used by `ach-memory context load`)."""
    return str((await call_tool(url, "load_context", {}, project_slug)).get("text") or "")
