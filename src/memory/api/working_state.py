"""REST surface for the explicit Working State handoff (SPEC Working State).

Both routes reject master credentials outright: Working State is a per-user
handoff between that user's own sessions, and a master key acting through
On-Behalf-Of has no session of its own to hand off from or to.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from memory import activity, projects
from memory import working_state as domain
from memory.api.app import current_principal
from memory.api.common import RenameForwarding
from memory.auth.principal import Principal
from memory.contracts import SessionId, WorkspaceId
from memory.db import get_session
from memory.errors import Forbidden
from memory.models import Project
from memory.working_state import WorkingStateWrite

router = APIRouter(prefix="/v1/working-state", tags=["working-state"])


class StartSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_slug: str
    workspace_id: WorkspaceId
    session_id: SessionId
    git_locator: str | None = None


class SessionResponse(RenameForwarding):
    session_epoch: int
    session_id: str
    workspace_id: str
    project_slug: str


class WorkingStateResponse(RenameForwarding):
    project_slug: str
    workspace_id: str
    session_id: str
    session_epoch: int
    checkpoint_seq: int
    objective: str
    current_direction: str | None
    recent_decisions: list[str]
    open_questions: list[str]
    next_steps: list[str]
    updated_at: datetime
    # Whether this write actually changed anything, vs. an idempotent retry
    # of the pair already stored.
    changed: bool


def _reject_master(principal: Principal) -> None:
    if principal.is_master:
        raise Forbidden(
            "Working State has no On-Behalf-Of path; a master key has no "
            "session of its own to hand off"
        )


def _record(
    *, action: str, principal: Principal, project: Project, current_slug: str
) -> None:
    """Neither route has a Hindsight bank to fingerprint through `_resolve_bank`
    (memory/api/memory.py), so without this both silently produced no
    activity row and no metrics: `activity.finish()` requires "action"/"scope"
    to have been set by a `describe()` call before it writes anything."""
    activity.describe(
        action=action,
        scope="project",
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        project_slug=current_slug,
        bank_fingerprint=activity.fingerprint(project.bank_id),
    )


@router.post("/sessions", response_model=SessionResponse)
def start_session(
    body: StartSessionRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> SessionResponse:
    """Allocate this session's epoch, or return the one already allocated.

    200, never 201, including the first call: the same request is
    deliberately idempotent, so there is no "first time" a client can
    observe from the response alone.
    """
    _reject_master(principal)
    resolution = projects.resolve(
        db, principal, body.project_slug, git_locator=body.git_locator, create=False
    )
    project = resolution.project
    row = domain.start_session(
        db,
        principal,
        resolution.current_slug,
        body.workspace_id,
        body.session_id,
        body.git_locator,
    )
    _record(
        action="working_state.start_session",
        principal=principal,
        project=project,
        current_slug=resolution.current_slug,
    )
    db.commit()
    return SessionResponse(
        session_epoch=row.session_epoch,
        session_id=row.session_id,
        workspace_id=row.workspace_id,
        project_slug=resolution.current_slug,
        resolved_from=resolution.resolved_from,
    )


@router.put("", response_model=WorkingStateResponse)
def set_working_state(
    body: WorkingStateWrite,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> WorkingStateResponse:
    """Write a checkpoint. A stale or conflicting pair reaches the client as
    a 409 through the existing DomainError handler -- no special-casing
    here."""
    _reject_master(principal)
    resolution = projects.resolve(
        db, principal, body.project_slug, git_locator=body.git_locator, create=False
    )
    project = resolution.project
    state, changed = domain.replace(db, principal, body)
    _record(
        action="working_state.replace",
        principal=principal,
        project=project,
        current_slug=resolution.current_slug,
    )
    db.commit()
    return WorkingStateResponse(
        project_slug=resolution.current_slug,
        resolved_from=resolution.resolved_from,
        workspace_id=state.workspace_id,
        session_id=state.session_id,
        session_epoch=state.session_epoch,
        checkpoint_seq=state.checkpoint_seq,
        objective=state.objective,
        current_direction=state.current_direction,
        recent_decisions=state.recent_decisions,
        open_questions=state.open_questions,
        next_steps=state.next_steps,
        updated_at=state.updated_at,
        changed=changed,
    )
