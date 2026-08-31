"""Shapes shared by the capture pipeline's stages. Task 1 only needs the
checkpoint submission body and its acceptance response; later tasks extend
this module with the extractor envelope and normalized-candidate shapes
(SPEC Phase 3 §7-§10)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HEX64 = r"^[0-9a-f]{64}$"

CaptureStatus = Literal[
    "pending", "extracting", "retaining", "applying", "completed", "failed"
]


class CheckpointSubmission(BaseModel):
    """The exact wire body for `POST /v1/capture/checkpoints`. Built once
    the local client has already sanitized the slice -- nothing on this
    model is raw transcript content.
    """

    model_config = ConfigDict(extra="forbid")

    host: str
    session_id: str
    project_slug: str | None = None
    git_locator: str | None = None
    workspace_id: str | None = None
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    content_hash: str = Field(pattern=_HEX64)
    sanitized_hash: str = Field(pattern=_HEX64)
    content: str

    @model_validator(mode="after")
    def _end_after_start(self) -> "CheckpointSubmission":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class CheckpointAccepted(BaseModel):
    """The `202` acceptance response. `extra="ignore"`: the server may add
    fields the local client has no use for, and a forward-compatible client
    should not break on them."""

    model_config = ConfigDict(extra="ignore")

    capture_id: str
    status: CaptureStatus
    duplicate: bool
    session_epoch: int | None = None
    checkpoint_seq: int
    project_slug: str | None = None
    resolved_from: str | None = None
