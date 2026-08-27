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

from memory.errors import ProjectInvalidSlug
from memory.slugs import slug_from_locator


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


def build_proxy(url: str, api_key: str) -> FastMCP:
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    proxy = create_proxy(transport, name="ach-memory")
    proxy.add_middleware(ProjectContextMiddleware())
    return proxy
