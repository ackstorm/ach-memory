"""Curate one already-retained memory through the backend adapter (SPEC §3.1, I4/I7).

Same discipline as `retain.py` -- no ledger, no reconciliation. Each function
resolves the bank, locks it (I7), asks the adapter to act, and journals the
mutation. Caller commits.
"""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from memory import audit, catalog
from memory.auth.principal import Principal
from memory.backend.base import Backend
from memory.bank_ref import LogicalBankRef, lock_bank, resolve_memory_bank
from memory.config import get_settings
from memory.errors import InvalidRequest, MemoryNotFound
from memory.models import Memory
from memory.sanitization import normalize_claim


class CurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["user", "project"] | None = None
    project_slug: str | None = None
    memory_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)


class CorrectRequest(CurationRequest):
    content: str = Field(min_length=1)


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["user", "project"] | None = None
    project_slug: str | None = None
    memory_id: str = Field(min_length=1, max_length=128)
    pinned: bool = True
    reason: str | None = Field(default=None, max_length=512)


class PinResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    pinned: bool
    notice: str | None = None


class CurationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str
    status: Literal["invalidated", "valid", "deleted"]
    notice: str | None = None


def _set_state(db: Session, ref: LogicalBankRef, memory_id: str, state: str) -> None:
    if (row := catalog.get(db, ref, memory_id)) is not None:
        row.state = state


def _journal(db: Session, principal: Principal, action: str, request: CurationRequest | PinRequest,
             ref: LogicalBankRef, extra: dict) -> None:
    audit.record(db, principal=principal, action=action, resource=request.memory_id,
                 operation_id=str(uuid4()),
                 details={"scope": ref.scope, "owner": ref.owner_id, "reason": request.reason, **extra})


def forget(db: Session, principal: Principal, request: CurationRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_memory_bank(db, principal, request.scope, request.project_slug, request.memory_id)
    lock_bank(db, ref.bank_id)
    backend.invalidate(ref.bank_id, request.memory_id, reason=request.reason)
    _set_state(db, ref, request.memory_id, "invalid")
    _journal(db, principal, "memory.forget", request, ref, {})
    return CurationResponse(memory_id=request.memory_id, status="invalidated", notice=notice)


def restore(db: Session, principal: Principal, request: CurationRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_memory_bank(db, principal, request.scope, request.project_slug, request.memory_id)
    lock_bank(db, ref.bank_id)
    backend.revalidate(ref.bank_id, request.memory_id)
    _set_state(db, ref, request.memory_id, "valid")
    _journal(db, principal, "memory.restore", request, ref, {})
    return CurationResponse(memory_id=request.memory_id, status="valid", notice=notice)


def correct(db: Session, principal: Principal, request: CorrectRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_memory_bank(db, principal, request.scope, request.project_slug, request.memory_id)
    lock_bank(db, ref.bank_id)
    before = backend.get(ref.bank_id, request.memory_id)
    if before is None:
        raise MemoryNotFound()
    if len(request.content.encode()) > get_settings().max_content_bytes:
        raise InvalidRequest("content exceeds the maximum size")
    content = normalize_claim(request.content)
    row = catalog.get(db, ref, request.memory_id)
    if row is not None and row.pinned:
        catalog.check_pin_cap(db, ref, len(content), memory_id=request.memory_id)
    backend.retain(ref.bank_id, request.memory_id, content, tags=before.tags, metadata=before.metadata, wait=True)
    if row is not None:
        row.chars = len(content)
    _journal(db, principal, "memory.correct", request, ref, {"before": before.text, "after": content})
    return CurationResponse(memory_id=request.memory_id, status="valid", notice=notice)


def delete(db: Session, principal: Principal, request: CurationRequest, *, backend: Backend) -> CurationResponse:
    ref, notice = resolve_memory_bank(db, principal, request.scope, request.project_slug, request.memory_id)
    lock_bank(db, ref.bank_id)
    backend.delete(ref.bank_id, request.memory_id)
    if (row := catalog.get(db, ref, request.memory_id)) is not None:
        db.delete(row)
        db.flush()
    _journal(db, principal, "memory.delete", request, ref, {})
    return CurationResponse(memory_id=request.memory_id, status="deleted", notice=notice)


def pin(db: Session, principal: Principal, request: PinRequest, *, backend: Backend) -> PinResponse:
    """Pin or unpin one valid memory; no engine call. A pin past the bank's cap is refused."""
    ref, notice = resolve_memory_bank(db, principal, request.scope, request.project_slug, request.memory_id)
    lock_bank(db, ref.bank_id)
    row: Memory | None = catalog.get(db, ref, request.memory_id)
    if row is None:
        raise MemoryNotFound()
    if request.pinned and row.state != "valid":
        raise InvalidRequest("restore this memory before pinning it")
    if request.pinned and not row.pinned:
        catalog.check_pin_cap(db, ref, row.chars)
    if request.pinned != row.pinned:
        row.pinned = request.pinned
        _journal(db, principal, "memory.pin" if request.pinned else "memory.unpin", request, ref, {})
    return PinResponse(memory_id=request.memory_id, pinned=row.pinned, notice=notice)
