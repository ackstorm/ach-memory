"""Authenticated checkpoint acceptance (SPEC Phase 3 §4.2-§6).

The route never calls an LLM or Hindsight: acceptance is one bounded
PostgreSQL transaction (memory.capture.repository.accept_checkpoint) and a
`202`. Semantic extraction happens later, out of band, in the Task 6 worker.
"""

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from memory import activity
from memory.api.app import current_principal
from memory.api.common import RenameForwarding
from memory.api.memory import _check_content_size
from memory.auth.principal import Principal
from memory.capture import repository
from memory.capture.contracts import CaptureStatus
from memory.contracts import SessionId, WorkspaceId
from memory.db import get_session
from memory.errors import CaptureIntegrityError, Forbidden

router = APIRouter(prefix="/v1/capture", tags=["capture"])

_HEX64 = r"^[0-9a-f]{64}$"


class CheckpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=32)
    session_id: SessionId
    project_slug: str
    git_locator: str | None = None
    workspace_id: WorkspaceId
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    content_hash: str = Field(pattern=_HEX64)
    sanitized_hash: str = Field(pattern=_HEX64)
    content: str

    @model_validator(mode="after")
    def _end_after_start(self) -> "CheckpointRequest":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class CheckpointResponse(RenameForwarding):
    capture_id: str
    status: CaptureStatus
    duplicate: bool
    session_epoch: int
    checkpoint_seq: int
    project_slug: str


def _reject_master(principal: Principal) -> None:
    if principal.is_master:
        raise Forbidden(
            "capture checkpoints have no On-Behalf-Of path; a master key has "
            "no Claude Code session of its own to checkpoint"
        )


@router.post("/checkpoints", status_code=202, response_model=CheckpointResponse)
def submit_checkpoint(
    body: CheckpointRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> CheckpointResponse:
    _reject_master(principal)
    _check_content_size(body.content)
    if hashlib.sha256(body.content.encode("utf-8")).hexdigest() != body.sanitized_hash:
        raise CaptureIntegrityError("sanitized_hash does not match content")

    result = repository.accept_checkpoint(
        db,
        principal,
        host=body.host,
        session_id=body.session_id,
        project_slug=body.project_slug,
        git_locator=body.git_locator,
        workspace_id=body.workspace_id,
        start_offset=body.start_offset,
        end_offset=body.end_offset,
        content_hash=body.content_hash,
        sanitized_hash=body.sanitized_hash,
        content=body.content,
    )
    row = result.row

    # Only fields activity_events already has room for: no content, no
    # offsets, no hashes, no host/status/duplicate column yet (those land in
    # Task 8's dedicated capture counters). action alone already says "this
    # was a capture checkpoint".
    activity.describe(
        action="capture.checkpoint",
        scope="project",
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        project_slug=result.resolution.project.project_slug,
        bank_fingerprint=activity.fingerprint(result.resolution.project.bank_id),
        content_bytes=len(body.content.encode("utf-8")),
    )
    db.commit()

    return CheckpointResponse(
        capture_id=str(row.id),
        status=row.status,
        duplicate=result.duplicate,
        session_epoch=row.session_epoch,
        checkpoint_seq=row.end_offset,
        project_slug=result.resolution.project.project_slug,
        resolved_from=result.resolution.resolved_from,
    )
