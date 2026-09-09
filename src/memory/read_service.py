"""Resolve, call Hindsight, normalize -- always in that order.

Every function here trusts an already-validated `RecallRequest`/
`HistoryRequest` (read_models.py) and an already-authenticated `Principal`
(the caller's own FastAPI dependency); it still authorizes the BANK itself
via `read_context.resolve_read_bank` -- never assumes a caller already did --
and never creates, enriches or writes any domain row beyond what that
resolver itself may audit.

Normalization here is deliberately conservative: every field on a
`RecallHit`/`CurrentFact`/`HistoryChange`/`SourceFact` is picked from the raw
upstream payload EXPLICITLY, by name, never by unpacking (`Model(**raw)`) --
an upstream extra (a nested `bank_id`, a raw `tags` array, an embedding) has
no field to land in regardless of what pydantic would have coerced it into.
A `pydantic.ValidationError` (oversized text, an undocumented enum value)
drops the one hit/change it came from rather than truncating or coercing a
field: one bad item lost, never one bad field silently reshaped into
something that merely looks safe.
"""

from typing import Any, get_args

from pydantic import ValidationError
from sqlalchemy.orm import Session

from memory import read_context
from memory.auth.principal import Principal
from memory.currentness import bank_is_withheld
from memory.db import db_now
from memory.errors import BankCurrentnessUnavailable, MemoryNotFound
from memory.expiry import ensure_no_expiry_backlog
from memory.hindsight.client import get_client
from memory.memory_types import EvidenceBasis, MemoryType
from memory.read_models import (
    CurrentFact,
    FactType,
    HistoryChange,
    HistoryRequest,
    HistoryResponse,
    MemoryState,
    RecallHit,
    RecallRequest,
    RecallResponse,
    SourceFact,
    View,
    build_history_response,
    build_recall_response,
    resolve_filters,
)
from memory.retained_records import LogicalBankRef

_MEMORY_TYPES = set(get_args(MemoryType))
_BASES = set(get_args(EvidenceBasis))
_FACT_TYPES = set(get_args(FactType))
_STATES = set(get_args(MemoryState))

# Defends against a hostile or merely huge upstream array: bounded
# independent of a caller's own `max_results`/change-count cap, so a
# malicious or broken Hindsight response cannot make this service do
# unbounded normalization work. Generous relative to anything a real bank
# returns (recall/history responses are themselves budget-limited upstream).
_MAX_RAW_RESULTS_CONSIDERED = 200
_MAX_RAW_CHANGES_CONSIDERED = 50


def _tag_value(tags: Any, prefix: str, allowed: set[str]) -> str | None:
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(prefix):
            value = tag.removeprefix(prefix)
            if value in allowed:
                return value
    return None


def _kind_of(tags: Any) -> MemoryType | None:
    return _tag_value(tags, "type:", _MEMORY_TYPES)


def _origin_of(tags: Any) -> EvidenceBasis | None:
    return _tag_value(tags, "basis:", _BASES)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_hit(raw: Any) -> RecallHit | None:
    """One raw `RecallResult` (hindsight-api 0.9.2) -> one `RecallHit`, or
    None to drop it."""
    if not isinstance(raw, dict):
        return None
    memory_id = _str_or_none(raw.get("id"))
    text = _str_or_none(raw.get("text"))
    fact_type = raw.get("type")
    if memory_id is None or text is None or fact_type not in _FACT_TYPES:
        return None
    tags = raw.get("tags")
    try:
        return RecallHit(
            memory_id=memory_id,
            text=text,
            fact_type=fact_type,
            # `RecallResult` carries no `state` field at all (confirmed
            # against hindsight-api 0.9.2's own openapi.json) -- recall never
            # surfaces an invalidated memory, so every hit is current by
            # construction.
            state="valid",
            kind=_kind_of(tags),
            origin=_origin_of(tags),
            occurred_at=(
                _str_or_none(raw.get("occurred_start"))
                or _str_or_none(raw.get("mentioned_at"))
            ),
            document_id=_str_or_none(raw.get("document_id")),
        )
    except ValidationError:
        return None


