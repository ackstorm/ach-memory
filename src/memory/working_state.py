"""Transient per-workspace handoff state, outside the memory bank (SPEC §3.1, §7)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.errors import InvalidRequest
from memory.identifiers import has_control_character
from memory.models import WorkingState

_STATE_FIELDS = ("objective", "direction", "decisions", "open_questions", "next_steps")
_MAX_FIELD_CHARS = 4000


class WorkingStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str

    @field_validator("workspace_id")
    @classmethod
    def _check_workspace_id(cls, value: str) -> str:
        if not 1 <= len(value) <= 128 or has_control_character(value):
            raise InvalidRequest("workspace_id must be 1-128 characters with no control characters")
        return value


class PutWorkingStateRequest(WorkingStateRequest):
    state: dict[str, str]

    @field_validator("state")
    @classmethod
    def _check_state(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - set(_STATE_FIELDS)
        if unknown:
            raise InvalidRequest(f"unknown state fields: {sorted(unknown)}")
        for key, text in value.items():
            if not isinstance(text, str) or len(text) > _MAX_FIELD_CHARS:
                raise InvalidRequest(f"{key} must be a string of at most {_MAX_FIELD_CHARS} characters")
        return value


class WorkingStateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str
    state: dict | None
    updated_at: datetime | None


def _owner(principal: Principal) -> str:
    return principal.on_behalf_of or principal.user_id


def get(db: Session, principal: Principal, request: WorkingStateRequest) -> WorkingStateResponse:
    row = db.get(WorkingState, (_owner(principal), request.workspace_id))
    if row is None:
        return WorkingStateResponse(workspace_id=request.workspace_id, state=None, updated_at=None)
    return WorkingStateResponse(workspace_id=request.workspace_id, state=row.state, updated_at=row.updated_at)


def put(db: Session, principal: Principal, request: PutWorkingStateRequest) -> WorkingStateResponse:
    """Caller commits."""
    owner = _owner(principal)
    row = db.get(WorkingState, (owner, request.workspace_id))
    if row is None:
        row = WorkingState(user_id=owner, workspace_id=request.workspace_id, state=request.state)
        db.add(row)
    else:
        row.state = request.state
    db.flush()
    return WorkingStateResponse(workspace_id=request.workspace_id, state=row.state, updated_at=row.updated_at)


def delete(db: Session, principal: Principal, request: WorkingStateRequest) -> None:
    """Caller commits. No-op if the workspace has no checkpoint."""
    row = db.get(WorkingState, (_owner(principal), request.workspace_id))
    if row is not None:
        db.delete(row)
        db.flush()
