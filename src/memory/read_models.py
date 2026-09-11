"""Closed, bounded request/response contracts for the read-only recall and
history surfaces (`POST /v1/read/recall`, `POST /v1/read/history`).

Every field here is either a caller input with server-owned validation, or a
server-constructed output whitelisted field by field -- see the plan's
"Read-only public contracts" for the wire shapes these implement
(docs/superpowers/plans/2026-09-02-memory-quality-phase-5-read-only-recall.md).
No CALLER-FACING model here ever carries `git_locator`, `tag_groups`,
`tags_match`, a bank ID or a tenant ID: the Phase 5 non-negotiable contracts
forbid all five on the read surface, and `extra="forbid"` on every request
model turns a caller who sends one into a 422, not a silently-ignored field.
`RecallFilters` does carry `tag_groups`, and that is the point of it: it is
server-constructed, never caller input, and it is the one place upstream tag
syntax is allowed to exist so that no request model has to admit it.

`RecallRequest.tags_filter` is the one deliberate, narrow exception: a
caller-supplied list, validated and normalised by
`memory.tags.normalize_caller_tags` -- the same gate retain applies, so a tag
written and a tag searched are byte-identical -- then ANDed, as a group of
its own, into the fixed server-owned strict filter (`resolve_filters`) inside
a bank the caller's own `scope`/`project_slug` already resolved and
authorized. It can only narrow what that caller could already read; it is
never a raw Hindsight tag expression, and it carries no match mode: the group
is always `all_strict` (see `memory.tags` for why the `any` mode was removed).

This module also owns the one piece of caller-controllable Hindsight
behavior a read exposes: mapping `view`/`kinds`/`tags_filter` to fixed
upstream filters (`resolve_filters`). The caller chooses from closed enums
plus its own normalised tags; the actual Hindsight
`types`/`prefer_observations`/`tag_groups` values are server-owned and never
themselves caller input.

Relevance is NOT caller input either. The floor a hit must clear lives in
`config.recall_min_semantic`, and there is no per-request override: a quality
contract a caller can switch off is not a contract, and the one caller who
reads "too few results" and sets it to 0 gets the padding back for everybody
downstream of it. Deployments tune it through the environment; `RecallHit`
carries its `score` so a caller can always see WHY something came back.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memory.identifiers import has_control_character
from memory.memory_types import EvidenceBasis, MemoryType
from memory.tags import normalize_caller_tags

ReadScope = Literal["user", "project"]

# Hindsight's own vocabulary, not a value this service computes. Duplicated
# here rather than shared, matching this codebase's existing precedent:
# curation.py, admin.py and mcp/tools.py already each define
# `Literal["world", "experience", "observation"]` independently rather than
# import one canonical copy.
FactType = Literal["world", "experience", "observation"]
MemoryState = Literal["valid", "invalidated"]
View = Literal["current", "evidence", "all"]

MAX_QUERY_LENGTH = 2048
MAX_PROJECT_SLUG_LENGTH = 128  # matches models.ProjectSlug.slug's column
MAX_KINDS = 6  # len(get_args(MemoryType)) -- a caller can never usefully
# repeat itself past naming every kind there is.
MIN_MAX_RESULTS = 1
MAX_RESULTS_CEILING = 20
DEFAULT_MAX_RESULTS = 10

MAX_HITS = MAX_RESULTS_CEILING
MAX_HIT_TEXT_LENGTH = 4_000
MAX_RECALL_RESPONSE_BYTES = 32_000

MAX_MEMORY_ID_LENGTH = 128
MAX_HISTORY_CHANGES = 10
MAX_SOURCE_FACTS_PER_CHANGE = 5
MAX_HISTORY_RESPONSE_BYTES = 32_000


def _no_control_characters(value: str | None) -> str | None:
    if value and has_control_character(value):
        raise ValueError("must not contain control characters")
    return value


class _ReadRequest(BaseModel):
    """Everything a read-only request shares: scope-resolution fields, and
    nothing a write request also carries.

    No `git_locator` field exists on this base or on either subclass below --
    unlike `api.memory.ScopedRequest`, which still carries one for the write
    surface, the Phase 5 read contract does not accept one at all. `extra=
    "forbid"` means a caller who sends one gets an ordinary 422, the same
    outcome as sending any other unknown field, never a silently-accepted
    no-op.
    """

    model_config = ConfigDict(extra="forbid")

    scope: ReadScope
    #: Accepted only for a master-key delegated read; a user key naming
    #: itself, or anyone else, is `read_context.resolve_read_bank`'s job to
    #: reject -- this model has no principal to check that against.
    user_id: str | None = None
    project_slug: str | None = Field(default=None, max_length=MAX_PROJECT_SLUG_LENGTH)

    @field_validator("user_id", "project_slug")
    @classmethod
    def _validate_no_control_characters(cls, value: str | None) -> str | None:
        return _no_control_characters(value)


class RecallRequest(_ReadRequest):
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    view: View = "current"
    kinds: tuple[MemoryType, ...] | None = Field(default=None, max_length=MAX_KINDS)
    max_results: int = Field(
        default=DEFAULT_MAX_RESULTS, ge=MIN_MAX_RESULTS, le=MAX_RESULTS_CEILING
    )
    #: Additive caller tags (e.g. `repo:group/app`), ANDed into the fixed
    #: strict filter -- see the module docstring for why this is safe.
    #: Hindsight's own `tags_match` syntax stays out of this model entirely.
    tags_filter: tuple[str, ...] = ()

    @field_validator("tags_filter", mode="before")
    @classmethod
    def _normalize_tags(cls, value: list[str] | None) -> tuple[str, ...]:
        return normalize_caller_tags(value)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        # min_length=1 alone still admits a single space: length is not
        # blankness.
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class HistoryRequest(_ReadRequest):
    memory_id: str = Field(min_length=1, max_length=MAX_MEMORY_ID_LENGTH)

    @field_validator("memory_id")
    @classmethod
    def _memory_id_no_control_characters(cls, value: str) -> str:
        if has_control_character(value):
            raise ValueError("memory_id must not contain control characters")
        return value


class RecallHit(BaseModel):
    """One grounded fact. Whitelisted field by field: an upstream extra
    (embeddings, raw chunks, entities, tool traces, a bank ID) has no field
    to land in and is dropped by construction, not by a strip step that could
    be forgotten on some other path."""

    model_config = ConfigDict(extra="forbid")

    memory_id: str
    text: str = Field(max_length=MAX_HIT_TEXT_LENGTH)
    fact_type: FactType
    state: MemoryState
    # Keep released response names; their values follow the v0.4 contracts.
    kind: MemoryType | None = None
    origin: EvidenceBasis | None = None
    occurred_at: str | None = None
    document_id: str | None = None
    #: The caller's OWN tags on this fact (e.g. `repo:group/app`), so a caller
    #: that filtered by one can see which value a hit carries. Server-derived
    #: tags are excluded: `type:`/`basis:` are already surfaced as `kind` and
    #: `origin`, and `schema:`/`validity:` are internal bookkeeping. Filtering
    #: by a tag you can never read back is what made this field necessary --
    #: unstripping tags in mcp/compact.py does nothing here, because this
    #: model is `extra="forbid"` and drops anything with no field to land in.
    tags: tuple[str, ...] = ()
    #: Upstream's `final` ranking score: the value the hits are ordered by,
    #: blending reranker relevance with recency/temporal/proof boosts.
    #:
    #: Exposed so a caller can apply its own judgement instead of trusting
    #: the order alone. Hindsight sent this from the start and the same
    #: `extra="forbid"` above dropped it, so every hit arrived looking
    #: equally confident whether it scored 1.08 or 0.00001 -- both measured,
    #: in one response, on one query.
    #:
    #: NOT bounded to 0-1: `final` is a combined score and 1.0997 is a real
    #: observed value. And relative, not absolute -- the same fact can score
    #: orders of magnitude apart on two queries, so compare hits within one
    #: response and never against a constant remembered from another.
    #:
    #: None when upstream sent no scores for a hit, which its own contract
    #: allows (`scores` is nullable, and source facts carry none).
    score: float | None = None


class RecallResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_slug: str | None = None
    resolved_from: str | None = None
    hits: tuple[RecallHit, ...] = Field(default=(), max_length=MAX_HITS)
    truncated: bool = False


class SourceFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    text: str = Field(max_length=MAX_HIT_TEXT_LENGTH)
    origin: EvidenceBasis | None = None


class HistoryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_HIT_TEXT_LENGTH)
    # The oldest upstream revision has no predecessor timestamp.
    valid_from: str | None
    valid_to: str | None = None
    source_facts: tuple[SourceFact, ...] = Field(
        default=(), max_length=MAX_SOURCE_FACTS_PER_CHANGE
    )


class CurrentFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_HIT_TEXT_LENGTH)
    state: MemoryState
    kind: MemoryType | None = None


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_slug: str | None = None
    resolved_from: str | None = None
    memory_id: str
    current: CurrentFact
    changes: tuple[HistoryChange, ...] = Field(default=(), max_length=MAX_HISTORY_CHANGES)
    truncated: bool = False


def _fits(budget_left: int, candidate: BaseModel) -> int | None:
    """Byte cost of adding `candidate`, or None if it would not fit."""
    size = len(candidate.model_dump_json().encode("utf-8"))
    return size if size <= budget_left else None


def build_recall_response(
    *, project_slug: str | None, resolved_from: str | None, hits: list[RecallHit]
) -> RecallResponse:
    """Cap whole hits, never a partial one, against both the count cap and
    the total-byte cap; report truncation whenever either cap actually bound
    the result.

    Byte-costed by each hit's own serialized form, not the whole response at
    once: a response-level check would make the cost of hit N depend on every
    hit before it in a way a caller could not predict from `MAX_HIT_TEXT_LENGTH`
    alone, and would drop a normally-sized hit for the crime of following an
    oversized one instead of the oversized one being the one that doesn't fit.
    """
    kept: list[RecallHit] = []
    remaining = MAX_RECALL_RESPONSE_BYTES
    for hit in hits[:MAX_HITS]:
        cost = _fits(remaining, hit)
        if cost is None:
            break
        kept.append(hit)
        remaining -= cost
    truncated = len(kept) < len(hits)
    return RecallResponse(
        project_slug=project_slug,
        resolved_from=resolved_from,
        hits=tuple(kept),
        truncated=truncated,
    )


def build_history_response(
    *,
    project_slug: str | None,
    resolved_from: str | None,
    memory_id: str,
    current: CurrentFact,
    changes: list[HistoryChange],
) -> HistoryResponse:
    """Same discipline as `build_recall_response`, one level deeper: a
    change's own `source_facts` are already capped at construction
    (`HistoryChange.source_facts`'s own `max_length`), so only the list of
    changes itself needs capping here, against both count and total bytes."""
    kept: list[HistoryChange] = []
    remaining = MAX_HISTORY_RESPONSE_BYTES
    for change in changes[:MAX_HISTORY_CHANGES]:
        cost = _fits(remaining, change)
        if cost is None:
            break
        kept.append(change)
        remaining -= cost
    truncated = len(kept) < len(changes)
    return HistoryResponse(
        project_slug=project_slug,
        resolved_from=resolved_from,
        memory_id=memory_id,
        current=current,
        changes=tuple(kept),
        truncated=truncated,
    )


@dataclass(frozen=True)
class RecallFilters:
    """The server-owned Hindsight filter set a `view`/`memory_types`/
    `caller_tags` choice maps to. Never caller input directly: a
    `RecallRequest` cannot construct one of these itself, only name a `view`,
    `memory_types` and already-normalised `caller_tags` for `resolve_filters`
    to map."""

    types: tuple[FactType, ...]
    #: Upstream's compound tag filter. Top-level groups are ANDed together,
    #: which is the whole reason this is not a flat `tags`/`tags_match` pair:
    #: the server's scoping tags and the caller's narrowing tags need
    #: DIFFERENT match modes in the same query, and one flat list can only
    #: carry one mode for all of them.
    tag_groups: tuple[dict[str, object], ...]


def resolve_filters(
    view: View,
    memory_types: tuple[MemoryType, ...] | None,
    caller_tags: tuple[str, ...] = (),
) -> RecallFilters:
    """Map a caller's closed `view`/`memory_types` choice, plus its own
    already-normalised tags, to Hindsight's actual filter vocabulary.

    Every ACH-authored fact is scoped by the fixed `schema:ach-retain-v1`
    tag, optionally narrowed by the caller's closed `memory_types`, then
    further narrowed by `caller_tags` (`RecallRequest.tags_filter`, already
    validated by `memory.tags.normalize_caller_tags` -- this function never
    normalises or validates them itself, only appends). `view` does not
    currently branch this mapping; the documented views select the same
    exact retained corpus until a future contract gives them separate
    meaning.
    `ach-exact-v1` never produces an "experience" fact (SPEC §5.7), so only
    `world` (the retained claim) and `observation` (Hindsight's own later
    consolidation) are ever relevant types.

    Three ANDed groups, not one flat list, because each axis needs a
    DIFFERENT match mode and a flat list can only carry one. Sharing a single
    mode across all of them broke both server-owned narrowings, each silently:

    * ORing put `schema:ach-retain-v1` in with the caller's tags. Every
      ACH-authored memory carries that tag, so the filter matched the entire
      corpus -- a caller asking for LESS silently received EVERYTHING, with
      no error to notice.
    * ANDing joined the `type:` tags together, and a memory carries exactly
      one. Asking for two memory types could therefore never match anything.

    Grouped, each axis keeps the mode it actually needs: the schema tag is
    always required, the requested `memory_types` are ORed against each
    other, and the caller's own tags are ANDed. Every group is `_strict`; the
    loose Hindsight forms, which also return untagged memories, are not
    reachable from this surface at all.
    """
    groups: list[dict[str, object]] = [
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"}
    ]
    if memory_types:
        groups.append(
            {"tags": [f"type:{value}" for value in memory_types], "match": "any_strict"}
        )
    if caller_tags:
        groups.append({"tags": list(caller_tags), "match": "all_strict"})
    return RecallFilters(types=("world", "observation"), tag_groups=tuple(groups))