def ensure_current_read_allowed(db: Session, bank: LogicalBankRef) -> None:
    """Withhold a bank whose current state cannot yet be trusted.

    An ACH-mediated safety mutation on this bank (correction, forget,
    restore, expiry) may have an indeterminate upstream outcome (SPEC §5.8);
    until it is proven, every ordinary current read is refused rather than
    risk serving a state that was never actually reached. Authorized
    history/audit surfaces are unaffected -- they never claim currentness.
    """
    if bank_is_withheld(db, bank):
        raise BankCurrentnessUnavailable(
            "this bank's current state is temporarily unavailable"
        )


def bank_ref(principal: Principal, read_bank: read_context.ReadBank) -> LogicalBankRef:
    return LogicalBankRef(
        tenant_id=principal.tenant_id,
        scope=read_bank.scope,
        user_id=read_bank.user_id,
        project_internal_id=read_bank.project_internal_id,
        bank_id=read_bank.bank_id,
    )


def run_access_maintenance(db: Session, bank: LogicalBankRef) -> None:
    """The one bounded maintenance side effect an authorized recall/reflect
    access performs: at most one batch of overdue expiry (SPEC §6.4). Never
    run by `load_context` or by current list/get, which only honor an
    existing barrier without claiming new work."""
    ensure_no_expiry_backlog(db, bank, client=get_client(), now=db_now(db))


def _recall_hits(
    bank_id: str,
    query: str,
    view: View,
    memory_types: tuple[MemoryType, ...] | None,
    caller_tags: tuple[str, ...] = (),
) -> list[RecallHit]:
    """Everything AFTER a bank is already resolved, authorized and proven
    current: build the server-owned filter set, call Hindsight, normalize
    every candidate hit.

    Shared by `recall` below (which also resolves the bank itself, via
    `read_context.resolve_read_bank`, create=False) and by the legacy
    `POST /v1/memory/recall` (`api/memory.py`, which now resolves the same
    existing-only way and checks the same currentness barrier, and never
    passes `caller_tags` -- its own `RecallRequest` has no `tags` field).
    What legitimately differs between the two surfaces is resolution; how an
    already-resolved bank's recall is queried and normalized is identical on
    purpose, so both delegate to one place.

    Returns every hit that normalized cleanly, NOT sliced to any
    `max_results` -- the caller decides how much of this to keep and whether
    that makes the response truncated.
    """
    filters = resolve_filters(view, memory_types, caller_tags)
    raw = get_client().recall(
        bank_id,
        query,
        with_entities=False,
        types=list(filters.types),
        tags=list(filters.tags),
        tags_match=filters.tags_match,
    )
    raw_results = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(raw_results, list):
        raw_results = []
    hits: list[RecallHit] = []
    for item in raw_results[:_MAX_RAW_RESULTS_CONSIDERED]:
        hit = _normalize_hit(item)
        if hit is not None:
            hits.append(hit)
    return hits


def recall(
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    request: RecallRequest,
) -> RecallResponse:
    read_bank = read_context.resolve_read_bank(
        db,
        principal,
        on_behalf_of,
        "read.recall",
        request.scope,
        user_id=request.user_id,
        project_slug=request.project_slug,
    )
    db.commit()
    ref = bank_ref(principal, read_bank)
    ensure_current_read_allowed(db, ref)
    run_access_maintenance(db, ref)

    hits = _recall_hits(
        read_bank.bank_id, request.query, request.view, request.kinds, request.tags
    )
    capped = hits[: request.max_results]
    response = build_recall_response(
        project_slug=read_bank.current_slug,
        resolved_from=read_bank.resolved_from,
        hits=capped,
    )
    if len(hits) > len(capped):
        # build_recall_response only saw the already-sliced list, so it
        # cannot know max_results itself cut something off -- its own
        # truncated flag (byte/count budget) still applies too.
        response = response.model_copy(update={"truncated": True})
    return response


def _normalize_current(raw: Any) -> CurrentFact | None:
    if not isinstance(raw, dict):
        return None
    text = _str_or_none(raw.get("text"))
    state = raw.get("state")
    if text is None or state not in _STATES:
        return None
    try:
        return CurrentFact(text=text, state=state, kind=_kind_of(raw.get("tags")))
    except ValidationError:
        return None


