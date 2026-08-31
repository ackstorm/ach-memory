"""REST surface for the explicit Working State handoff (SPEC Working State).

Both routes reject master credentials outright: Working State is a per-user
handoff between that user's own sessions, and a master key acting through
On-Behalf-Of has no session of its own to hand off from or to.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from memory import working_state as domain
from memory.api.app import current_principal
from memory.auth.principal import Principal
from memory.db import get_session
from memory.errors import Forbidden
from memory.working_state import WORKSPACE_ID_PATTERN, WorkingStateWrite

router = APIRouter(prefix="/v1/working-state", tags=["working-state"])


class StartSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_slug: str
    workspace_id: str
    session_id: str
    git_locator: str | None = None

    @field_validator("workspace_id")
    @classmethod
    def _workspace_id_is_well_formed(cls, value: str) -> str:
        if not WORKSPACE_ID_PATTERN.match(value):
            raise ValueError("workspace_id must be 'ws_' followed by 32 hex characters")
        return value


class SessionResponse(BaseModel):
    session_epoch: int
    session_id: str
    workspace_id: str
    project_slug: str


class WorkingStateResponse(BaseModel):
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
    row = domain.start_session(
        db, principal, body.project_slug, body.workspace_id, body.session_id, body.git_locator
    )
    db.commit()
    return SessionResponse(
        session_epoch=row.session_epoch,
        session_id=row.session_id,
        workspace_id=row.workspace_id,
        project_slug=body.project_slug,
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
    state, changed = domain.replace(db, principal, body)
    db.commit()
    return WorkingStateResponse(
        project_slug=body.project_slug,
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
