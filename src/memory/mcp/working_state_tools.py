"""Working State MCP tool registration."""

import logging

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import ValidationError

from memory import activity, metrics, projects, ratelimit
from memory import working_state as working_state_domain
from memory.api.working_state import StartSessionRequest
from memory.contracts import (
    CheckpointSeq,
    SessionEpoch,
    SessionId,
    WorkingStateLine,
    WorkingStateLines,
    WorkspaceId,
)
from memory.errors import DomainError
from memory.mcp.server import tool_session
from memory.mcp.tools import (
    REGISTRY,
    MCPToolError,
    ToolResult,
    _internal_error,
    _invalid_request,
)
from memory.working_state import WorkingStateWrite

logger = logging.getLogger("memory.mcp")


def _run_working_state(ctx: Context, body_factory, call) -> ToolResult:
    """Working State's own pipeline, never the bank-backed memory pipeline.

    These two tools have no bank, and routing them through bank resolution
    would create an accidental side effect. Authentication, rate limiting,
    error mapping and activity tracking still match the memory pipeline.
    """
    activity.new_call()
    try:
        with tool_session(ctx) as tc:
            ratelimit.check(tc.principal)
            # Built after authentication so an uncredentialed caller never
            # reaches pydantic's bounds as a free request-shape oracle.
            body = body_factory()
            result = call(tc.db, tc.principal, body)
            tc.db.commit()
            return result
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


def _record_working_state_call(
    *, action: str, principal, project, current_slug: str
) -> None:
    """Describe calls that have no Hindsight bank-resolution pipeline."""
    activity.describe(
        action=action,
        scope="project",
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        project_slug=current_slug,
        bank_fingerprint=activity.fingerprint(project.bank_id),
    )


def _start_working_session(db, principal, body: StartSessionRequest) -> ToolResult:
    resolution = projects.resolve(
        db, principal, body.project_slug, git_locator=body.git_locator, create=False
    )
    project = resolution.project
    row = working_state_domain.start_session(
        db,
        principal,
        resolution.current_slug,
        body.workspace_id,
        body.session_id,
        body.git_locator,
    )
    _record_working_state_call(
        action="working_state.start_session",
        principal=principal,
        project=project,
        current_slug=resolution.current_slug,
    )
    return ToolResult(
        result={
            "session_epoch": row.session_epoch,
            "session_id": row.session_id,
            "workspace_id": row.workspace_id,
            "project_slug": resolution.current_slug,
        },
        project_slug=resolution.current_slug,
        resolved_from=resolution.resolved_from,
        notice="PROJECT_RENAMED" if resolution.resolved_from else None,
    )


def _set_working_state(db, principal, body: WorkingStateWrite) -> ToolResult:
    resolution = projects.resolve(
        db, principal, body.project_slug, git_locator=body.git_locator, create=False
    )
    project = resolution.project
    state, changed = working_state_domain.replace(db, principal, body)
    _record_working_state_call(
        action="working_state.replace",
        principal=principal,
        project=project,
        current_slug=resolution.current_slug,
    )
    return ToolResult(
        result={
            "project_slug": resolution.current_slug,
            "workspace_id": state.workspace_id,
            "session_id": state.session_id,
            "session_epoch": state.session_epoch,
            "checkpoint_seq": state.checkpoint_seq,
            "objective": state.objective,
            "current_direction": state.current_direction,
            "recent_decisions": state.recent_decisions,
            "open_questions": state.open_questions,
            "next_steps": state.next_steps,
            "updated_at": state.updated_at.isoformat(),
            "changed": changed,
        },
        project_slug=resolution.current_slug,
        resolved_from=resolution.resolved_from,
        notice="PROJECT_RENAMED" if resolution.resolved_from else None,
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Allocate this session's ordering metadata for an explicit "
            "Working State handoff. Idempotent: a repeated call with the "
            "same session_id returns the same session_epoch, never a fresh "
            "one."
        ),
        # destructiveHint=False stated explicitly: the MCP spec DEFAULTS it
        # to true, and allocating a session creates nothing destructive.
        annotations=ToolAnnotations(idempotentHint=True, destructiveHint=False),
    )
    def start_working_session(
        project_slug: str,
        workspace_id: WorkspaceId,
        session_id: SessionId,
        ctx: Context,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run_working_state(
            ctx,
            lambda: StartSessionRequest(
                project_slug=project_slug,
                workspace_id=workspace_id,
                session_id=session_id,
                git_locator=git_locator,
            ),
            _start_working_session,
        )

    @mcp.tool(
        description=(
            "Replace the single Working State checkpoint for this project "
            "and workspace with an explicit human handoff. Ephemeral "
            "PostgreSQL state, never durable memory or history, and never a "
            "substitute for retain -- creates no memory, observation or "
            "document. Call this only when a human asks you to checkpoint "
            "or hand off the session; agents must not call it proactively "
            "from inferred task progress. A stale or conflicting checkpoint "
            "pair is refused (409), never merged."
        ),
        # idempotentHint, not destructiveHint: an exact retry of the pair
        # already stored is a no-op, but a greater pair does overwrite the
        # payload, so destructiveHint stays at its true default.
        annotations=ToolAnnotations(idempotentHint=True),
    )
    def set_working_state(
        project_slug: str,
        workspace_id: WorkspaceId,
        session_id: SessionId,
        session_epoch: SessionEpoch,
        checkpoint_seq: CheckpointSeq,
        objective: WorkingStateLine,
        ctx: Context,
        current_direction: WorkingStateLine | None = None,
        recent_decisions: WorkingStateLines | None = None,
        open_questions: WorkingStateLines | None = None,
        next_steps: WorkingStateLines | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run_working_state(
            ctx,
            lambda: WorkingStateWrite(
                project_slug=project_slug,
                workspace_id=workspace_id,
                session_id=session_id,
                session_epoch=session_epoch,
                checkpoint_seq=checkpoint_seq,
                objective=objective,
                current_direction=current_direction,
                recent_decisions=recent_decisions or [],
                open_questions=open_questions or [],
                next_steps=next_steps or [],
                git_locator=git_locator,
            ),
            _set_working_state,
        )

    REGISTRY.update(
        start_working_session=start_working_session,
        set_working_state=set_working_state,
    )