def _normalize_source_fact(raw: Any) -> SourceFact | None:
    if not isinstance(raw, dict):
        return None
    memory_id = _str_or_none(raw.get("id"))
    text = _str_or_none(raw.get("text"))
    if memory_id is None or text is None:
        return None
    try:
        return SourceFact(
            memory_id=memory_id,
            text=text,
            origin=_origin_of(raw.get("tags")),
        )
    except ValidationError:
        return None


def _normalize_change(raw: Any, *, valid_from: str | None) -> HistoryChange | None:
    if not isinstance(raw, dict):
        return None
    text = _str_or_none(raw.get("previous_text"))
    valid_to = _str_or_none(raw.get("changed_at"))
    if text is None or valid_to is None:
        return None
    raw_source_facts = raw.get("source_facts")
    source_facts: list[SourceFact] = []
    if isinstance(raw_source_facts, list):
        for item in raw_source_facts[:5]:
            fact = _normalize_source_fact(item)
            if fact is not None:
                source_facts.append(fact)
    try:
        return HistoryChange(
            text=text,
            valid_from=valid_from,
            valid_to=valid_to,
            source_facts=tuple(source_facts),
        )
    except ValidationError:
        return None


def _normalize_changes(raw: Any) -> list[HistoryChange]:
    """Measured live against a real bank (hindsight-api 0.9.2, 2026-09-02):
    `GET .../memories/{id}/history` (untyped in its own openapi.json) is a
    bare JSON array, newest change first. Each entry:
    `{previous_text, previous_tags, previous_occurred_start,
    previous_occurred_end, previous_mentioned_at, changed_at,
    new_source_memory_ids, source_facts: [{id, text, type, context,
    is_new}, ...]}`.

    An entry's `source_facts` are the facts that caused THIS entry's
    `previous_text` to be superseded -- evidence for what it changed INTO
    (the next-newer entry's `previous_text`, or the current text for the
    newest entry), not evidence for `previous_text` itself. There is no
    cleaner unit to attach them to in `HistoryChange`: they arrive paired
    with exactly this `(previous_text, changed_at)` record and nothing else.

    An entry's `valid_from` is the NEXT (older) entry's `changed_at` -- when
    that entry's own `previous_text` (this one's predecessor) was itself
    superseded -- and None for the oldest entry, whose start this response
    does not carry.
    """
    if not isinstance(raw, list):
        return []
    entries = raw[:_MAX_RAW_CHANGES_CONSIDERED]
    changes: list[HistoryChange] = []
    for index, entry in enumerate(entries):
        valid_from = None
        if index + 1 < len(entries) and isinstance(entries[index + 1], dict):
            valid_from = _str_or_none(entries[index + 1].get("changed_at"))
        change = _normalize_change(entry, valid_from=valid_from)
        if change is not None:
            changes.append(change)
    return changes


def history(
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    request: HistoryRequest,
) -> HistoryResponse:
    read_bank = read_context.resolve_read_bank(
        db,
        principal,
        on_behalf_of,
        "read.history",
        request.scope,
        user_id=request.user_id,
        project_slug=request.project_slug,
    )
    db.commit()

    client = get_client()
    # Dereferences only inside the resolved bank: `bank_id` is part of the
    # Hindsight URL path for both calls below (paths.memory/memory_history),
    # so a memory_id belonging to a different bank 404s upstream -- there is
    # no code path here that could ever look one up across banks.
    current_raw = client.get_memory(read_bank.bank_id, request.memory_id)
    current = _normalize_current(current_raw)
    if current is None:
        # Either genuinely absent (client.get_memory already raises
        # MemoryNotFound on a real 404, so this branch is reached only by
        # a 200 whose body could not be normalized into a servable
        # CurrentFact) or malformed beyond use -- both fail closed the same
        # way, under the same typed error a caller already has to handle.
        raise MemoryNotFound(memory_id=request.memory_id)

    history_raw = client.get_memory_history(read_bank.bank_id, request.memory_id)
    changes = _normalize_changes(history_raw)

    return build_history_response(
        project_slug=read_bank.current_slug,
        resolved_from=read_bank.resolved_from,
        memory_id=request.memory_id,
        current=current,
        changes=changes,
    )
