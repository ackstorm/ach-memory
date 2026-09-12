"""What the read tools of SPEC §11 drop before a payload reaches an agent.

Every tool used to forward Hindsight's response verbatim (`_strip_bank_id` was
the only filter, and it removes exactly one key). That is correct but
expensive: an agent pays context for every field, and most of what Hindsight
returns is addressed to a caller this surface does not have. `chunk_id` is the
clearest case — no tool of §11 accepts one, so it can only ever be read and
discarded.

These are EXCLUDE lists, not allowlists, and the difference is deliberate: a
field a future Hindsight adds reaches the agent untouched instead of vanishing
silently. The failure mode points at "too much", which is recoverable by adding
a key here, rather than at "too little", which is invisible until someone
notices the agent stopped seeing something. Every removal is `pop(key, None)`,
so a key that is absent — a field gated behind a request option we do not send,
a shape that changed upstream — is a no-op, never an error.

`verbose=True` on a tool skips all of this and returns the upstream payload
exactly as before, so nothing here is load-bearing for correctness.

Field-by-field reasoning lives next to each set below. Shapes were read from
the pinned `hindsight-api==0.9.2` (`Dockerfile.hindsight`): `RecallResult` and
`ReflectResponse` in `hindsight_api/api/http.py`, and the memory-unit row built
in `hindsight_api/engine/memories/pg/curation.py`.
"""

from typing import Any, NamedTuple

# `unify` below: the wire names that differ from the parameter names the
# surface accepts. A NamedTuple default is one shared object, so the empty
# mapping is a module constant nothing may mutate.
_NO_RENAMES: dict[str, str] = {}
# Hindsight names every record `id`; the tool that takes it back says which
# id it is. `type` on a memory unit is the world/experience/observation kind
# that `list_memories` filters on as `type` but recall returns as `fact_type`.
_MEMORY_NAMES = {"id": "memory_id", "type": "fact_type"}

# Facts, as recall returns them. `tags` is deliberately NOT here: retain has
# written caller tags (e.g. `repo:group/app`) since v0.4.x, and a caller
# filtering recall/reflect by one needs to see it on the result to tell
# scoped claims apart -- stripping it would make the filter useless.
_RECALL_FACT = frozenset(
    {
        # No tool of §11 accepts a chunk id, so it is unusable by the caller.
        # It is also where a bank id hides as a substring (see
        # `_strip_bank_id`), which is why compaction runs AFTER that filter and
        # never in place of it.
        "chunk_id",
        # Only resolvable through the `source_facts` map, which is disabled
        # upstream by default and which we never request — so these ids point
        # at nothing in the same response.
        "source_fact_ids",
        # A list of names already present verbatim in `text`.
        "entities",
    }
)

# Memory units, as list_memories and get_memory return them. `tags`: same
# reasoning as _RECALL_FACT above.
_MEMORY_UNIT = frozenset(
    {
        "chunk_id",
        "entities",
        # Hindsight's own extraction-pipeline bookkeeping. Nothing on this
        # surface reacts to it. `state`, `invalidation_reason`, `invalidated_at`
        # and `edited_at` are deliberately NOT here: they are what the curation
        # workflow reads, and they are already absent from a valid memory
        # because null values are dropped.
        "proof_count",
        "consolidated_at",
        "consolidation_failed_at",
    }
)

# Documents, as list_documents and get_document return them.
_DOCUMENT = frozenset(
    {
        # An internal dedup hash. The caller chooses document ids itself
        # (see list_documents' description) and never sees a hash anywhere else.
        "content_hash",
        "tags",
    }
)

# Async operations, as list_operations and get_operation return them.
_OPERATION = frozenset(
    {
        # Worker internals behind a status the agent is already polling.
        "retry_count",
        "next_retry_at",
        "progress",
        # Set only for file_convert_retain, a task type this surface never
        # issues.
        "filename",
        # Always 1: the client sends one item per retain.
        "items_count",
    }
)


