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

import re
from typing import Any, get_args

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import read_context
from memory.auth.principal import Principal
from memory.config import get_settings
from memory.currentness import bank_is_withheld
from memory.db import db_now
from memory.errors import BankCurrentnessUnavailable, MemoryNotFound
from memory.expiry import ensure_no_expiry_backlog
from memory.hindsight.client import get_client
from memory.memory_types import EvidenceBasis, MemoryType
from memory.models import CurationOperation, RetainedRecord
from memory.read_models import (
    MAX_HIT_TEXT_LENGTH,
    MAX_PROVENANCE_EVIDENCE_RAW_LENGTH,
    CurationEvent,
    CurrentFact,
    FactType,
    HistoryChange,
    HistoryRequest,
    HistoryResponse,
    MemoryState,
    Provenance,
    ProvenanceEvidence,
    RecallHit,
    RecallRequest,
    RecallResponse,
    SourceFact,
    View,
    build_history_response,
    build_recall_response,
    resolve_filters,
)
from memory.retained_records import LogicalBankRef, get_by_source_memory_id
from memory.tags import RESERVED_PREFIXES

_MEMORY_TYPES = set(get_args(MemoryType))
_BASES = set(get_args(EvidenceBasis))
_FACT_TYPES = set(get_args(FactType))
_STATES = set(get_args(MemoryState))

# Two bounds, because the work being bounded is two different things and one
# number on the wrong one silently decided which answers exist.
#
# A single `raw_results[:200]` used to do both, and it sliced in upstream's
# `final` order while the floor immediately after judged `semantic` -- a
# different axis, and the one a measured reranker false negative already got
# wrong (0.000024 on a query a fact with `semantic` 0.6135 plainly answered).
# So a hit the floor WOULD have admitted could be discarded before the floor
# ever saw it, for ranking badly among junk. Scanning is cheap --
# `_passes_semantic_floor` is two dict lookups -- and normalizing is not, so
# the floor now sees everything and only survivors are built.
#
# Insurance, not a repair: measured both ways on a 129-claim bank, slicing
# first left 58 of 258 entries unjudged and cost no answer (24/25 either way,
# and the one loss is the relative cut's, below). It was also unreachable
# until this commit, because upstream's own 4096-token default truncated to
# 122-167 entries before 200 could bite -- raising `_RECALL_MAX_TOKENS` is
# what makes this bound live, which is why the two changes belong together.
#
# Sized against measurement (2026-09-11, 129-claim bank): upstream returns
# ~27 tokens per entry and two entries per claim, so `_RECALL_MAX_TOKENS`
# admits at most ~1200 entries and 2000 leaves margin without letting a
# hostile or broken response drive unbounded iteration. 200 normalized hits
# is ~16x the 12.7 hits per query the 0.60 floor actually admitted.
_MAX_RAW_RESULTS_SCANNED = 2000
_MAX_HITS_NORMALIZED = 200
_MAX_RAW_CHANGES_CONSIDERED = 50

# Upstream's own result budget, which it applies in `final` order and reports
# nothing about -- its response carries `results` and no truncation flag of
# any kind, so a cut is invisible by construction.
#
# Never sent before, which meant Hindsight's 4096 default. Measured on a
# 129-claim bank: that default returned 161 of 258 entries and 109 of 129
# claims, cutting 38% of the bank in reranker order before this service saw
# it, and the cut varied per query (122-167 entries) because a token budget
# is not a row count. Raised so the floor is what narrows a recall, on the
# axis that means the same thing on every query, rather than a budget
# upstream spends on whatever the reranker happened to rank first.
#
# Not unbounded: a bank past roughly 600 claims is truncated again, just
# further out. That ceiling is upstream's to report and it does not, so
# there is nothing to detect here -- only a number to keep ahead of real
# banks.
_RECALL_MAX_TOKENS = 32768


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


#: The provenance suffix Hindsight appends to an observation's own text
#: (`... (mentioned_at=2026-09-11 10:14:32.619026+00:00)`). Stripped for the
#: duplicate comparison only: the hit's text is returned exactly as upstream
#: sent it, because the suffix is upstream's rendering and not ours to rewrite.
_MENTIONED_AT_SUFFIX = re.compile(r"\s*\(mentioned_at=[^)]*\)\s*$")


