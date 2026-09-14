"""Curate one already-retained memory through the backend adapter (SPEC §3.1, I4/I7).

Same discipline as `retain.py` -- no ledger, no reconciliation. Each function
resolves the bank, locks it (I7), asks the adapter to act, and journals the
mutation. Caller commits.
"""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from memory import audit
from memory.auth.principal import Principal
from memory.backend.base import Backend
from memory.bank_ref import lock_bank, resolve_bank
from memory.config import get_settings
from memory.errors import InvalidRequest, MemoryNotFound
from memory.sanitization import normalize_claim


class CurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["user", "project"]
    project_slug: str | None = None
    memory_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)


class CorrectRequest(CurationRequest):
    content: str = Field(min_length=1)


class CurationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    status: Literal["invalidated", "valid", "deleted"]
    notice: str | None = None


def _journal(db: Session, principal: Principal, action: str, request: CurationRequest,
             owner: str, extra: dict) -> None:
    audit.record(db, principal=principal, action=action, resource=request.memory_id,
                 operation_id=str(uuid4()),
                 details={"scope": request.scope, "owner": owner, "reason": request.reason, **extra})


def forget(db: Session, principal: Principal, request: CurationRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    lock_bank(db, ref.bank_id)
    backend.invalidate(ref.bank_id, request.memory_id, reason=request.reason)
    _journal(db, principal, "memory.forget", request, ref.owner_id, {})
    return CurationResponse(memory_id=request.memory_id, status="invalidated", notice=notice)


def restore(db: Session, principal: Principal, request: CurationRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    lock_bank(db, ref.bank_id)
    backend.revalidate(ref.bank_id, request.memory_id)
    _journal(db, principal, "memory.restore", request, ref.owner_id, {})
    return CurationResponse(memory_id=request.memory_id, status="valid", notice=notice)


def correct(db: Session, principal: Principal, request: CorrectRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    lock_bank(db, ref.bank_id)
    before = backend.get(ref.bank_id, request.memory_id)
    if before is None:
        raise MemoryNotFound(memory_id=request.memory_id)
    if len(request.content.encode()) > get_settings().max_content_bytes:
        raise InvalidRequest("content exceeds the maximum size")
    content = normalize_claim(request.content)
    backend.retain(ref.bank_id, request.memory_id, content, tags=before.tags, metadata=before.metadata, wait=True)
    _journal(db, principal, "memory.correct", request, ref.owner_id, {"before": before.text, "after": content})
    return CurationResponse(memory_id=request.memory_id, status="valid", notice=notice)


def delete(db: Session, principal: Principal, request: CurationRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    lock_bank(db, ref.bank_id)
    backend.delete(ref.bank_id, request.memory_id)
    _journal(db, principal, "memory.delete", request, ref.owner_id, {})
    return CurationResponse(memory_id=request.memory_id, status="deleted", notice=notice)
