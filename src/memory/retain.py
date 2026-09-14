"""Submit one durable, idempotent claim through the backend adapter (SPEC §3.1).

ACH owns governance here -- idempotency, sanitization, the journal; the
adapter owns durability and search.
"""

import hashlib
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from memory import audit
from memory.auth.principal import Principal
from memory.backend.base import Backend
from memory.bank_ref import resolve_bank
from memory.bootstrap import provision_before_retain
from memory.config import get_settings
from memory.errors import InvalidRequest
from memory.sanitization import normalize_claim
from memory.tags import SCHEMA_TAG, Basis, MemoryType, basis_tag, normalize_caller_tags, type_tag


class RetainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["user", "project"]
    project_slug: str | None = None
    content: str = Field(min_length=1)
    memory_type: MemoryType
    basis: Basis
    operation_id: UUID
    tags: tuple[str, ...] = ()
    wait: bool = False

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: list[str] | None) -> tuple[str, ...]:
        return normalize_caller_tags(value)


class RetainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    operation_id: UUID
    status: Literal["completed", "pending", "unknown", "replayed"]
    notice: str | None = None
    operation_ref: str | None = None


def submit(
    db: Session, principal: Principal, request: RetainRequest, *, backend: Backend
) -> RetainResponse:
    """Caller commits."""
    # find_retain-then-act is check-then-act on a non-unique column: serialize
    # on the operation id so two concurrent first submits can't each mint a
    # memory id.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"retain:{request.operation_id}"}
    )
    prior = audit.find_retain(db, str(request.operation_id))
    if prior is not None:
        return RetainResponse(
            memory_id=prior.resource, operation_id=request.operation_id, status="replayed"
        )

    if len(request.content.encode()) > get_settings().max_content_bytes:
        raise InvalidRequest("content exceeds the maximum size")
    content = normalize_claim(request.content)

    ref, notice = resolve_bank(db, principal, request.scope, request.project_slug, create=True)
    provision_before_retain(db, ref, backend=backend)

    memory_id = f"mem_{uuid4().hex}"
    tags = (type_tag(request.memory_type), basis_tag(request.basis), SCHEMA_TAG, *request.tags)
    ack = backend.retain(
        ref.bank_id, memory_id, content,
        tags=tags, metadata={"memory_id": memory_id}, wait=request.wait,
    )

    audit.record(
        db, principal=principal, action="memory.retain", resource=memory_id,
        operation_id=str(request.operation_id),
        details={
            "scope": request.scope,
            "owner": ref.owner_id,
            "status": ack.status,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "memory_type": request.memory_type,
            "basis": request.basis,
            "tags": list(tags),
            "operation_ref": ack.operation_ref,
        },
    )
    return RetainResponse(
        memory_id=memory_id, operation_id=request.operation_id,
        status=ack.status, notice=notice, operation_ref=ack.operation_ref,
    )
