"""Local stdio MCP server that forwards to the remote HTTP endpoint.

This is the client-side half SPEC §8 always assumed and nothing ever
shipped: "the MCP derives a slug from the current Git repository" cannot
run on the remote server, which sees a bearer token and JSON and nothing
else. Running as a stdio child of the agent host, this process has the
cwd, so it resolves MEMORY_PROJECT -> git origin -> nothing (SPEC §8
order) once at startup and fills the gap into project-scoped tool calls
the model left bare. Measured motivation: pi called
list_memories(scope="project") with neither param and got
PROJECT_CONTEXT_UNAVAILABLE with no way to recover (2026-08-27).

The remote HTTP endpoint stays first-class: this proxy adds arguments the
model omitted and forwards everything else verbatim, so a host talking
HTTP directly sees identical behavior minus the auto-fill.
"""

import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware, MiddlewareContext

from memory import brief
from memory.errors import ProjectInvalidSlug
from memory.slugs import slug_from_locator

BRIEF_TIMEOUT_SECONDS = 2.0


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
    """
    slug = os.environ.get("MEMORY_PROJECT")
    if slug:
        return slug, None
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # No git on PATH (or a hung filesystem): same outcome as no repo.
        return None, None
    locator = result.stdout.strip()
    if result.returncode != 0 or not locator:
        return None, None
    try:
        return slug_from_locator(locator), locator
    except ProjectInvalidSlug:
        # A remote that names no host and path -- a local clone, a bare path,
        # a bundle file. It identifies no project, and raising here would take
        # the whole session down at startup over a repository the agent may
        # never ask about. Same outcome as no repo at all: the server's error
        # tells the model what to pass.
        return None, None


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


class ProjectContextMiddleware(Middleware):
    """Resolves once at startup: the cwd of a stdio child never changes."""

    def __init__(self) -> None:
        self._slug, self._locator = resolve_project_context()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        arguments = context.message.arguments
        if isinstance(arguments, dict):
            fill_project_arguments(arguments, self._slug, self._locator)
        return await call_next(context)


def fetch_brief(
    base_url: str,
    api_key: str,
    slug: str | None,
    locator: str | None,
    timeout: float = BRIEF_TIMEOUT_SECONDS,
    *,
    tier: str = "index",
    host: str | None = None,
) -> dict | None:
    """The session brief, or None -- never an exception.

    Bounded and silent on purpose: this runs before the host's first prompt,
    so a slow or broken memory service must cost a session its brief and
    nothing else. Returning None leaves the proxy advertising no instructions
    of its own, which makes FastMCP forward the server's policy text verbatim.
    """
    params = {"scope": "user", "tier": tier}
    if slug:
        params["project_slug"] = slug
    if locator:
        params["git_locator"] = locator
    if host:
        params["host"] = host
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/v1/session-brief",
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) and body.get("instructions") else None


def _cache_path(base_url: str, slug: str | None, locator: str | None) -> Path:
    """One private cache file per memory service and project context.

    A credential never contributes to a filename: filenames are observable
    metadata, while the cache content itself is protected because it holds the
    current user's memory.
    """
    root = Path(
        os.environ.get("ACH_MEMORY_CACHE_DIR")
        or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        / "ach-memory"
    )
    digest = hashlib.sha256(
        f"{base_url}|{slug or ''}|{locator or ''}".encode()
    ).hexdigest()[:16]
    return root / f"index-{digest}.txt"


def _cache_owner(api_key: str) -> str:
    """A private cache-record fingerprint, never a filename component."""
    return hashlib.sha256(api_key.encode()).hexdigest()


@dataclass(frozen=True)
class CachedIndex:
    instructions: str
    stored_at: datetime


def load_cached_index(
    base_url: str, api_key: str, slug: str | None, locator: str | None
) -> CachedIndex | None:
    """Return a last-good index, if this host can safely read one."""
    try:
        record = json.loads(_cache_path(base_url, slug, locator).read_text())
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
                _cache_path(base_url, slug, locator).stat().st_mtime, UTC
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
) -> None:
    """Atomically replace the private last-good index, or quietly give up."""
    path = _cache_path(base_url, slug, locator)
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "w") as file:
            timestamp = (stored_at or datetime.now(UTC)).astimezone(UTC)
            json.dump({
                "version": 2,
                "owner": _cache_owner(api_key),
                "stored_at": timestamp.isoformat(),
                "instructions": instructions,
            }, file)
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
    base_url: str, api_key: str, slug: str | None, locator: str | None
) -> None:
    brief = fetch_brief(base_url, api_key, slug, locator, tier="index")
    if brief:
        store_cached_index(base_url, api_key, slug, locator, brief["instructions"])


def _stamp_or_append_cache_age(instructions: str, age_seconds: int) -> str:
    """Make a served cache's age visible, whichever protocol compiled it.

    A payload compiled under protocol 2 already reserves a cache-age slot;
    stamping it costs no budget. A payload compiled before that slot existed
    has nowhere to put the number, so append one compact line instead and
    trim only complete trailing lines -- never the header, never a partial
    line -- until it fits SMALLEST_BUDGET. A cache entry must never be served
    with its age invisible.
    """
    if brief.carries_cache_age(instructions):
        return brief.stamp_cache_age(instructions, age_seconds)

    age_line = f"cached-index age {age_seconds}s"
    lines = instructions.split("\n")
    while len(lines) > 1 and len("\n".join([*lines, age_line])) > brief.SMALLEST_BUDGET:
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
) -> str:
    """Return a cached index immediately and refresh it for the next session.

    With no cache, the one bounded request is the best available orientation.
    A total failure returns an explicit stub so the agent knows memory may
    exist and can use ``recall`` after startup.
    """
    cached = load_cached_index(base_url, api_key, slug, locator)
    if cached:
        if refresh:
            threading.Thread(
                target=_refresh_cached_index,
                args=(base_url, api_key, slug, locator),
                daemon=True,
            ).start()
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        age_seconds = int(max((instant - cached.stored_at).total_seconds(), 0))
        return _stamp_or_append_cache_age(cached.instructions, age_seconds)

    fetched = fetch_brief(base_url, api_key, slug, locator, tier="index")
    if fetched:
        instructions = fetched["instructions"]
        store_cached_index(base_url, api_key, slug, locator, instructions)
        return instructions
    return "[ach-memory] Session brief unavailable; recall still works."


def build_proxy(url: str, api_key: str) -> FastMCP:
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    proxy = create_proxy(transport, name="ach-memory")
    proxy.add_middleware(ProjectContextMiddleware())
    return proxy
