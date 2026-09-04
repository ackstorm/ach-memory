"""Closed, bounded request/response contracts for the read-only recall and
history surfaces (`POST /v1/read/recall`, `POST /v1/read/history`).

Every field here is either a caller input with server-owned validation, or a
server-constructed output whitelisted field by field -- see the plan's
"Read-only public contracts" for the wire shapes these implement
(docs/superpowers/plans/2026-09-02-memory-quality-phase-5-read-only-recall.md).
No model here ever carries `git_locator`, a raw Hindsight tag/tag-group, a
bank ID or a tenant ID: the Phase 5 non-negotiable contracts forbid all four
on the read surface, and `extra="forbid"` on every request model turns a
caller who sends one into a 422, not a silently-ignored field.

This module also owns the one piece of caller-controllable Hindsight
behavior a read exposes: mapping `view`/`kinds` to fixed upstream filters
(`resolve_filters`). The caller chooses from closed enums; the actual
Hindsight `types`/`prefer_observations`/tag values are server-owned and never
themselves caller input.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memory.identifiers import has_control_character
from memory.memory_types import EvidenceBasis, MemoryType

ReadScope = Literal["user", "project"]

# Hindsight's own vocabulary, not a value this service computes. Duplicated
# here rather than shared, matching this codebase's existing precedent:
# curation.py, admin.py and mcp/tools.py already each define
# `Literal["world", "experience", "observation"]` independently rather than
# import one canonical copy.
FactType = Literal["world", "experience", "observation"]
MemoryState = Literal["valid", "invalidated"]
# The released response contains this nullable field. New v0.4 records do
# not produce an eligibility value; this remaining value only describes
# legacy evidence returned during the migration.
Eligibility = Literal["evidence_only"]

# The MCP compatibility surface still refers to the old annotation name.
# Read contracts themselves use MemoryType directly.
ProfileKind = MemoryType

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
    eligibility: Eligibility | None = None
    occurred_at: str | None = None
    document_id: str | None = None


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
    """The server-owned Hindsight filter set a `view`/`kinds` choice maps to.
    Never caller input: a `RecallRequest` cannot construct one of these
    directly, only name a `view` and `kinds` for `resolve_filters` to map."""

    types: tuple[FactType, ...]
    prefer_observations: bool
    eligibility_tags: tuple[str, ...]
    kind_tags: tuple[str, ...]


# view -> (types, prefer_observations, eligibility tags). Table, not a
# branching function: the whole point is that every value here is fixed at
# import time, so a new view can only ever be added by changing code, never
# by a request payload.
_VIEW_FILTERS: dict[View, tuple[tuple[FactType, ...], bool, tuple[str, ...]]] = {
    # Current: what memory asserts right now. Preferring observations over
    # their raw source facts is what makes this "current" rather than
    # "everything ever retained" -- an observation supersedes the world/
    # experience facts it was derived from.
    "current": (("observation", "world", "experience"), True, ()),
    # Evidence: the raw world/experience facts a claim rests on, not the
    # claim itself. `evidence_only` is the same closed eligibility tag the
    # explicit-retain surface writes (api/memory.py's
    # EXPLICIT_RETAIN_OBSERVATION_SCOPES); this is that tag read back.
    "evidence": (("world", "experience"), False, ("evidence_only",)),
    # All: still current/valid unless exact history is requested separately
    # through POST /v1/read/history -- "all" widens fact types and drops the
    # eligibility default, it does not reach into invalidated/superseded
    # history.
    "all": (("observation", "world", "experience"), False, ()),
}


def resolve_filters(view: View, kinds: tuple[MemoryType, ...] | None) -> RecallFilters:
    """Map a caller's closed `view`/`kinds` choice to Hindsight's actual
    filter vocabulary. The caller never controls Hindsight filter syntax or
    a temporal anchor -- only which of these three fixed rows applies, and
    which closed `type:<memory_type>` tags OR together on top of it."""
    types, prefer_observations, eligibility_tags = _VIEW_FILTERS[view]
    kind_tags = tuple(f"type:{kind}" for kind in kinds) if kinds else ()
    return RecallFilters(
        types=types,
        prefer_observations=prefer_observations,
        eligibility_tags=eligibility_tags,
        kind_tags=kind_tags,
    )
