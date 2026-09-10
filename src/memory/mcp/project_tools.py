"""Project ownership MCP tools. `projects.transfer` (projects.py) already
implements the domain operation; this is only its MCP exposure."""

import logging

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import ValidationError

from memory import activity, metrics, projects
from memory.errors import DomainError
from memory.mcp.server import tool_session
from memory.mcp.tools import (
    REGISTRY,
    MCPToolError,
    ToolResult,
    _internal_error,
    _invalid_request,
)

logger = logging.getLogger("memory.mcp")


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Transfer ownership of a project to a different user or group. "
            "Any authorized caller may do this -- v1 has no separate "
            "permission model, so a single group member can transfer a "
            "group-owned project to themselves and lock the rest of the "
            "group out; the audit trail is the mitigation."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def transfer(
        project_slug: str,
        owner_type: str,
        owner_id: str,
        ctx: Context,
    ) -> ToolResult:
        activity.new_call()
        try:
            with tool_session(ctx) as tc:
                # Resolve the slug first, then authorize -- exactly as the
                # REST route does (api/projects.py's transfer_project).
                # Calling projects.authorize directly here would raise
                # ProjectAccessDenied, carrying owner_type, and break Task
                # 5's invariant on the one surface that most invites the
                # shortcut: resolve()'s own _authorize_resolution collapses
                # a foreign project into the same PROJECT_NOT_FOUND an
                # absent one raises. projects.transfer's own authorize()
                # call is defense in depth, never reached as the first
                # denial for a caller that only knows the slug.
                #
                # `projects.transfer` applies SPEC §20's write ceiling itself,
                # for both surfaces at once -- transfer resolves no bank, so
                # there is no `_resolve_bank` here to carry it, nor to fill in
                # the activity row below.
                result = projects.resolve(
                    tc.db, tc.principal, project_slug, create=False
                )
                project = projects.transfer(
                    tc.db, tc.principal, result.project, owner_type, owner_id,
                )
                activity.describe(
                    action="projects.transfer",
                    scope="project",
                    tenant_id=tc.principal.tenant_id,
                    credential_id=tc.principal.credential_id,
                    # The RESOLVED slug, never the caller's raw argument --
                    # same rule `_describe` follows, for the same reason.
                    project_slug=result.current_slug,
                    bank_fingerprint=activity.fingerprint(result.project.bank_id),
                )
                tc.db.commit()
                return ToolResult(
                    result={
                        "project_slug": result.current_slug,
                        "owner_type": project.owner_type,
                        "owner_id": project.owner_id,
                    },
                    resolved_from=result.resolved_from,
                    notice="PROJECT_RENAMED" if result.resolved_from else None,
                )
        except DomainError as exc:
            metrics.ERRORS.labels(code=exc.code).inc()
            activity.set_error(exc.code)
            raise MCPToolError(exc.code, exc.message, exc.details) from None
        except ValidationError as exc:
            metrics.ERRORS.labels(code="INVALID_REQUEST").inc()
            activity.set_error("INVALID_REQUEST")
            raise _invalid_request(exc) from None
        except MCPToolError as exc:
            activity.set_error(getattr(exc, "code", "INTERNAL_ERROR"))
            raise
        except Exception as exc:
            logger.error("unhandled MCP tool error", exc_info=exc)
            metrics.ERRORS.labels(code="INTERNAL_ERROR").inc()
            activity.set_error("INTERNAL_ERROR")
            raise _internal_error() from None
        finally:
            activity.finish("mcp")

    REGISTRY["transfer"] = transfer
