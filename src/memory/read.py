"""Resolve through the adapter, apply the semantic floor, shape the response (SPEC §3.1).

The only read-side shaping is the recall floor (invariant I5); everything
else here is a straight adapter call plus tag/state translation. A read
never creates a bank -- `resolve_bank(..., create=False)`.
"""

import logging
from itertools import zip_longest
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory import catalog, llm
from memory.auth.principal import Principal
from memory.backend.base import Backend, Hit, MemoryView, TagGroup
from memory.bank_ref import resolve_bank, resolve_memory_bank
from memory.config import get_settings
from memory.errors import MemoryNotFound, ProjectNotFound, UnsupportedCapability
from memory.models import AuditEvent, Memory
from memory.tags import SCHEMA_TAG

logger = logging.getLogger(__name__)
_STATE_TO_BACKEND = {"valid": "valid", "invalid": "invalidated"}
#: How many of recall's first hits the decision model scores.
RELEVANCE_TOP = 10
NOTHING_CLEAR = ("No memory clearly answers this query. The hits below are only the nearest: "
                 "check one before relying on it.")
_STATE_FROM_BACKEND = {"valid": "valid", "invalidated": "invalid"}

class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

class _ScopedRequest(_Base):
    scope: Literal["user", "project"]; project_slug: str | None = None

class RecallRequest(_Base):
    # Not a `_ScopedRequest`: recall is the only intention that spans both banks.
    scope: Literal["user", "project", "all"]; project_slug: str | None = None
    query: str = Field(min_length=1)
    max_results: int = Field(default=10, ge=1, le=50)
    memory_types: list[str] = Field(default_factory=list); basis: list[str] = Field(default_factory=list)

class _MemoryFields(_Base):
    memory_id: str; title: str | None = None; content: str | None = None
    family: str | None = None; pinned: bool = False
    memory_type: str | None = None; basis: str | None = None
    tags: tuple[str, ...] = (); created_at: str | None = None

class RecallHit(_MemoryFields):
    score: float | None = None
    #: The decision model's probability that this hit helps answer the query; first hits only.
    relevance: float | None = None

class RecallResponse(_Base):
    #: First, so it is read first: set when even the best hit is unlikely to answer.
    note: str | None = None
    items: tuple[RecallHit, ...] = (); total: int = 0; truncated: bool = False

class ReflectRequest(_ScopedRequest):
    query: str = Field(min_length=1)
    memory_types: list[str] = Field(default_factory=list); basis: list[str] = Field(default_factory=list)

class ReflectResponse(_Base):
    answer: str

class ListRequest(_ScopedRequest):
    memory_types: list[str] = Field(default_factory=list); family: str | None = None
    pinned: bool | None = None; state: Literal["valid", "invalid"] | None = None
    full: bool = False
    limit: int = Field(default=20, ge=1, le=100); offset: int = Field(default=0, ge=0)

class MemoryItem(_MemoryFields):
    state: Literal["valid", "invalid"]

class ListResponse(_Base):
    items: tuple[MemoryItem, ...] = (); total: int = 0
    limit: int = 20; offset: int = 0

class GetRequest(_Base):
    scope: Literal["user", "project"] | None = None; project_slug: str | None = None
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

def _row_fields(row: Memory | None) -> dict:
    return {"title": row.title, "family": row.family, "pinned": row.pinned} if row else {}

def _to_hit(hit: Hit, row: Memory | None) -> RecallHit:
    memory_type, basis, tags = _split_tags(hit.tags)
    return RecallHit(memory_id=hit.memory_id, content=hit.text, memory_type=memory_type, basis=basis, tags=tags, score=hit.score, created_at=hit.mentioned_at, **_row_fields(row))

def _to_item(view: MemoryView, row: Memory | None) -> MemoryItem:
    memory_type, basis, tags = _split_tags(view.tags)
    return MemoryItem(memory_id=view.memory_id, content=view.text, memory_type=memory_type, basis=basis, tags=tags, state=_STATE_FROM_BACKEND[view.state], created_at=view.created_at, **_row_fields(row))

def _recall_banks(db: Session, principal: Principal, request: RecallRequest) -> list[str]:
    """The banks one recall covers. `scope="all"` is project then user, so a
    project-specific claim wins a tie against a general user one; a project that is
    unnamed or that nobody retained into drops out instead of failing the whole call."""
    scopes = ("project", "user") if request.scope == "all" else (request.scope,)
    banks = []
    for scope in scopes:
        if scope == "project" and request.scope == "all" and not request.project_slug:
            continue
        try:
            ref, _ = resolve_bank(db, principal, scope, request.project_slug, create=False)
        except ProjectNotFound:  # nobody has retained into it yet; nothing to recall
            continue
        banks.append(ref.bank_id)
    return banks

