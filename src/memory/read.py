"""Resolve through the adapter, apply the semantic floor, shape the response (SPEC §3.1).

The only read-side shaping is the recall floor (invariant I5); everything
else here is a straight adapter call plus tag/state translation. A read
never creates a bank -- `resolve_bank(..., create=False)`.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.backend.base import Backend, Hit, MemoryView, TagGroup
from memory.bank_ref import resolve_bank
from memory.config import get_settings
from memory.errors import MemoryNotFound, ProjectNotFound, UnsupportedCapability
from memory.models import AuditEvent
from memory.tags import SCHEMA_TAG

_STATE_TO_BACKEND = {"valid": "valid", "invalid": "invalidated"}
_STATE_FROM_BACKEND = {"valid": "valid", "invalidated": "invalid"}

class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

class _ScopedRequest(_Base):
    scope: Literal["user", "project"]; project_slug: str | None = None

class RecallRequest(_ScopedRequest):
    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=50)
    memory_types: list[str] = Field(default_factory=list); basis: list[str] = Field(default_factory=list)

class _MemoryFields(_Base):
    memory_id: str; content: str
    memory_type: str | None = None; basis: str | None = None
    tags: tuple[str, ...] = (); created_at: str | None = None

class RecallHit(_MemoryFields):
    score: float | None = None

class RecallResponse(_Base):
    items: tuple[RecallHit, ...] = (); total: int = 0; truncated: bool = False

class ReflectRequest(_ScopedRequest):
    query: str = Field(min_length=1)
    memory_types: list[str] = Field(default_factory=list); basis: list[str] = Field(default_factory=list)

class ReflectResponse(_Base):
    answer: str

class ListRequest(_ScopedRequest):
    memory_types: list[str] = Field(default_factory=list); basis: list[str] = Field(default_factory=list)
    state: Literal["valid", "invalid"] | None = None
    limit: int = Field(default=20, ge=1, le=100); offset: int = Field(default=0, ge=0)

class MemoryItem(_MemoryFields):
    state: Literal["valid", "invalid"]

class ListResponse(_Base):
    items: tuple[MemoryItem, ...] = (); total: int = 0
    limit: int = 20; offset: int = 0

class GetRequest(_ScopedRequest):
    memory_id: str = Field(min_length=1, max_length=128)

HistoryRequest = GetRequest

class JournalEntry(_Base):
    action: str; actor: str | None = None
    on_behalf_of: str | None = None; operation_id: str | None = None
    created_at: str; details: dict = Field(default_factory=dict)

class HistoryResponse(_Base):
    current: MemoryItem | None = None
    journal: tuple[JournalEntry, ...] = ()

def _tag_groups(memory_types: list[str], basis: list[str]) -> tuple[TagGroup, ...]:
    groups: list[TagGroup] = []
    if memory_types: groups.append({"tags": [f"type:{t}" for t in memory_types], "match": "any"})
    if basis: groups.append({"tags": [f"basis:{b}" for b in basis], "match": "any"})
    return tuple(groups)

def _split_tags(tags: tuple[str, ...]) -> tuple[str | None, str | None, tuple[str, ...]]:
    """`type:*`/`basis:*` become fields, not tags; `schema:*` is dropped entirely."""
    memory_type = basis = None
    caller_tags: list[str] = []
    for tag in tags:
        if tag.startswith("type:"):
            memory_type = tag.removeprefix("type:")
        elif tag.startswith("basis:"):
            basis = tag.removeprefix("basis:")
        elif tag != SCHEMA_TAG:
            caller_tags.append(tag)
    return memory_type, basis, tuple(caller_tags)

def _to_hit(hit: Hit) -> RecallHit:
    memory_type, basis, tags = _split_tags(hit.tags)
    return RecallHit(memory_id=hit.memory_id, content=hit.text, memory_type=memory_type, basis=basis, tags=tags, score=hit.score, created_at=hit.mentioned_at)

def _to_item(view: MemoryView) -> MemoryItem:
    memory_type, basis, tags = _split_tags(view.tags)
    return MemoryItem(memory_id=view.memory_id, content=view.text, memory_type=memory_type, basis=basis, tags=tags, state=_STATE_FROM_BACKEND[view.state], created_at=view.created_at)

def recall(db: Session, principal: Principal, request: RecallRequest, *, backend: Backend) -> RecallResponse:
    try:
        ref, _ = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    except ProjectNotFound:  # a project nobody has retained into yet simply has nothing to recall
        return RecallResponse(items=(), total=0, truncated=False)
    tag_groups = _tag_groups(request.memory_types, request.basis)
    # Ask for one more than requested: a full page signals there may be more.
    raw = backend.recall(ref.bank_id, request.query, tag_groups=tag_groups, limit=request.max_results + 1)
    floor = get_settings().recall_min_semantic
    kept = [hit for hit in raw if hit.score is None or hit.score >= floor]
    items = tuple(_to_hit(hit) for hit in kept[: request.max_results])
    return RecallResponse(items=items, total=len(items), truncated=len(raw) > request.max_results)

def reflect(db: Session, principal: Principal, request: ReflectRequest, *, backend: Backend) -> ReflectResponse:
    if "synthesis" not in backend.capabilities():
        raise UnsupportedCapability("synthesis")
    ref, _ = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    answer = backend.reflect(ref.bank_id, request.query,
                             tag_groups=_tag_groups(request.memory_types, request.basis))
    return ReflectResponse(answer=answer)

def list_memories(db: Session, principal: Principal, request: ListRequest, *, backend: Backend) -> ListResponse:
    try:
        ref, _ = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    except ProjectNotFound:
        return ListResponse(items=(), total=0, limit=request.limit, offset=request.offset)
    tag_groups = _tag_groups(request.memory_types, request.basis)
    state = _STATE_TO_BACKEND[request.state] if request.state else None
    page = backend.list(ref.bank_id, tag_groups=tag_groups, state=state, limit=request.limit, offset=request.offset)
    items = tuple(_to_item(view) for view in page.items)
    return ListResponse(items=items, total=page.total, limit=request.limit, offset=request.offset)

def get_memory(db: Session, principal: Principal, request: GetRequest, *, backend: Backend) -> MemoryItem:
    ref, _ = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    view = backend.get(ref.bank_id, request.memory_id)
    if view is None:
        raise MemoryNotFound()
    return _to_item(view)

def history(db: Session, principal: Principal, request: HistoryRequest, *, backend: Backend) -> HistoryResponse:
    ref, _ = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    view = backend.get(ref.bank_id, request.memory_id)
    current = _to_item(view) if view is not None else None
    # I1: the journal is read through the caller's own bank, never by memory id alone.
    stmt = select(AuditEvent).where(AuditEvent.resource == request.memory_id,
                                    AuditEvent.details["owner"].as_string() == ref.owner_id).order_by(
        AuditEvent.created_at.desc(), AuditEvent.id.desc())
    rows = db.scalars(stmt).all()
    journal = tuple(JournalEntry(action=r.action, actor=r.actor_user_id, on_behalf_of=r.on_behalf_of,
                                  operation_id=r.operation_id, created_at=r.created_at.isoformat(),
                                  details=r.details or {}) for r in rows)
    return HistoryResponse(current=current, journal=journal)
