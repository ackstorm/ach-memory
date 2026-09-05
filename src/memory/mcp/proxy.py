"""Thin stdio-to-Streamable-HTTP bridge for the remote MCP endpoint.

This is the client-side half SPEC §8 always assumed and nothing ever
shipped: "the MCP derives a slug from the current Git repository" cannot
run on the remote server, which sees a bearer token and JSON and nothing
else. Running as a stdio child of the agent host, this process has the
cwd, so it resolves MEMORY_PROJECT -> git origin -> nothing (SPEC §8
order) once at startup and fills the gap into project-scoped tool calls
the model left bare. Measured motivation: pi called
list_memories(scope="project") with neither param and got
PROJECT_CONTEXT_UNAVAILABLE with no way to recover (2026-08-27).

The remote HTTP endpoint stays first-class: this bridge adds arguments the
model omitted and otherwise forwards the JSON-RPC messages unchanged.  It is
not a second MCP server and it does not mirror the remote tool registry.
"""

import asyncio
import copy
import hashlib
import hmac
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from mcp.shared.inbound import (
    encode_header_value,
    find_invalid_x_mcp_header,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

CONTEXT_TIMEOUT_SECONDS = 2.0


def resolve_project_context(cwd: str | None = None) -> tuple[str | None, str | None]:
    """SPEC §8 order: MEMORY_PROJECT, else the repo's origin URL, else nothing.

    Returns (project_slug, git_locator). Deriving the slug from the remote is
    the CLIENT's job (§8.2, §10) and this process is the client the spec
    always meant: `slug_from_locator` shipped as a tested reference
    implementation with no caller, because until v0.3.1 this repository had
    no client to call it.

    Sending the bare locator instead does not work and cannot be made to
    work here: `resolve_project_bank` raises PROJECT_CONTEXT_UNAVAILABLE for
    any call without a slug whatever locator it carries, and that is correct
    -- a locator is metadata that never resolves identity (inv. 11) and is
    deliberately not unique (§17), so two projects may legitimately share
    one. Measured against production 2026-08-27:
    list_memories(scope="project", git_locator=<origin>) is still
    PROJECT_CONTEXT_UNAVAILABLE, by REST and through this proxy alike.

    The locator travels alongside the derived slug so the server binds it to
    the project on first touch and refuses a mismatch afterwards (§8.3/§8.4).

    Resolution happens locally; only the derived logical project and workspace
    identity are sent to the service.
    """
    slug = os.environ.get("MEMORY_PROJECT")
    if slug:
        return slug, None
    try:
        locator = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd or os.getcwd(), capture_output=True, text=True, timeout=3, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        locator = ""
    return (Path(locator).name.removesuffix(".git") if locator else None), (locator or None)


def bootstrap(base_url: str, api_key: str, project_slug: str | None) -> str | None:
    """Call `POST /v1/bootstrap` exactly once at startup (SPEC §7.5).

    Returns the content-free Project bootstrap error CODE -- never a
    message or details, which can carry a bank id -- when `project_slug`
    was configured and bootstrap failed; otherwise None. Never raises: a
    broken or slow service must cost this session its bootstrap, never its
    startup (SPEC §4.3, "does not prevent the MCP server from starting").
    A User-only bootstrap (no project_slug) failing is not reported here:
    there is nothing project-specific to route around, and startup must
    proceed regardless.
    """
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/bootstrap",
            json={"project_slug": project_slug} if project_slug else {},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return "BOOTSTRAP_UNAVAILABLE" if project_slug else None
    if response.status_code == 200 or not project_slug:
        return None
    try:
        code = response.json().get("error", {}).get("code")
    except ValueError:
        code = None
    return code or "BOOTSTRAP_UNAVAILABLE"


def resolve_workspace_context(cwd: str | None = None) -> str | None:
    """The opaque workspace id for the git worktree at cwd, or None outside one.

    `ws_` plus the first 32 hex characters of SHA-256 over the canonical
    absolute worktree root -- never the raw path, which must not cross the
    network or land in a cache filename (SPEC Working State). Branch names
    and remotes do not participate: two worktrees of the same project at the
    same commit still resolve to different ids, and a branch change in one
    leaves its id unchanged.

    Fails open (None) on missing git, a timeout, non-worktree output or a
    filesystem error resolving the root -- Working State is simply omitted
    rather than guessed.
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
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    return f"ws_{digest}"


_WORKING_STATE_TOOLS = frozenset({"start_working_session", "set_working_state"})
_READ_TOOLS = frozenset({"recall", "memory_history"})


def fill_working_state_arguments(
    arguments: dict,
    slug: str | None,
    locator: str | None,
    workspace_id: str | None,
) -> None:
    """Inject project/locator/workspace into a bare working-state call, in
    place. Explicit values from the model always win -- same reasoning as
    fill_project_arguments, just with no `scope` gate: these two tools carry
    no `scope` argument at all.

    project_slug and git_locator are filled only together, from the SAME
    branch, exactly like fill_project_arguments: a model that names an
    explicit alternate project_slug (or an explicit alternate git_locator)
    must never have the OTHER half silently paired in from this repository,
    which would point the call at a slug/locator combination the model never
    asked for.
    """
    if not arguments.get("project_slug") and not arguments.get("git_locator"):
        if slug:
            arguments["project_slug"] = slug
        if locator:
            arguments["git_locator"] = locator
    if not arguments.get("workspace_id") and workspace_id:
        arguments["workspace_id"] = workspace_id


def fill_project_arguments(
    arguments: dict, slug: str | None, locator: str | None
) -> None:
    """Inject project context into a bare scope=project call, in place.

    Only when the call already carries scope="project": every tool that
    accepts `scope` accepts both project params (they share ScopedRequest),
    while injecting into a scope-less tool (get_operation, ...) would add
    an argument its schema lacks and fail validation upstream. Explicit
    values from the model always win -- MEMORY_PROJECT pointing a second
    repository at an existing project (SPEC §8.1) must not be overridden,
    and neither must a model deliberately addressing another project.
    """
    if arguments.get("scope") != "project":
        return
    if arguments.get("project_slug") or arguments.get("git_locator"):
        return
    if slug:
        arguments["project_slug"] = slug
    if locator:
        # Sent with the slug, never instead of it: the server stores it on
        # first touch and compares it afterwards, so the project ends up bound
        # to the repository it was derived from.
        arguments["git_locator"] = locator


class StdioHttpBridge:
    """Forward protocol MCP requests between stdio and Streamable HTTP.

    Both sides speak the per-request protocol introduced in 2026-07-28. There
    is no initialization handshake or transport session to translate.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        slug: str | None = None,
        locator: str | None = None,
        workspace_id: str | None = None,
        instructions: str | None = None,
        client: httpx.AsyncClient | None = None,
        project_bootstrap_error: str | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._slug = slug
        self._locator = locator
        self._workspace_id = workspace_id
        self._instructions = instructions
        self._client = client or httpx.AsyncClient(timeout=300.0)
        self._owns_client = client is None
        self._tool_header_maps: dict[str, dict[tuple[str, ...], str]] = {}
        # Content-free: a code only, never the message/details a real
        # backend error could carry (SPEC inv. 29). Routes every
        # scope="project" tool call to a local error until a later process
        # restart re-bootstraps -- this proxy never retries bootstrap itself.
        self._project_bootstrap_error = project_bootstrap_error

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def forward(self, message: dict) -> list[dict]:
        """Collect one exchange; ``serve`` streams the same exchange live."""
        return [reply async for reply in self.stream(message)]

    async def stream(self, message: dict):
        """Yield one remote MCP exchange without buffering an SSE response."""
        outgoing = copy.deepcopy(message)
        if outgoing.get("method") == "tools/call":
            params = outgoing.get("params")
            arguments = params.get("arguments") if isinstance(params, dict) else None
            if isinstance(arguments, dict):
                tool_name = params.get("name") if isinstance(params, dict) else None
                if tool_name in _WORKING_STATE_TOOLS:
                    fill_working_state_arguments(
                        arguments, self._slug, self._locator, self._workspace_id
                    )
                else:
                    fill_project_arguments(
                        arguments,
                        self._slug,
                        None if tool_name in _READ_TOOLS else self._locator,
                    )

                if self._project_bootstrap_error and arguments.get("scope") == "project":
                    yield _project_bootstrap_error_reply(
                        outgoing.get("id"), self._project_bootstrap_error
                    )
                    return

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        protocol_version = _protocol_version(outgoing)
        if protocol_version not in MODERN_PROTOCOL_VERSIONS:
            raise UnsupportedProtocolVersion(protocol_version)
        headers["MCP-Protocol-Version"] = protocol_version
        method = outgoing.get("method")
        if not isinstance(method, str) or "id" not in outgoing:
            raise InvalidMCPRequest("Streamable HTTP accepts MCP requests only")
        headers["Mcp-Method"] = method
        name = _request_name(outgoing)
        if name is not None:
            headers["Mcp-Name"] = encode_header_value(name)
        if method == "tools/call" and name is not None:
            params = outgoing.get("params")
            arguments = params.get("arguments") if isinstance(params, dict) else None
            if isinstance(arguments, dict):
                header_map = self._tool_header_maps.get(name, {})
                headers.update(mcp_param_headers(header_map, arguments))

        async with self._client.stream(
            "POST", self._url, headers=headers, json=outgoing
        ) as response:
            media_type = (
                response.headers.get("content-type", "").partition(";")[0].strip()
            )
            if media_type == "text/event-stream":
                async for reply in _iter_sse_messages(response):
                    self._process_reply(method, outgoing.get("id"), reply)
                    yield reply
                return

            body = await response.aread()
            reply = _decode_json_message(body)
            if reply is None:
                if response.is_error:
                    response.raise_for_status()
                raise RemoteProtocolError("remote MCP returned no JSON-RPC response")
            self._process_reply(method, outgoing.get("id"), reply)
            yield reply

    def _process_reply(self, method: str, request_id: object, reply: dict) -> None:
        if method == "server/discover":
            self._replace_discovery_instructions([reply], request_id)
        elif method == "tools/list":
            self._absorb_tool_listing(reply)

    def _absorb_tool_listing(self, reply: dict) -> None:
        result = reply.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return
        kept = []
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            name = tool["name"]
            schema = tool.get("inputSchema")
            reason = find_invalid_x_mcp_header(schema)
            if reason is not None:
                self._tool_header_maps.pop(name, None)
                print(
                    f"ach-memory: dropping tool {name!r}: invalid x-mcp-header ({reason})",
                    file=sys.stderr,
                )
                continue
            self._tool_header_maps[name] = x_mcp_header_map(schema)
            kept.append(tool)
        result["tools"] = kept

    def _replace_discovery_instructions(
        self, messages: list[dict], request_id: object
    ) -> None:
        for message in messages:
            if message.get("id") != request_id:
                continue
            result = message.get("result")
            if not isinstance(result, dict):
                continue
            if self._instructions is not None:
                result["instructions"] = self._instructions

    async def serve(self, input_stream=None, output_stream=None) -> None:
        """Run until the MCP host closes stdin, emitting only JSON on stdout."""
        source = input_stream or await _stdin_reader()
        destination = output_stream or await _stdout_writer()
        writes = asyncio.Lock()
        pending: set[asyncio.Task] = set()
        in_flight: dict[object, asyncio.Task] = {}

        async def emit(replies: list[dict]) -> None:
            async with writes:
                for reply in replies:
                    payload = json.dumps(reply, separators=(",", ":")).encode() + b"\n"
                    destination.write(payload)
                drain = getattr(destination, "drain", None)
                if drain is not None:
                    await drain()
                else:
                    destination.flush()

        async def handle(message: dict) -> None:
            request_id = message.get("id")
            try:
                async for reply in self.stream(message):
                    await emit([reply])
                return
            except UnsupportedProtocolVersion as exc:
                replies = [
                    _jsonrpc_error(
                        request_id,
                        -32022,
                        "Unsupported protocol version",
                        {
                            "supported": list(MODERN_PROTOCOL_VERSIONS),
                            "requested": exc.requested,
                        },
                    )
                ]
            except InvalidMCPRequest as exc:
                replies = [_jsonrpc_error(request_id, -32600, str(exc))]
            except (RemoteProtocolError, httpx.HTTPError) as exc:
                print(f"ach-memory: remote MCP request failed: {exc}", file=sys.stderr)
                replies = [
                    _jsonrpc_error(request_id, -32000, "Remote MCP request failed")
                ]
            if replies:
                await emit(replies)

        try:
            while line := await _readline(source):
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    await emit([_jsonrpc_error(None, -32700, str(exc))])
                    continue
                if not isinstance(message, dict):
                    await emit(
                        [_jsonrpc_error(None, -32600, "MCP message must be an object")]
                    )
                    continue
                if message.get("method") == "notifications/cancelled":
                    params = message.get("params")
                    request_id = (
                        params.get("requestId") if isinstance(params, dict) else None
                    )
                    task = in_flight.get(request_id)
                    if task is not None:
                        task.cancel()
                    continue

                task = asyncio.create_task(handle(message))
                pending.add(task)
                request_id = message.get("id")
                if request_id is not None:
                    in_flight[request_id] = task

                def finished(done: asyncio.Task, request_id=request_id) -> None:
                    pending.discard(done)
                    if in_flight.get(request_id) is done:
                        in_flight.pop(request_id, None)

                task.add_done_callback(finished)
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.close()


class UnsupportedProtocolVersion(Exception):
    def __init__(self, requested: str | None) -> None:
        self.requested = requested


class InvalidMCPRequest(Exception):
    pass


class RemoteProtocolError(Exception):
    pass


async def _stdin_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader


async def _stdout_writer() -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout.buffer
    )
    return asyncio.StreamWriter(transport, protocol, None, loop)


