import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session

# Imported unconditionally (not gated on metrics_enabled) so the collectors
# register with the default REGISTRY and instrumentation runs regardless of
# whether the /metrics scrape endpoint is exposed; metrics_enabled only
# controls the endpoint below.
from memory import metrics
from memory.api.observability import ObservabilityMiddleware
from memory.auth.principal import Principal, resolve_principal
from memory.config import get_settings
from memory.db import get_session
from memory.errors import DomainError, Forbidden

logger = logging.getLogger("memory.api")


def _platform_token(request: Request) -> str | None:
    """The platform credential, read by a name that comes from configuration.

    FastAPI maps a parameter name to a fixed header, so a configurable header
    has to be read off the Request. Returns None when the provider is off, so
    a stray header on an unconfigured deployment is simply not a credential.
    """
    settings = get_settings()
    if not settings.auth_platform_enabled:
        return None
    raw = request.headers.get(settings.auth_platform_incoming_header)
    if raw is None:
        return None
    value = raw.strip()
    # LiteLLM's own header carries the prefix; the resolver must receive the
    # bare key.
    if value.lower().startswith("bearer "):
        value = value[len("bearer ") :].strip()
    return value or None


def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    # FastAPI maps this parameter name to the `x-ach-memory-key` header. It
    # takes precedence over Authorization when present -- see
    # memory.auth.principal.API_KEY_HEADER for why the dedicated header exists.
    x_ach_memory_key: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_session),
) -> Principal:
    return resolve_principal(
        authorization,
        db,
        api_key=x_ach_memory_key,
        platform_token=_platform_token(request),
    )


def require_master(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_master:
        raise Forbidden("this operation requires the master key")
    return principal


def current_on_behalf_of(
    principal: Annotated[Principal, Depends(current_principal)],
    # Bounded to match AuditEvent.on_behalf_of (String(128)) so an oversize
    # header is a typed 422 at the boundary, not a 500 from the DB -- same
    # reasoning as git_locator's bound in memory/api/memory.py. The pattern
    # excludes C0 controls and DEL for the same reason: unscreened, the value
    # flows to audit.record() -> AuditEvent.on_behalf_of -> INSERT, and a NUL
    # byte there is a psycopg DataError, not an IntegrityError, so it reaches
    # the catch-all as a 500. Confirmed live (FastAPI 0.141 / pydantic 2.13)
    # that `pattern` on a Header is enforced pre-route as a 422.
    on_behalf_of: Annotated[
        str | None, Header(max_length=128, pattern=r"^[^\x00-\x1f\x7f]*$")
    ] = None,
) -> str | None:
    """The subject a master key is acting for (SPEC §16.5).

    Ignored for a user key. Delegation is a master-key capability, and a user
    key that sets the header would otherwise write an unverified claim into the
    audit trail — which is the one place a claim must not be taken on trust.
    It is provenance, never authorization evidence.
    """
    return on_behalf_of if principal.is_master else None


def create_app() -> FastAPI:
    from memory.api import admin as admin_routes
    from memory.api import curation as curation_routes
    from memory.api import directives as directive_routes
    from memory.api import documents as document_routes
    from memory.api import groups as group_routes
    from memory.api import memory as memory_routes
    from memory.api import mental_models as mental_model_routes
    from memory.api import operations as operation_routes
    from memory.api import projects as project_routes
    from memory.api import users as user_routes
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register as register_tools

    mcp = build_mcp()
    register_tools(mcp)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Starlette does not run nested lifespans under a Mount, so the HOST
        # app must enter the session manager. Without this the server accepts
        # connections and then hangs — with no error to explain it.
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="ach-memory", version="0.1.0", lifespan=lifespan)
    app.add_middleware(ObservabilityMiddleware)

    # httpx logs the full request URL at INFO, and our Hindsight URLs carry the
    # bank ID. Silent today only because nothing configures the root logger —
    # one basicConfig(level=INFO) away from leaking it into every log line.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @app.exception_handler(DomainError)
    def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        metrics.ERRORS.labels(code=exc.code).inc()
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    **({"details": exc.details} if exc.details else {}),
                }
            },
        )

    @app.exception_handler(Exception)
    def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        """Last resort, so the error envelope is a contract and not a hope.

        Without this, anything that is not a DomainError — a driver
        IntegrityError, a bug — escapes to Starlette's default handler and the
        client gets plain text instead of {"error": {...}}. The message is
        deliberately fixed: never echo the exception, which can carry SQL,
        a connection string, or a bank ID.

        `logger.exception` reads `sys.exc_info()`, which is only populated
        inside an active `except` block. This handler runs from the
        exception-middleware's call site, not from one, so `exc_info()` is
        empty here and the log line was carrying "NoneType: None" instead of
        a traceback -- the one line meant to survive everything else logging
        nothing. Passing `exc` explicitly bypasses `sys.exc_info()` entirely.
        """
        metrics.ERRORS.labels(code="INTERNAL_ERROR").inc()
        logger.error("unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "internal error"}},
        )

    app.include_router(user_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(curation_routes.router)
    app.include_router(document_routes.router)
    app.include_router(operation_routes.router)
    app.include_router(group_routes.router)
    app.include_router(project_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(directive_routes.router)
    app.include_router(mental_model_routes.router)

    if get_settings().metrics_enabled:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        @app.get("/metrics", include_in_schema=False)
        def prometheus_metrics() -> Response:  # Name does not shadow memory.metrics module (line 15)
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # DNS-rebinding protection is on by default in the SDK and allows only
    # 127.0.0.1, so a deployed service behind an ingress would answer 421 to
    # every MCP call. Configured rather than disabled: the check is worth
    # keeping, it just has to know the hostname it is deployed under.
    allowed = [h.strip() for h in get_settings().mcp_allowed_hosts.split(",") if h.strip()]
    app.mount(
        "/mcp",
        mcp.streamable_http_app(
            streamable_http_path="/",
            transport_security=TransportSecuritySettings(
                allowed_hosts=allowed,
                # v1 supports native/non-browser MCP clients only; browser
                # Origin support stays off until a tested requirement exists.
                # Host values are not origins, so leave this SDK default empty.
            ),
        ),
    )
    return app
