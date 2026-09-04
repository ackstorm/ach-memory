from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memory.contracts import WorkspaceId
from memory.memory_types import EvidenceBasis, EvidenceKind, MemoryType, RetainTrigger


class RetainEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: EvidenceKind
    raw: str = Field(min_length=1, max_length=1024)
    source_ref: str | None = Field(default=None, max_length=512)


class TypedRetainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["user", "project"]
    user_id: str | None = None
    project_slug: str | None = Field(default=None, max_length=128)
    content: str = Field(min_length=1)
    memory_type: MemoryType
    basis: EvidenceBasis
    trigger: RetainTrigger
    valid_until: datetime | None = None
    evidence: tuple[RetainEvidence, ...] = Field(min_length=1, max_length=4)
    operation_id: UUID

    @model_validator(mode="after")
    def validate_scope_and_time(self):
        if self.scope == "project" and not self.project_slug:
            raise ValueError("project_slug is required for project scope")
        if self.scope == "user" and self.project_slug is not None:
            raise ValueError("project_slug is forbidden for user scope")
        if self.valid_until is not None and self.valid_until.utcoffset() is None:
            raise ValueError("valid_until must include an RFC 3339 offset")
        return self


class TypedRetainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str
    operation_id: UUID
    document_id: str
    status: Literal["pending", "accepted", "completed", "failed"]
    recorded_at: datetime
    valid_until: datetime | None


class LoadContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str | None = Field(default=None, max_length=128)
    workspace_id: WorkspaceId | None = None

    @model_validator(mode="after")
    def workspace_requires_project(self):
        if self.workspace_id is not None and self.project_slug is None:
            raise ValueError("workspace_id requires project_slug")
        return self