async def _readline(source) -> bytes:
    line = source.readline()
    return await line if inspect.isawaitable(line) else line


def _protocol_version(message: dict) -> str | None:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    version = (
        meta.get("io.modelcontextprotocol/protocolVersion")
        if isinstance(meta, dict)
        else None
    )
    return version if isinstance(version, str) else None


def _request_name(message: dict) -> str | None:
    method = message.get("method")
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    if method in {"tools/call", "prompts/get"}:
        value = params.get("name")
    elif method == "resources/read":
        value = params.get("uri")
    else:
        return None
    return value if isinstance(value, str) else None


async def _iter_sse_messages(response: httpx.Response):
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data:
                    reply = _decode_json_message(data.encode())
                    if reply is None:
                        raise RemoteProtocolError("remote MCP returned invalid SSE data")
                    yield reply
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        reply = _decode_json_message("\n".join(data_lines).encode())
        if reply is None:
            raise RemoteProtocolError("remote MCP returned invalid SSE data")
        yield reply


def _decode_json_message(body: bytes) -> dict | None:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        return None
    if "method" in value:
        return value if isinstance(value["method"], str) else None
    if "id" not in value or ("result" in value) == ("error" in value):
        return None
    return value


def _jsonrpc_error(
    request_id: object, code: int, message: str, data: object | None = None
) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }


