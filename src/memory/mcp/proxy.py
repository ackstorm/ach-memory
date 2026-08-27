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

import os
import subprocess

from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware, MiddlewareContext


def resolve_project_context(cwd: str | None = None) -> tuple[str | None, str | None]:
    """SPEC §8 order: MEMORY_PROJECT, else the repo's origin URL, else nothing.

    Returns (project_slug, git_locator); at most one is set. The raw origin
    URL is sent as git_locator -- canonicalization and the digest suffix are
    the server's job (projects.resolve), same as for any other caller.
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
        )
    except (OSError, subprocess.TimeoutExpired):
        # No git on PATH (or a hung filesystem): same outcome as no repo.
        return None, None
    locator = result.stdout.strip()
    if result.returncode != 0 or not locator:
        return None, None
    return None, locator


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
    elif locator:
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


def build_proxy(url: str, api_key: str) -> FastMCP:
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    proxy = create_proxy(transport, name="ach-memory")
    proxy.add_middleware(ProjectContextMiddleware())
    return proxy