class _Rule(NamedTuple):
    """How one action's payload is reduced, and how its keys are named.

    `item_field` names the list the payload carries (`results` for recall,
    `items` for the list tools); None means the payload IS the single item, as
    it is for the three get_* tools. `date_anchor` is the key whose timestamp
    the other timestamps are compared against; None disables the collapse.
    `rename_top`/`rename_item` are `unify`'s old-name -> new-name maps for the
    envelope and for each item; `item_field` is read AFTER `rename_top`, so a
    rule that renames its own wrapper names the wrapper's NEW key.
    """

    top: frozenset[str] = frozenset()
    item_field: str | None = None
    item: frozenset[str] = frozenset()
    date_anchor: str | None = None
    scores: bool = False
    rename_top: dict[str, str] = _NO_RENAMES
    rename_item: dict[str, str] = _NO_RENAMES


# Keyed by the `action` string `_run` already threads through the pipeline.
_RULES: dict[str, _Rule] = {
    "memory.recall": _Rule(
        top=frozenset(
            {
                # `include.entities` defaults to ENABLED upstream, so every
                # recall carried an entity-observation map nothing asked for.
                # The client now disables it on the request too, which also
                # saves the work of building it; this stays as the belt to that
                # braces, since the default lives in Hindsight, not here.
                "entities",
                # Gated behind request options we never send, so normally
                # absent — listed because "normally" is not "always".
                "trace",
                "source_facts",
                "source_facts_truncated",
            }
        ),
        item_field="results",
        item=_RECALL_FACT,
        date_anchor="occurred_start",
        scores=True,
    ),
    "memory.reflect": _Rule(
        # Token accounting for the service, not for the agent reading the
        # answer. `based_on` is requested and arrives already reduced to
        # {memories: [{memory_id, text, fact_type}]} by `read_service.
        # whitelist_reflect_evidence`, so there is nothing left to prune in it;
        # `trace` stays behind an `include` this service never sends.
        top=frozenset({"usage"}),
    ),
    "memory.list": _Rule(
        item_field="items",
        item=_MEMORY_UNIT,
        date_anchor="date",
        rename_item=_MEMORY_NAMES,
    ),
    "memory.get": _Rule(
        item=_MEMORY_UNIT,
        date_anchor="date",
        rename_top=_MEMORY_NAMES,
    ),
    # Rename-only rules: the three curation tools never compact (they run
    # `_run` with its default `verbose=True`), but Hindsight's untracked
    # reply to them is a memory unit and has to say `memory_id` like the
    # ACH-built one does.
    "memory.forget": _Rule(rename_top=_MEMORY_NAMES),
    "memory.restore": _Rule(rename_top=_MEMORY_NAMES),
    "memory.correct": _Rule(rename_top=_MEMORY_NAMES),
    "memory.documents.list": _Rule(
        item_field="items", item=_DOCUMENT, rename_item={"id": "document_id"}
    ),
    "memory.documents.get": _Rule(item=_DOCUMENT, rename_top={"id": "document_id"}),
    # Hindsight wraps this one list in `operations`, not `items`. Until
    # `unify` renamed the wrapper, `item_field="items"` here found nothing and
    # the `_OPERATION` drops silently never applied to a listed row; they do
    # now, because compaction runs after the rename.
    "memory.operations.list": _Rule(
        item_field="items",
        item=_OPERATION,
        rename_top={"operations": "items"},
        rename_item={"id": "operation_id"},
    ),
    "memory.operations.get": _Rule(item=_OPERATION, rename_top={"id": "operation_id"}),
}

# The timestamps that collapse into `date_anchor` when they carry the same
# instant. A memory recorded from a single statement repeats one string across
# all of these; a fact that genuinely spans time keeps every distinct value.
_TIMESTAMPS = ("occurred_start", "occurred_end", "mentioned_at", "date")


