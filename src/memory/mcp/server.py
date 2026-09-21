"""The MCP server: build the FastMCP app, mount it under Starlette with `/health` (SPEC §6)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from memory import __version__
from memory.config import get_settings
from memory.mcp.tools import register

logger = logging.getLogger("memory.mcp")

INSTRUCTIONS = (
    "Durable memory across sessions and context resets: the system of record for what was "
    "decided, preferred or learned. `scope` selects whose memory: 'user' is your own, "
    "'project' is the shared memory of the project named by `project_slug`. Never store "
    "credentials, tokens or keys. Write in English whatever language the conversation uses: "
    "retrieval reranks in English only."
)


def build_mcp() -> MCPServer:
    mcp = MCPServer(name="ach-memory", instructions=INSTRUCTIONS, version=__version__)
    register(mcp)
    return mcp


class _BareMcpPath:
    """Serve the MCP mount's bare `/mcp` path instead of Starlette's 307 to `/mcp/`.

    A gateway in front of this service can rewrite the scheme/prefix on the way in, so the
    Location Starlette would compute for the redirect is wrong, and the SDK's client follows
    redirects by default -- a credentialed POST would follow it. Registered as outer
    middleware (not a wrapper around the mounted sub-app): the router's own redirect-slash
    check runs before a mount's app is ever called, so the scope has to be rewritten before
    routing, not after.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# The Dockerfile's COPY destination. `tests/test_plugins.py` holds the two ends together.
PLUGIN_TARBALL = Path("/app/plugin.tgz")


async def _plugin(request: Request) -> Response:
    """Serve the npm bundle so a host can install over plain HTTPS, with no git and no registry.

    opencode accepts a URL as a `plugin` entry and hands it to bun, which expects an
    `npm pack` tarball. The bytes are the deployed release's own bundle, so the endpoint
    can never drift from the service the plugin is configured against. Unauthenticated
    like `/health`: `authenticate()` runs in the MCP tool layer, not as middleware, and
    bun sends no credentials anyway.

    `cache-control: no-cache` is what makes a release reach anyone who already installed.
    The URL never changes, and with no directive npm's request cache (`~/.npm/_cacache`)
    falls back to heuristic freshness off `last-modified`: an old tarball is held fresh
    for long enough that a new release is answered from the client's own cache without
    the server ever being asked. `no-cache` means revalidate, not "do not store" -- the
    etag still yields a 304, so an unchanged bundle costs one conditional request.
    """
    if not PLUGIN_TARBALL.is_file():
        # A dev run outside the image has no tarball. 404 beats refusing to start.
        return JSONResponse({"error": "no plugin tarball in this deployment"}, status_code=404)
    return FileResponse(
        PLUGIN_TARBALL, media_type="application/gzip", headers={"cache-control": "no-cache"}
    )


def create_app() -> Starlette:
    settings = get_settings()
    # A Host mismatch answers 421 from inside the SDK with no other trace, and /health is
    # deliberately dependency-blind, so this is the only server-side record of the setting.
    logger.info("mcp transport allows hosts: %s", ", ".join(settings.mcp_allowed_hosts))

    mcp_app = build_mcp().streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=settings.mcp_allowed_hosts),
    )

    # mcp_app's own `lifespan=` starts its session manager's task group; Starlette never
    # runs a mounted sub-app's lifespan on its own, so every request would otherwise hit
    # "Task group is not initialized" -- this app's lifespan enters it in turn.
    @asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = Starlette(
        routes=[Route("/health", _health), Route("/plugin", _plugin)],
        middleware=[Middleware(_BareMcpPath)],
        lifespan=_lifespan,
    )
    app.mount("/mcp", mcp_app)
    return app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
