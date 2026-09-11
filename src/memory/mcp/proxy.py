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
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
from mcp.shared.inbound import (
    encode_header_value,
    find_invalid_x_mcp_header,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp_types.version import KNOWN_PROTOCOL_VERSIONS

from memory.errors import ProjectInvalidSlug
from memory.slugs import canonical_locator, slug_from_locator

CONTEXT_TIMEOUT_SECONDS = 2.0


def auth_headers(api_key: str) -> dict[str, str]:
    """The outgoing header that carries the credential to the service.

    `ACH_MEMORY_HEADER` names it, default `Authorization`. Configurable
    because a proxy in front of the service (toolhive, stacklok/toolhive#6394)
    can strip or block `Authorization` as a passthrough header while a custom
    one such as `x-litellm-api-key` passes untouched. The service accepts the
    key from any header its `MEMORY_AUTH_PLATFORM_INCOMING_HEADER` lists, so
    the two only have to agree on a name.

    `Authorization` gets the conventional `Bearer ` prefix; any other header
    carries the bare key. The service strips a `bearer ` prefix off whichever
    header it reads, so the bare key is always accepted -- the prefix is only
    what an `Authorization` consumer expects to see.
    """
    header = (os.environ.get("ACH_MEMORY_HEADER") or "Authorization").strip() or "Authorization"
    if header.lower() == "authorization":
        return {"Authorization": f"Bearer {api_key}"}
    return {header: api_key}


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
    if not locator:
        return None, None
    try:
        canonical = canonical_locator(locator)
    except ProjectInvalidSlug:
        return None, None
    return slug_from_locator(canonical), canonical


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
            headers=auth_headers(api_key),
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


# Tools that carry no `scope` argument at all, so fill_project_arguments' scope
# gate can never fire for them and they have to be filled directly. Called
# bare, load_context would otherwise resolve no project and silently return
# user-only standing context -- no error, and nothing in `omissions`.
_SCOPELESS_TOOLS = frozenset(
    {
        "start_working_session",
        "set_working_state",
        "clear_working_state",
        "load_context",
    }
)
# Tools that must never be sent a git_locator: recall and memory_history are
# not bound to one repository, and load_context has no such parameter to fill.
_READ_TOOLS = frozenset({"recall", "memory_history", "load_context"})


def fill_working_state_arguments(
    arguments: dict,
    slug: str | None,
    locator: str | None,
    workspace_id: str | None,
) -> None:
    """Inject project/locator/workspace into a bare working-state call, in
    place. Explicit values from the model always win -- same reasoning as
    fill_project_arguments, just with no `scope` gate: these tools carry
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
    """Forward MCP requests between stdio and Streamable HTTP.

    Whatever revision the host speaks is the revision this bridge carries.
    The per-request protocol introduced in 2026-07-28 names it in each
    request's `_meta`; a handshake host names it once on `initialize` and
    never again, so the negotiated value is remembered from the remote's own
    reply and sent as the header on every later request. Demanding the
    per-request field unconditionally rejected every handshake host on its
    very first message -- `initialize` cannot carry a revision that has not
    been negotiated yet, so `requested` was always null and no MCP host
    (Claude Code and the mcp SDK's own `ClientSession` included) could reach
    the remote at all.

    There is still no transport session to translate: the remote is
    stateless, so the handshake is forwarded like any other request and the
    notifications that follow it are answered by neither side.
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
        # Set from the remote's `initialize` reply -- the only authority on
        # what was actually negotiated -- and sent as the per-request header
        # for hosts whose requests do not carry one themselves.
        self._negotiated_version: str | None = None
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
                locator = None if tool_name in _READ_TOOLS else self._locator
                if tool_name in _SCOPELESS_TOOLS:
                    fill_working_state_arguments(
                        arguments, self._slug, locator, self._workspace_id
                    )
                else:
                    fill_project_arguments(arguments, self._slug, locator)

                if self._project_bootstrap_error and arguments.get("scope") == "project":
                    yield _project_bootstrap_error_reply(
                        outgoing.get("id"), self._project_bootstrap_error
                    )
                    return

        headers = {
            **auth_headers(self._api_key),
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        requested = _protocol_version(outgoing)
        if requested is not None and requested not in KNOWN_PROTOCOL_VERSIONS:
            raise UnsupportedProtocolVersion(requested)
        method = outgoing.get("method")
        # `initialize` is what negotiates the revision, so it carries no
        # header: naming one here would pin the exchange to a revision the
        # caller never agreed to. The remote serves the body's own
        # `protocolVersion` and answers with what it settled on.
        version = None if method == "initialize" else (
            requested or self._negotiated_version
        )
        if version is not None:
            headers["MCP-Protocol-Version"] = version
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
        if method == "initialize":
            self._absorb_handshake(reply)
        elif method == "server/discover":
            self._replace_discovery_instructions([reply], request_id)
        elif method == "tools/list":
            self._absorb_tool_listing(reply)

    def _absorb_handshake(self, reply: dict) -> None:
        """Remember the negotiated revision and carry our own instructions.

        `initialize` is where a handshake host reads instructions, the same
        way the 2026-07-28 era reads them from `server/discover`, so the
        fetched context has to be substituted in both or it reaches only one
        kind of host.
        """
        result = reply.get("result")
        if not isinstance(result, dict):
            return
        version = result.get("protocolVersion")
        if isinstance(version, str):
            self._negotiated_version = version
        # Empty is what a fail-open context fetch yields (a timeout, or a
        # workspace with nothing standing), and substituting it would delete
        # the remote's own operating guidance -- the only instructions a
        # handshake host ever sees -- in exchange for nothing.
        if self._instructions:
            result["instructions"] = self._instructions

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
                            "supported": list(KNOWN_PROTOCOL_VERSIONS),
                            "requested": exc.requested,
                        },
                    )
                ]
            except InvalidMCPRequest as exc:
                replies = [_jsonrpc_error(request_id, -32600, str(exc))]
            except (RemoteProtocolError, httpx.HTTPError) as exc:
                print(f"ach-memory: remote MCP request failed: {exc}", file=sys.stderr)
                replies = [
                    _jsonrpc_error(
                        request_id,
                        -32000,
                        "Remote MCP request failed",
                        _remote_failure_data(exc, self._url),
                    )
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
                # A notification has no id and must never be answered.
                # `notifications/initialized` follows every handshake, and
                # replying to it with an error -- which is what falling
                # through to `handle` did -- is a protocol violation the host
                # sees immediately after connecting.
                if "id" not in message and message.get("method") != (
                    "notifications/cancelled"
                ):
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
    """The revision this one request is being made under, if it names one.

    The 2026-07-28 era carries it per request in `_meta`. On `initialize`
    nothing can be there yet, so the request's own `params.protocolVersion`
    -- the revision the host is asking for -- is the only value to read.
    """
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    version = (
        meta.get("io.modelcontextprotocol/protocolVersion")
        if isinstance(meta, dict)
        else None
    )
    if version is None and message.get("method") == "initialize":
        version = params.get("protocolVersion")
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


def _remote_failure_data(exc: Exception, url: str) -> dict:
    """What the caller needs to tell one remote failure from another.

    -32000 covers a wrong endpoint, DNS, TLS, 401, 404 and every 5xx alike,
    and the detail went only to stderr -- which no MCP host shows. A Codex
    install whose `--url` carried the /mcp/ mount twice therefore reported
    nothing but "Remote MCP request failed" on every session, and the 404
    behind it took a packet capture's worth of digging to name (2026-09-07).

    The endpoint is safe to return: the credential travels in a header, and
    a URL the caller configured is not something it needs protecting from.
    """
    data: dict[str, object] = {"url": url, "reason": type(exc).__name__}
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        data["status"] = status
    return data


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


def fetch_context(
    base_url: str,
    api_key: str,
    slug: str | None,
    timeout: float = CONTEXT_TIMEOUT_SECONDS,
    *,
    workspace_id: str | None = None,
) -> dict | None:
    """The bounded context response, or None -- never an exception.

    Bounded and silent on purpose: this runs before the host's first prompt,
    so a slow or broken memory service can only omit standing context. The
    host still starts normally when this returns ``None``.
    """
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/v1/context/load",
            json={"project_slug": slug, "workspace_id": workspace_id},
            headers=auth_headers(api_key),
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(body, dict) and isinstance(body.get("text"), str):
        return body
    return None
