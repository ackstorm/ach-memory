"""Authenticated checkpoint acceptance (SPEC Phase 3 §4.2-§6).

The route never calls an LLM or Hindsight: acceptance is one bounded
PostgreSQL transaction (memory.capture.repository.accept_checkpoint) and a
`202`. Semantic extraction happens later, out of band, in the Task 6 worker.
"""

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from memory import metrics
from memory.api.app import current_principal
from memory.api.common import RenameForwarding
from memory.api.memory import _check_content_size
from memory.auth.principal import Principal
from memory.capture import repository
from memory.capture.contracts import CaptureStatus, CheckpointSubmission
from memory.db import get_session
from memory.errors import CaptureIntegrityError, Forbidden

router = APIRouter(prefix="/v1/capture", tags=["capture"])

# The request body is the local client's own submission model, not a
# parallel copy of it (SPEC Phase 3 review finding 1): a second definition
# is exactly how the shipped hook came to omit a field this route requires.
CheckpointRequest = CheckpointSubmission


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

    # Acceptance telemetry is counts and modes only (SPEC Phase 3 review
    # finding 9). The activity_events row this used to write carried the
    # project slug and a bank fingerprint -- identity, on a path that fires
    # once per Stop hook -- so it is a Prometheus counter now: host bucket,
    # resulting status, whether it was a replay, and a byte bucket. No slug,
    # no bank, no offsets, no hashes, no content.
    metrics.CAPTURE_CHECKPOINT.labels(
        host=metrics.host_label(body.host),
        status=row.status,
        duplicate=str(result.duplicate).lower(),
        bytes_bucket=metrics.bytes_bucket(len(body.content.encode("utf-8"))),
    ).inc()
    db.commit()

    return CheckpointResponse(
        capture_id=str(row.id),
        status=row.status,
        duplicate=result.duplicate,
        session_epoch=row.session_epoch,
        checkpoint_seq=row.end_offset,
        project_slug=result.resolution.current_slug,
        resolved_from=result.resolution.resolved_from,
    )
