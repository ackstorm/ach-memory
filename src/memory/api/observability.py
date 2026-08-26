"""Request-scoped observability, wired as a pure ASGI middleware.

Pure ASGI rather than BaseHTTPMiddleware for one reason that matters later:
Task 6 puts a mutable per-call record in a ContextVar here, before calling
downstream, and reads it back afterwards. BaseHTTPMiddleware runs the
downstream app in a separate task, and the sync routes below it run in a
threadpool -- in both cases the child gets a COPY of the context, so a
`.set()` performed down there never reaches us. Passing a mutable dict DOWN
and mutating it is what survives both boundaries.
"""

from memory import activity, metrics

# ponytail: our own mount prefixes, not caller input -- still a closed set.
# Plain `starlette.routing.Mount` (used for the /mcp sub-app) never sets
# scope["route"]; that's only set by FastAPI's APIRoute. Without this
# fallback every real MCP call -- all fifteen tools are POST /mcp -- would
# collapse into "unmatched" next to genuine 404 probing.
_MOUNT_PREFIXES = ("/mcp",)


def _route_label(scope) -> str:
    route = scope.get("route")
    if route is not None:
        return route.path
    path = scope.get("path", "")
    for prefix in _MOUNT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return prefix
    return "unmatched"


class ObservabilityMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 500 is the honest default: if the response never starts, the client
        # got nothing, and recording that as anything else would hide it.
        status = 500

        async def _send(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        activity.new_call()
        try:
            await self.app(scope, receive, _send)
        finally:
            activity.finish("rest")
            # scope["route"] is set by Starlette's router once a path
            # matches -- but only FastAPI's APIRoute does that; a plain
            # Mount (the /mcp sub-app) never does. "unmatched" is the final
            # fallback for everything else: without it, every 404 on an
            # invented URL would be its own label value.
            metrics.HTTP.labels(
                route=_route_label(scope),
                method=scope.get("method", "-"),
                status=str(status),
            ).inc()
