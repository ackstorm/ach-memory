"""Request-scoped observability, wired as a pure ASGI middleware.

Pure ASGI rather than BaseHTTPMiddleware for one reason that matters later:
Task 6 puts a mutable per-call record in a ContextVar here, before calling
downstream, and reads it back afterwards. BaseHTTPMiddleware runs the
downstream app in a separate task, and the sync routes below it run in a
threadpool -- in both cases the child gets a COPY of the context, so a
`.set()` performed down there never reaches us. Passing a mutable dict DOWN
and mutating it is what survives both boundaries.
"""

from memory import metrics


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

        try:
            await self.app(scope, receive, _send)
        finally:
            # Set by Starlette's router once a path matches. "unmatched" for
            # everything else: without the fallback, every 404 on an invented
            # URL would be its own label value.
            route = scope.get("route")
            metrics.HTTP.labels(
                route=getattr(route, "path", "unmatched"),
                method=scope.get("method", "-"),
                status=str(status),
            ).inc()
