from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memory.contracts import WorkspaceId
from memory.memory_types import EvidenceBasis, EvidenceKind, Lifecycle, MemoryType, RetainTrigger
from memory.tags import normalize_caller_tags


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
    #: Additive, caller-supplied tags (e.g. `repo:group/app`), appended after
    #: the four server-derived tags in `retention._tags`. Normalised here so
    #: REST and MCP share one gate and a caller can never forge a reserved
    #: namespace (memory.tags.RESERVED_PREFIXES).
    tags: tuple[str, ...] = ()

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: list[str] | None) -> tuple[str, ...]:
        return normalize_caller_tags(value)

    @model_validator(mode="after")
    def validate_scope_and_time(self):
        if self.scope == "project" and not self.project_slug:
            raise ValueError("project_slug is required for project scope")
        if self.scope == "user" and self.project_slug is not None:
            raise ValueError("project_slug is forbidden for user scope")
        if self.valid_until is not None and self.valid_until.utcoffset() is None:
            raise ValueError("valid_until must include an RFC 3339 offset")
        if self.valid_until is not None and self.valid_until <= datetime.now(UTC):
            raise ValueError("valid_until must be later than the current time")
        return self


class TypedRetainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str
    operation_id: UUID
    document_id: str
    status: Literal["pending", "accepted", "completed", "failed"]
    recorded_at: datetime
    valid_until: datetime | None
    lifecycle: Lifecycle
    #: The Hindsight memory id curation tools key on (`forget`, `correct`,
    #: `get_memory`, `memory_history`). Known once the operation completed --
    #: `sync_retain` always, `retain` only on an idempotent replay that
    #: finished in between. None while pending (QA F-03).
    memory_id: str | None = None
    #: "PROJECT_CREATED" when this retain minted the project (lazy
    #: provisioning, decision 1) so a misspelt slug is visible at once (QA F-10);
    #: "DUPLICATE_CLAIM" when an active record with the same canonical text
    #: already existed and was returned instead of a second one (QA F-05).
    notice: str | None = None


class LoadContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str | None = Field(default=None, min_length=1, max_length=128)
    workspace_id: WorkspaceId | None = None
    scope: Literal["user", "project", "both"] = "both"

    @model_validator(mode="after")
    def workspace_requires_project(self):
        if self.workspace_id is not None and self.project_slug is None:
            raise ValueError("workspace_id requires project_slug")
        return self