def _collapse_duplicate_claims(
    hits: list[RecallHit],
    sources: dict[str, tuple[str, ...]] | None = None,
) -> list[RecallHit]:
    """One claim, one slot. Keeps the most traceable copy of each identical text.

    `sources` maps a hit's memory_id to the `source_fact_ids` upstream reported
    for it. It drives a second pass, after text grouping, that catches what
    identical text cannot: see `_restates_a_present_source` below.

    Two separate mechanisms put the same sentence in a response several times,
    measured on a 10-claim bank (2026-09-11):

    * Every retained claim exists upstream twice -- once as the `world` fact
      and once as Hindsight's own `observation` of it, same text -- and
      `resolve_filters` asks for both types, so a bank nothing was ever
      re-retained into already answered every query at 2x.
    * A re-retain of identical content is a NEW claim: `accept_retain`
      compares `payload_hash` only against a row it already found by
      `operation_id`, and nothing indexes or queries that hash alone. The
      same sentence sent four times is four facts and eight upstream entries.

    Together they cost 7 of 8 slots on the worst measured query: eight hits,
    one claim. Neither threshold can help -- identical text scores
    identically, so the floor and the relative cut keep or drop all copies
    alike.

    Which copy survives is decided by `document_id`, not by rank. Upstream
    orders by `final` and the `world` fact outranks its observation almost
    always, so first-wins would usually pick it anyway -- but the floor runs
    BEFORE this and judges each copy separately, and the twins' scores sit
    within 0.002% of each other (1.0997851 vs 1.0997698, measured), so a fact
    can be withheld while its observation passes. First-wins then returned
    the observation: on one measured query that was 3 of 3 hits, each
    carrying no `document_id` and a `(mentioned_at=...)` suffix upstream
    renders into the text. Preferring the traceable copy costs a score
    difference in that fifth decimal and buys provenance the caller can
    follow.

    Nothing is merged: the hit returned is one upstream record, exactly as it
    arrived. A genuine consolidation is never dropped -- but text is not what
    proves that any more. Measured in production on 2026-09-11 (n=30, two
    banks): a single-source observation is REWORDED, not copied, so its text
    never matched its source's and both survived text grouping. `sources`
    catches that case afterward: an observation with exactly one source
    already present in the response is dropped as the copy that adds no
    claim the caller is not already getting, while one with two or more
    sources always survives -- it says something none of them says alone.
    """
    groups: dict[str, RecallHit] = {}
    for hit in hits:
        key = _MENTIONED_AT_SUFFIX.sub("", hit.text).strip()
        kept = groups.get(key)
        if kept is None or (kept.document_id is None and hit.document_id is not None):
            groups[key] = hit
    survivors = list(groups.values())
    if not sources:
        return survivors
    # Over every input hit, not the survivors: a source the text grouping
    # folded into an identical copy is still a claim the caller receives.
    present = {hit.memory_id for hit in hits}
    return [hit for hit in survivors if not _restates_a_present_source(hit, sources, present)]


def _restates_a_present_source(
    hit: RecallHit, sources: dict[str, tuple[str, ...]], present: set[str]
) -> bool:
    """One source, already in the response, means this observation carries no
    claim the caller is not getting anyway -- and it is the copy with no
    `document_id`, so it is the one to lose.

    Text equality alone could not see this: measured 2026-09-11, a single-source
    observation is REWORDED, not copied. Two or more sources is a consolidation
    and is never dropped, however present its sources are.
    """
    if hit.fact_type != "observation":
        return False
    ids = sources.get(hit.memory_id, ())
    return len(ids) == 1 and ids[0] in present


def _apply_relative_cut(hits: list[RecallHit], ratio: float) -> list[RecallHit]:
    """Drop the tail the best hit in this same response makes irrelevant.

    A hit with no score is kept: an unscored hit is unjudged, not judged
    badly, and upstream's contract allows it. When nothing is scored there
    is no reference to measure against, so the set passes untouched.
    """
    if ratio <= 0:
        return hits
    scored = [hit.score for hit in hits if hit.score is not None]
    if not scored:
        return hits
    threshold = max(scored) * ratio
    return [hit for hit in hits if hit.score is None or hit.score >= threshold]


def _score(raw: Any, stage: str) -> float | None:
    """One upstream per-stage score, or None if it sent none.

    Defensive at every step because `scores` is nullable in upstream's own
    contract and absent entirely for source facts. `bool` is excluded before
    the numeric check for the usual reason -- it is an `int` to Python, and
    `True` would sail through as a score of 1.0.
    """
    if not isinstance(raw, dict):
        return None
    scores = raw.get("scores")
    if not isinstance(scores, dict):
        return None
    value = scores.get(stage)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _passes_semantic_floor(
    raw: Any, floor: float, *, keyword_only_min_reranker: float = 0.0
) -> bool:
    """Whether a raw hit is about the query at all.

    `semantic` is a cosine similarity, so unlike `final` it means the same
    thing on every query and every bank. A hit that upstream surfaced by
    keyword alone reports no semantic score; it is judged by the reranker
    instead when one ran (`config.recall_keyword_only_min_reranker`), and kept
    unjudged when none did -- dropping it outright would silently narrow
    recall to the vector arm.
    """
    semantic = _score(raw, "semantic")
    if semantic is not None:
        return floor <= 0 or semantic >= floor
    if keyword_only_min_reranker <= 0:
        return True
    reranker = _score(raw, "reranker")
    return reranker is None or reranker >= keyword_only_min_reranker


