import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types.version import KNOWN_PROTOCOL_VERSIONS
from sqlalchemy import text
from sqlalchemy.orm import Session

# Imported unconditionally (not gated on metrics_enabled) so the collectors
# register with the default REGISTRY and instrumentation runs regardless of
# whether the /metrics scrape endpoint is exposed; metrics_enabled only
# controls the endpoint below.
from memory import activity, db, metrics
from memory.api.observability import ObservabilityMiddleware
from memory.auth.principal import Principal, resolve_principal
from memory.config import get_settings
from memory.db import get_session
from memory.errors import DomainError, Forbidden

logger = logging.getLogger("memory.api")


class NegotiatedProtocolMCP:
    """Refuse only a protocol revision the SDK does not know.

    On `initialize` the header is absent by construction -- a handshake client
    cannot name a version it has not negotiated yet -- so demanding the newest
    revision here rejected every mcp 1.x client on its very first request.
    Measured against LiteLLM v1.99.1 (mcp SDK 1.28.1), whose `MCPSpecVersion`
    offers 2024-11-05, 2025-03-26 and 2025-06-18 and nothing newer: its
    `initialize` came back 400 and its tool listing failed with "Failed to
    fetch MCP tools". No configuration on the caller's side could have fixed
    it, because the per-request revision does not exist in mcp 1.x at all.

    Statelessness is not what the gate was protecting. Under `stateless_http`
    the handshake era negotiates and then answers with no `mcp-session-id`
    either, so serving it pins no caller to a pod (verified for all three
    revisions above). Authentication is unaffected: `tool_session` reads the
    request's own headers whichever revision carried them.

    Non-POST is the SDK's business, not this layer's: it answers the GET
    stream and DELETE according to the revision the caller negotiated, and a
    405 here pre-empted a request this wrapper cannot interpret.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", ()))
            version = headers.get(b"mcp-protocol-version", b"").decode(
                "ascii", errors="ignore"
            )
            # Absent stays legal; only a value the SDK cannot serve is refused,
            # so a typo or a future revision fails loudly instead of being
            # silently negotiated down to something the caller did not ask for.
            if version and version not in KNOWN_PROTOCOL_VERSIONS:
                response = JSONResponse(
                    status_code=400,
                    content={
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32020,
                            "message": "unsupported MCP-Protocol-Version",
                            "data": {"supported": list(KNOWN_PROTOCOL_VERSIONS)},
                        },
                    },
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _platform_token(request: Request) -> str | None:
    """The platform credential, read by a name that comes from configuration.

    FastAPI maps a parameter name to a fixed header, so a configurable header
    has to be read off the Request. Returns None when the provider is off, so
    a stray header on an unconfigured deployment is simply not a credential.
    """
    settings = get_settings()
    if not settings.auth_platform_enabled:
        return None
    # First configured header present on the request wins (order is priority):
    # the ACH gateway sends `x-litellm-api-key`, a local stdio client sends
    # `Authorization` -- one deployment serves both by listing both.
    for header in settings.incoming_headers:
        raw = request.headers.get(header)
        if raw is None:
            continue
        value = raw.strip()
        # LiteLLM's own header carries the prefix, and so does Authorization;
        # the resolver must receive the bare key.
        if value.lower().startswith("bearer "):
            value = value[len("bearer ") :].strip()
        if value:
            return value
    return None


def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_session),
) -> Principal:
    return resolve_principal(
        authorization, db, platform_token=_platform_token(request)
    )


def require_master(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_master:
        raise Forbidden("this operation requires operator authority")
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
    """The subject an operator is acting for (SPEC §16.5).

    Ignored for everyone else. Delegation is an operator capability, and an
    ordinary caller that sets the header would otherwise write an unverified
    claim into the audit trail — which is the one place a claim must not be
    taken on trust. It is provenance, never authorization evidence.
    """
    return on_behalf_of if principal.is_master else None


def _assert_master_config_cannot_over_grant() -> None:
    """Refuse to start rather than hand every bank to everyone.

    An empty default is not enough on its own. `config._id_set` discards
    empty entries, so an unset MEMORY_MASTER_USERS grants nobody today -- but
    if that parsing ever loosens, `""` lands in the granted set and matches a
    principal with no id, and nothing anywhere reports it. Every other
    misconfiguration in this service announces itself; over-granting is
    silent, so it gets the one signal loud enough to be noticed: a container
    that will not start.
    """
    settings = get_settings()
    if "" in settings.master_user_ids or "" in settings.master_group_ids:
        raise RuntimeError(
            "MEMORY_MASTER_USERS/MEMORY_MASTER_GROUPS parsed to an empty "
            "entry, which would grant operator authority to a principal with "
            "no identity. Refusing to start."
        )
    # A subject is only unique within the issuer that minted it, and which
    # provider authenticates a caller is decided by the token's own shape
    # (`resolve_principal`), not by anything configuration pins down. So with
    # both providers enabled, `MEMORY_MASTER_USERS=a@b.com` naming a JWT
    # subject is equally satisfied by anyone holding a platform credential
    # whose resolver returns that same string -- a privilege escalation with
    # no bad credential anywhere in it. Naming the issuer disambiguates; not
    # naming it, when it matters, is a refusal to start.
    granting = settings.master_user_ids or settings.master_group_ids
    providers = sum([settings.auth_jwt_enabled, settings.auth_platform_enabled])
    if granting and providers > 1 and not settings.master_issuer_value:
        raise RuntimeError(
            "MEMORY_MASTER_USERS/MEMORY_MASTER_GROUPS grant operator "
            "authority while more than one identity provider is enabled, and "
            "MEMORY_MASTER_ISSUER does not say which one may grant it. A "
            "subject or group id asserted by either provider would match. "
            "Set MEMORY_MASTER_ISSUER to the JWT issuer or the platform "
            "resolver URL. Refusing to start."
        )
    logger.info(
        "operator authority: %d user(s), %d group(s), issuer=%s",
        len(settings.master_user_ids),
        len(settings.master_group_ids),
        settings.master_issuer_value or "(any enabled provider)",
    )


def create_app() -> FastAPI:
    _assert_master_config_cannot_over_grant()

    from memory.api import activity as activity_routes
    from memory.api import admin as admin_routes
    from memory.api import bootstrap as bootstrap_routes
    from memory.api import context as context_routes
    from memory.api import curation as curation_routes
    from memory.api import directives as directive_routes
    from memory.api import documents as document_routes
    from memory.api import memory as memory_routes
    from memory.api import mental_models as mental_model_routes
    from memory.api import operations as operation_routes
    from memory.api import projects as project_routes
    from memory.api import read as read_routes
    from memory.api import working_state as working_state_routes
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
        activity.set_error(exc.code)
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
        activity.set_error("INTERNAL_ERROR")
        logger.error("unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "internal error"}},
        )

    app.include_router(bootstrap_routes.router)
    app.include_router(activity_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(curation_routes.router)
    app.include_router(document_routes.router)
    app.include_router(operation_routes.router)
    app.include_router(project_routes.router)
    app.include_router(read_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(directive_routes.router)
    app.include_router(mental_model_routes.router)
    app.include_router(working_state_routes.router)
    app.include_router(context_routes.router)

    # Kubernetes probes. Unauthenticated on purpose: a kubelet carries no
    # bearer token, and neither route discloses anything a caller who can
    # already open the port does not know.
    @app.get("/health", include_in_schema=False)
    def health() -> Response:
        """Liveness: the process is up and serving.

        Deliberately touches no dependency. A liveness probe that fails on a
        database blip restarts every replica at once, turning a recoverable
        outage into a crash loop -- readiness is what takes a pod out of the
        load balancer, and it is below.
        """
        return JSONResponse({"status": "ok"})

    @app.get("/ready", include_in_schema=False)
    def ready() -> Response:
        """Readiness: this replica can actually serve, i.e. the database
        answers. `get_engine` sets `pool_pre_ping`, so `connect()` validates
        the connection rather than handing back a dead pooled one.

        The body never carries the reason. This endpoint is reachable by
        anything that can open the port, and a DSN or driver message in it is
        a gift to whoever is scanning; the detail goes to the log instead.
        """
        try:
            with db.get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            logger.warning("readiness probe failed", exc_info=True)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    if get_settings().metrics_enabled:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        @app.get("/metrics", include_in_schema=False)
        def prometheus_metrics() -> Response:  # Name does not shadow memory.metrics module (line 15)
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if get_settings().admin_ui_enabled:
        from pathlib import Path

        from fastapi.responses import FileResponse

        dashboard = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"

        @app.get("/admin/ui", include_in_schema=False)
        def admin_ui() -> FileResponse:
            # One file, read from the package. No StaticFiles mount: a mount
            # serves a whole directory, and this directory should never gain
            # a second servable file by accident.
            return FileResponse(dashboard, media_type="text/html")

    # DNS-rebinding protection is on by default in the SDK and allows only
    # 127.0.0.1, so a deployed service behind an ingress would answer 421 to
    # every MCP call. Configured rather than disabled: the check is worth
    # keeping, it just has to know the hostname it is deployed under.
    allowed = [h.strip() for h in get_settings().mcp_allowed_hosts.split(",") if h.strip()]
    # Logged at startup, not left to the first failed call: a Host mismatch
    # answers 421 from inside the SDK, whose body we do not control, and
    # /health is deliberately dependency-blind so the pod still reports ready.
    # Without this line the only symptom is an MCP call failing with no
    # server-side trace of why.
    logger.info("mcp transport allows hosts: %s", ", ".join(allowed))
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        # ach-memory emits request/response tool results only. JSON mode
        # avoids opening an SSE stream for exchanges that never publish
        # progress or subscriptions, while remaining Streamable HTTP.
        json_response=True,
        # Stateless whichever revision the caller negotiates: every tool
        # re-authenticates from its own headers and opens its own DB unit, so
        # a session id would pin a caller to one pod while carrying nothing.
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed,
            # v1 supports native/non-browser MCP clients only; browser
            # Origin support stays off until a tested requirement exists.
            # Host values are not origins, so leave this SDK default empty.
        ),
    )
    app.mount("/mcp", NegotiatedProtocolMCP(mcp_app))
    return app