def _project_bootstrap_error_reply(request_id: object, code: str) -> dict:
    """The standard MCP tool-call error shape (`isError=true` + text
    content) -- synthesized locally so a host cannot tell this apart from
    the same failure the remote server would eventually report itself."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {"type": "text", "text": f"{code}: project bootstrap failed at startup"}
            ],
            "isError": True,
        },
    }


def run_stdio_bridge(
    url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    instructions: str,
    *,
    workspace_id: str | None = None,
    project_bootstrap_error: str | None = None,
) -> None:
    bridge = StdioHttpBridge(
        url,
        api_key,
        slug=slug,
        locator=locator,
        workspace_id=workspace_id,
        instructions=instructions,
        project_bootstrap_error=project_bootstrap_error,
    )
    asyncio.run(bridge.serve())


def fetch_context(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    timeout: float = CONTEXT_TIMEOUT_SECONDS,
    *,
    tier: str = "index",
    host: str | None = None,
    workspace_id: str | None = None,
) -> dict | None:
    """The bounded context response, or None -- never an exception.

    Bounded and silent on purpose: this runs before the host's first prompt,
    so a slow or broken memory service must cost a session its brief and
    nothing else. The caller supplies the small fallback instruction when this
    returns ``None``.
    """
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/v1/context/load",
            json={"project_slug": slug, "workspace_id": workspace_id},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(body, dict) and isinstance(body.get("text"), str):
        return {"instructions": body["text"]}
    return None


def _cache_path(
    base_url: str, slug: str | None, locator: str | None, workspace_id: str | None = None
) -> Path:
    """One private cache file per memory service, project and workspace.

    A credential never contributes to a filename: filenames are observable
    metadata, while the cache content itself is protected because it holds the
    current user's memory. workspace_id joins the digest only when resolved,
    so two git worktrees of the same project never share a cache file, while
    the existing no-workspace digest is unchanged -- a resolved workspace
    must never read or overwrite that cache, which could replay another
    worktree's state, but a session outside any worktree still finds the
    cache file it always has.
    """
    root = Path(
        os.environ.get("ACH_MEMORY_CACHE_DIR")
        or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ach-memory"
    )
    key = f"{base_url}|{slug or ''}|{locator or ''}"
    if workspace_id:
        key = f"{key}|{workspace_id}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return root / f"index-{digest}.txt"


def _cache_owner(api_key: str) -> str:
    """A private cache-record fingerprint, never a filename component."""
    return hashlib.sha256(api_key.encode()).hexdigest()


@dataclass(frozen=True)
class CachedIndex:
    instructions: str
    stored_at: datetime


def load_cached_index(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    workspace_id: str | None = None,
) -> CachedIndex | None:
    """Return a last-good index, if this host can safely read one."""
    try:
        record = json.loads(_cache_path(base_url, slug, locator, workspace_id).read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    owner = record.get("owner")
    instructions = record.get("instructions")
    if not isinstance(owner, str) or not isinstance(instructions, str):
        return None
    if not hmac.compare_digest(owner, _cache_owner(api_key)):
        return None
    if not instructions:
        return None
    stored_at = None
    if record.get("version") == 2:
        value = record.get("stored_at")
        if isinstance(value, str):
            try:
                stored_at = datetime.fromisoformat(value)
                if stored_at.tzinfo is None:
                    stored_at = None
            except ValueError:
                pass
    if stored_at is None:
        try:
            stored_at = datetime.fromtimestamp(
                _cache_path(base_url, slug, locator, workspace_id).stat().st_mtime, UTC
            )
        except (OSError, ValueError, OverflowError):
            return None
    return CachedIndex(instructions=instructions, stored_at=stored_at.astimezone(UTC))


def store_cached_index(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    instructions: str,
    *,
    stored_at: datetime | None = None,
    workspace_id: str | None = None,
) -> None:
    """Atomically replace the private last-good index, or quietly give up."""
    path = _cache_path(base_url, slug, locator, workspace_id)
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "w") as file:
            timestamp = (stored_at or datetime.now(UTC)).astimezone(UTC)
            json.dump(
                {
                    "version": 2,
                    "owner": _cache_owner(api_key),
                    "stored_at": timestamp.isoformat(),
                    "instructions": instructions,
                },
                file,
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        # A read-only home directory must cost this session its cache, not its
        # MCP server. A later session may run somewhere writable.
        pass
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _refresh_cached_index(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    workspace_id: str | None = None,
) -> None:
    brief = fetch_context(base_url, api_key, slug, locator, tier="index", workspace_id=workspace_id)
    if brief:
        store_cached_index(
            base_url, api_key, slug, locator, brief["instructions"], workspace_id=workspace_id
        )


def _stamp_or_append_cache_age(instructions: str, age_seconds: int) -> str:
    """Make a served cache's age visible, whichever protocol compiled it.

    A payload compiled under protocol 2 already reserves a cache-age slot;
    stamping it costs no budget. A payload compiled before that slot existed
    has nowhere to put the number, so append one compact line instead and
    trim only complete trailing lines -- never the header, never a partial
    line -- until it fits SMALLEST_BUDGET. A cache entry must never be served
    with its age invisible.
    """
    age_line = f"cached-index age {age_seconds}s"
    lines = instructions.split("\n")
    while len(lines) > 1 and len("\n".join([*lines, age_line])) > 512:
        lines.pop()
    return "\n".join([*lines, age_line])


def startup_instructions(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    *,
    refresh: bool = True,
    now: datetime | None = None,
    workspace_id: str | None = None,
) -> str:
    """Return a cached index immediately and refresh it for the next session.

    With no cache, the one bounded request is the best available orientation.
    A total failure returns an explicit stub so the agent knows memory may
    exist and can use ``recall`` after startup.
    """
    cached = load_cached_index(base_url, api_key, slug, locator, workspace_id)
    if cached:
        if refresh:
            threading.Thread(
                target=_refresh_cached_index,
                args=(base_url, api_key, slug, locator, workspace_id),
                daemon=True,
            ).start()
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        age_seconds = int(max((instant - cached.stored_at).total_seconds(), 0))
        return _stamp_or_append_cache_age(cached.instructions, age_seconds)

    fetched = fetch_context(base_url, api_key, slug, locator, tier="index", workspace_id=workspace_id)
    if fetched:
        instructions = fetched["instructions"]
        store_cached_index(base_url, api_key, slug, locator, instructions, workspace_id=workspace_id)
        return instructions
    return "[ach-memory] Standing context unavailable; recall still works."