def _caller_tags_of(tags: Any) -> tuple[str, ...]:
    """The caller-authored tags on a hit, in the order `normalize_caller_tags`
    would have produced.

    Server-derived tags are dropped rather than forwarded: `type:`/`basis:`
    already reach the caller as `memory_type`/`basis`, and `schema:`/
    `validity:` are internal bookkeeping that the read surface has never
    exposed. So this returns what the caller itself wrote and nothing of
    Hindsight's own vocabulary.
    """
    if not isinstance(tags, list):
        return ()
    return tuple(
        sorted(
            tag
            for tag in tags
            if isinstance(tag, str)
            and not any(tag.startswith(prefix) for prefix in RESERVED_PREFIXES)
        )
    )


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
            memory_type=_kind_of(tags),
            basis=_origin_of(tags),
            mentioned_at=_str_or_none(raw.get("mentioned_at")),
            document_id=_str_or_none(raw.get("document_id")),
            tags=_caller_tags_of(tags),
            score=_score(raw, "final"),
        )
    except ValidationError:
        return None


def whitelist_reflect_evidence(result: Any) -> Any:
    """Reduce upstream's `based_on` to what a caller may see: the memories.

    Requested with `include_facts=True`, upstream returns three lists. Only
    `memories` survives, and each entry only as `memory_id`/`text`/`fact_type`
    -- upstream's `id`/`type` under the names a recall hit carries (QA F-17),
    the same identity class `RecallHit.memory_id` already exposes, so nothing
    new crosses the boundary. `mental_models` carries upstream's own model ids,
    which every surface here keeps internal (`upstream_model_id`) behind a
    caller-facing `model_key`; `directives` is not a surface this service
    exposes at all. An upstream `context`/`occurred_*` on a memory is dropped
    for the same reason `RecallHit` never carried them.

    Whitelisted field by field, not stripped: a field upstream adds tomorrow
    has no place to land. A result with no `based_on` is returned untouched
    rather than given an empty one -- absent and empty are different answers
    to "what was this grounded on".
    """
    if not isinstance(result, dict) or "based_on" not in result:
        return result
    based_on = result["based_on"]
    if not isinstance(based_on, dict):
        # Present but not the documented shape: nothing in it is known to be
        # safe to forward, and a whitelist forwards only what it knows.
        return {key: value for key, value in result.items() if key != "based_on"}
    memories = based_on.get("memories")
    kept = []
    if isinstance(memories, list):
        for item in memories:
            if not isinstance(item, dict):
                continue
            text = _str_or_none(item.get("text"))
            if text is None:
                continue
            kept.append(
                {
                    "memory_id": _str_or_none(item.get("id")),
                    "text": text,
                    "fact_type": _str_or_none(item.get("type")),
                }
            )
    return {**result, "based_on": {"memories": kept}}


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

    Returns every hit that CLEARED THE RELEVANCE FLOOR and normalized
    cleanly, NOT sliced to any `max_results` -- the caller decides how much
    of this to keep and whether that makes the response truncated. The floor
    is applied upstream, not here, so `max_results` now caps a set that is
    already relevant rather than padding it out of the tail.

    Both thresholds come from configuration only. There is no per-request
    override and deliberately so -- see `read_models`' module docstring. They
    are also read independently of each other: each answers a different
    question about a hit, so neither disabling the other is a behaviour any
    caller asked for.
    """
    settings = get_settings()
    floor = settings.recall_min_semantic
    ratio = settings.recall_relative_cut
    filters = resolve_filters(view, memory_types, caller_tags)
    raw = get_client().recall(
        bank_id,
        query,
        with_entities=False,
        types=list(filters.types),
        # `tag_groups`, never `tags`: upstream treats the two as mutually
        # exclusive, and only the grouped form can give the server's scoping
        # tags and the caller's narrowing tags different match modes in one
        # query. See `read_models.resolve_filters`.
        tag_groups=list(filters.tag_groups),
        max_tokens=_RECALL_MAX_TOKENS,
        # An observation carries no `document_id` of its own; its
        # `source_fact_ids` are the only link back to the `world` fact it
        # was derived from, and `_collapse_duplicate_claims` needs that link
        # to recognize a paraphrased twin. See Task 4.
        with_source_facts=True,
    )
    raw_results = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(raw_results, list):
        raw_results = []
    # Filtered here rather than through upstream's own `min_scores`, which
    # cannot express this: its `semantic` floor is a RETRIEVAL-level cutoff
    # pushed into the vector arm only, so a hit surfaced by keyword bypasses
    # it entirely and the filter would mean something different depending on
    # which arm found the hit.
    hits: list[RecallHit] = []
    # Keyed by memory_id rather than carried on RecallHit: the ids are only
    # for this module's own collapse, and RecallHit is `extra="forbid"` on
    # purpose (see `read_models`).
    sources: dict[str, tuple[str, ...]] = {}
    for item in raw_results[:_MAX_RAW_RESULTS_SCANNED]:
        if not _passes_semantic_floor(
            item, floor, keyword_only_min_reranker=settings.recall_keyword_only_min_reranker
        ):
            continue
        hit = _normalize_hit(item)
        if hit is not None:
            hits.append(hit)
            raw_ids = item.get("source_fact_ids")
            if isinstance(raw_ids, list):
                sources[hit.memory_id] = tuple(i for i in raw_ids if isinstance(i, str))
        if len(hits) >= _MAX_HITS_NORMALIZED:
            break
    return _apply_relative_cut(_collapse_duplicate_claims(hits, sources), ratio)


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
        read_bank.bank_id, request.query, request.view, request.memory_types,
        request.tags_filter,
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
        return CurrentFact(text=text, state=state, memory_type=_kind_of(raw.get("tags")))
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
            basis=_origin_of(raw.get("tags")),
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


def _provenance_of(record: RetainedRecord) -> Provenance:
    evidence = []
    for item in record.sanitized_evidence or ():
        kind, raw = item.get("kind"), item.get("raw")
        if not isinstance(kind, str) or not isinstance(raw, str):
            continue
        evidence.append(
            ProvenanceEvidence(
                kind=kind,
                raw=raw[:MAX_PROVENANCE_EVIDENCE_RAW_LENGTH],
                source_ref=_str_or_none(item.get("source_ref")),
            )
        )
    return Provenance(
        record_id=str(record.id),
        basis=record.basis,
        trigger=record.trigger,
        recorded_at=record.recorded_at.isoformat(),
        valid_until=record.valid_until.isoformat() if record.valid_until else None,
        lifecycle=record.lifecycle,
        tags=tuple(record.caller_tags or ()),
        evidence=tuple(evidence),
    )


def _curation_of(db: Session, record: RetainedRecord) -> list[CurationEvent]:
    ops = db.scalars(
        select(CurationOperation)
        .where(CurationOperation.retained_record_id == record.id)
        .order_by(CurationOperation.created_at)
    )
    return [
        CurationEvent(
            operation_id=op.operation_id,
            action=op.action,
            state=op.state,
            reason=op.reason,
            # A corrected claim is bounded in BYTES (4096); the hit-text cap
            # is in characters (4000), so an ASCII-heavy correction can
            # exceed it. Cut rather than let a row fail the whole history.
            desired_content=(
                op.desired_content[:MAX_HIT_TEXT_LENGTH] if op.desired_content else None
            ),
            created_at=op.created_at.isoformat(),
            completed_at=op.completed_at.isoformat() if op.completed_at else None,
        )
        for op in ops
    ]


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

    try:
        history_raw = client.get_memory_history(read_bank.bank_id, request.memory_id)
    except MemoryNotFound:
        # Hindsight serves revision history for current memories only: once a
        # memory is invalidated the history route 404s although get_memory
        # above still returned it. After a forget, ACH's own provenance and
        # curation below are what the caller came for, so serve them with no
        # upstream revisions rather than fail the whole read (0.7.3 retest).
        history_raw = []
    changes = _normalize_changes(history_raw)

    # The half Hindsight cannot tell (QA F-13/F-14): what ACH recorded at
    # retain time and every curation it has applied since. Same bank scoping
    # as the two upstream calls -- `get_by_source_memory_id` never looks
    # across banks either.
    record = get_by_source_memory_id(db, bank_ref(principal, read_bank), request.memory_id)
    provenance = _provenance_of(record) if record is not None else None
    curation = _curation_of(db, record) if record is not None else []

    return build_history_response(
        project_slug=read_bank.current_slug,
        resolved_from=read_bank.resolved_from,
        memory_id=request.memory_id,
        current=current,
        changes=changes,
        provenance=provenance,
        curation=curation,
    )
