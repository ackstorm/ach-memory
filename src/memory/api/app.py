import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, resolve_principal
from memory.config import get_settings
from memory.db import get_session
from memory.errors import DomainError, Forbidden

logger = logging.getLogger("memory.api")


def current_principal(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_session),
) -> Principal:
    return resolve_principal(authorization, db)


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
    # reasoning as git_locator's bound in memory/api/memory.py.
    on_behalf_of: Annotated[str | None, Header(max_length=128)] = None,
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

    # httpx logs the full request URL at INFO, and our Hindsight URLs carry the
    # bank ID. Silent today only because nothing configures the root logger —
    # one basicConfig(level=INFO) away from leaking it into every log line.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @app.exception_handler(DomainError)
    def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
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
                allowed_hosts=allowed, allowed_origins=allowed
            ),
        ),
    )
    return app