def recall(db: Session, principal: Principal, request: RecallRequest, *, backend: Backend) -> RecallResponse:
    tag_groups = _tag_groups(request.memory_types, request.basis)
    # Ask each bank for one more than requested: a full page signals there may be more.
    batches = [backend.recall(bank_id, request.query, tag_groups=tag_groups,
                               limit=request.max_results + 1)
               for bank_id in _recall_banks(db, principal, request)]
    floor = get_settings().recall_min_semantic
    kept = [[hit for hit in batch if hit.score is None or hit.score >= floor] for batch in batches]
    # Each bank's hits come in the engine reranker's order, which `score` (the semantic
    # similarity) knows nothing of; two banks interleave by rank, project first.
    merged = [hit for rank in zip_longest(*kept) for hit in rank if hit is not None][: request.max_results]
    rows = catalog.by_ids(db, [hit.memory_id for hit in merged])
    items, note = _with_relevance(request.query, tuple(_to_hit(hit, rows.get(hit.memory_id)) for hit in merged))
    return RecallResponse(note=note, items=items, total=len(items),
                          truncated=any(len(batch) > request.max_results for batch in batches))

def _with_relevance(query: str, items: tuple[RecallHit, ...]) -> tuple[tuple[RecallHit, ...], str | None]:
    """The first hits with the decision model's `relevance`, and the "nothing clear" note when
    the best is low. Nothing is dropped or reordered. Without a model, or when the call fails or
    is slow, the hits come back as they were."""
    settings = get_settings()
    head = items[:RELEVANCE_TOP]
    if not head or not (settings.llm_url and settings.llm_decision_model):
        return items, None
    listing = "\n\n".join(f"[{n}] {hit.title or ''}\n{hit.content}" for n, hit in enumerate(head, 1))
    text = f"Query from a coding agent searching its project's memory:\n{query}\n\nMemories:\n{listing}"
    questions = [f"Does memory [{n}] help answer the query?" for n in range(1, len(head) + 1)]
    try:
        probs = llm.within(settings.recall_relevance_timeout_seconds, llm.decide, settings, text, questions)
    except Exception as exc:  # noqa: BLE001 -- relevance is advisory; recall answers without it
        logger.warning("recall relevance failed: %s", type(exc).__name__)
        return items, None
    scored = tuple(hit.model_copy(update={"relevance": round(p, 3)}) for hit, p in zip(head, probs, strict=True))
    return scored + items[len(head):], NOTHING_CLEAR if max(probs) < settings.recall_relevance_note_below else None

def reflect(db: Session, principal: Principal, request: ReflectRequest, *, backend: Backend) -> ReflectResponse:
    if "synthesis" not in backend.capabilities():
        raise UnsupportedCapability("synthesis")
    ref, _ = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    answer = backend.reflect(ref.bank_id, request.query,
                             tag_groups=_tag_groups(request.memory_types, request.basis))
    return ReflectResponse(answer=answer)

def list_memories(db: Session, principal: Principal, request: ListRequest, *, backend: Backend) -> ListResponse:
    """Titles from ACH's own record, newest first; the text only with `full`, one engine read per item."""
    try:
        ref, _ = resolve_bank(db, principal, request.scope, request.project_slug, create=False)
    except ProjectNotFound:
        return ListResponse(items=(), total=0, limit=request.limit, offset=request.offset)
    where = [Memory.owner_id == ref.owner_id, Memory.state == (request.state or "valid")]
    if request.memory_types: where.append(Memory.memory_type.in_(request.memory_types))
    if request.family: where.append(Memory.family == request.family)
    if request.pinned is not None: where.append(Memory.pinned.is_(request.pinned))
    total = db.scalar(select(func.count()).select_from(Memory).where(*where)) or 0
    rows = db.scalars(select(Memory).where(*where).order_by(Memory.created_at.desc(), Memory.memory_id)
                      .offset(request.offset).limit(request.limit)).all()
    items = []
    for row in rows:
        # ponytail: one engine read per row with `full`; a batch read if `limit=100` lists get slow.
        view = backend.get(ref.bank_id, row.memory_id) if request.full else None
        items.append(MemoryItem(memory_id=row.memory_id, title=row.title, family=row.family, pinned=row.pinned,
                                memory_type=row.memory_type, state=row.state, created_at=row.created_at.isoformat(),
                                content=view.text if view else None))
    return ListResponse(items=tuple(items), total=total, limit=request.limit, offset=request.offset)

def get_memory(db: Session, principal: Principal, request: GetRequest, *, backend: Backend) -> MemoryItem:
    ref, _ = resolve_memory_bank(db, principal, request.scope, request.project_slug, request.memory_id)
    view = backend.get(ref.bank_id, request.memory_id)
    if view is None:
        raise MemoryNotFound()
    return _to_item(view, catalog.get(db, ref, request.memory_id))

def history(db: Session, principal: Principal, request: HistoryRequest, *, backend: Backend) -> HistoryResponse:
    ref, _ = resolve_memory_bank(db, principal, request.scope, request.project_slug, request.memory_id)
    view = backend.get(ref.bank_id, request.memory_id)
    current = _to_item(view, catalog.get(db, ref, request.memory_id)) if view is not None else None
    # I1: the journal is read through the caller's own bank, never by memory id alone.
    stmt = select(AuditEvent).where(AuditEvent.resource == request.memory_id,
                                    AuditEvent.details["owner"].as_string() == ref.owner_id).order_by(
        AuditEvent.created_at.desc(), AuditEvent.id.desc())
    rows = db.scalars(stmt).all()
    journal = tuple(JournalEntry(action=r.action, actor=r.actor_user_id, on_behalf_of=r.on_behalf_of,
                                  operation_id=r.operation_id, created_at=r.created_at.isoformat(),
                                  details=r.details or {}) for r in rows)
    return HistoryResponse(current=current, journal=journal)
