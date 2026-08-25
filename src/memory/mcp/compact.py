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
the pinned `hindsight-api==0.9.1` (`Dockerfile.hindsight`): `RecallResult` and
`ReflectResponse` in `hindsight_api/api/http.py`, and the memory-unit row built
in `hindsight_api/engine/memories/pg/curation.py`.
"""

from typing import Any, NamedTuple

# Facts, as recall returns them.
_RECALL_FACT = frozenset(
    {
        # No tool of §11 accepts a chunk id, so it is unusable by the caller.
        # It is also where a bank id hides as a substring (see
        # `_strip_bank_id`), which is why compaction runs AFTER that filter and
        # never in place of it.
        "chunk_id",
        # SPEC §13.6: v1 writes no retrieval tags, so this is [] on every read.
        "tags",
        # Only resolvable through the `source_facts` map, which is disabled
        # upstream by default and which we never request — so these ids point
        # at nothing in the same response.
        "source_fact_ids",
        # A list of names already present verbatim in `text`.
        "entities",
    }
)

# Memory units, as list_memories and get_memory return them.
_MEMORY_UNIT = frozenset(
    {
        "chunk_id",
        "tags",
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
    """How one action's payload is reduced.

    `item_field` names the list the payload carries (`results` for recall,
    `items` for the list tools); None means the payload IS the single item, as
    it is for the three get_* tools. `date_anchor` is the key whose timestamp
    the other timestamps are compared against; None disables the collapse.
    """

    top: frozenset[str] = frozenset()
    item_field: str | None = None
    item: frozenset[str] = frozenset()
    date_anchor: str | None = None
    scores: bool = False


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
        # answer. `based_on` and `trace` are gated behind `include`, which
        # defaults to disabled upstream and which we do not send, so reflect is
        # already lean: `text` is the payload and it is left untouched.
        top=frozenset({"usage"}),
    ),
    "memory.list": _Rule(
        item_field="items",
        item=_MEMORY_UNIT,
        date_anchor="date",
    ),
    "memory.get": _Rule(
        item=_MEMORY_UNIT,
        date_anchor="date",
    ),
    "memory.documents.list": _Rule(item_field="items", item=_DOCUMENT),
    "memory.documents.get": _Rule(item=_DOCUMENT),
    "memory.operations.list": _Rule(item_field="items", item=_OPERATION),
    "memory.operations.get": _Rule(item=_OPERATION),
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


def compact(action: str, payload: Any) -> Any:
    """Reduce one upstream payload for `action`.

    An action with no rule — every write tool — is returned with nothing but
    its nulls dropped. A payload that is not an object is returned untouched;
    `_run` rejects that shape immediately afterwards anyway.

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