def _collapse_dates(item: dict[str, Any], anchor: str) -> None:
    """Drop a timestamp that only repeats the anchor's own value.

    Nothing is lost: an absent key means "same as the anchor". A fact whose
    start and end really differ keeps both, which is the only case where the
    distinction carried information in the first place.
    """
    value = item.get(anchor)
    if value is None:
        return
    for key in _TIMESTAMPS:
        if key != anchor and item.get(key) == value:
            item.pop(key, None)


def _reduce_scores(item: dict[str, Any]) -> None:
    """Keep the ranking signal, drop the tuning internals.

    `final` is the number that orders the results. `reranker`, `semantic` and
    the keyword arm are inputs to it, uncalibrated across queries by Hindsight's
    own admission (see `min_scores` in its RecallRequest), so they cannot be
    compared to anything. Two decimals is enough to tell a strong hit from a
    weak one.
    """
    scores = item.get("scores")
    if not isinstance(scores, dict):
        return
    final = scores.get("final")
    if final is None:
        item.pop("scores", None)
    elif isinstance(final, (int, float)) and not isinstance(final, bool):
        item["scores"] = {"final": round(final, 2)}
    else:
        item["scores"] = {"final": final}


def _reduce_item(item: Any, rule: _Rule) -> None:
    if not isinstance(item, dict):
        return
    for key in rule.item:
        item.pop(key, None)
    if rule.date_anchor:
        _collapse_dates(item, rule.date_anchor)
    if rule.scores:
        _reduce_scores(item)


def _drop_nulls(value: Any) -> Any:
    """Remove null-valued keys, recursively.

    Lossless: a key set to null and a key that is absent say the same thing,
    and both surfaces already type every optional field as nullable. Empty
    lists survive on purpose — `results: []` means "searched, found nothing",
    which is not the same as the key being missing.
    """
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


def _rename(item: Any, names: dict[str, str]) -> None:
    """Move each old key to its new name. Only when the old one is present and
    the new one is absent: a payload that already says `operation_id` (an
    ACH-built curation reply, a ledger-described operation) keeps it, and a
    stray `id` beside it is left where it was rather than overwriting it."""
    if not isinstance(item, dict):
        return
    for old, new in names.items():
        if old in item and new not in item:
            item[new] = item.pop(old)


def unify(action: str, payload: Any) -> Any:
    """Name every key of one upstream payload after the parameter that
    accepts it (QA F-17): `memory_id`, `document_id`, `operation_id`,
    `fact_type`, and `items` for every list.

    Runs on every read and curation reply, reduced or not -- the names are
    the contract, not a reduction, so `verbose` does not skip this the way it
    skips `compact`. No aliases: the old name goes away. An action with no
    rule, or a payload that is not an object, is returned untouched. Mutates
    in place, on the same grounds as `compact`.
    """
    if not isinstance(payload, dict):
        return payload
    rule = _RULES.get(action)
    if rule is None:
        return payload
    _rename(payload, rule.rename_top)
    if rule.rename_item and rule.item_field is not None:
        items = payload.get(rule.item_field)
        if isinstance(items, list):
            for item in items:
                _rename(item, rule.rename_item)
    return payload


def compact(action: str, payload: Any) -> Any:
    """Reduce one upstream payload for `action`.

    Only the eight read tools call this; the write tools return their payload
    untouched. An action with no rule therefore means a read tool was added
    without one — it falls back to dropping nulls, which is safe for any shape.
    A payload that is not an object is returned untouched.

    Mutates in place, which is safe because `_strip_bank_id` has already
    rebuilt the whole structure by the time this runs.
    """
    if not isinstance(payload, dict):
        return payload

    rule = _RULES.get(action)
    if rule is None:
        return _drop_nulls(payload)

    for key in rule.top:
        payload.pop(key, None)

    if rule.item_field is None:
        _reduce_item(payload, rule)
    else:
        items = payload.get(rule.item_field)
        if isinstance(items, list):
            for item in items:
                _reduce_item(item, rule)

    return _drop_nulls(payload)
