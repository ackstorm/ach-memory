"""Phase 4 structured profile contracts (SPEC §4.2, §6.4, §10-§13, §17).

Scope of this module *so far* (Tasks 1, 3 and 4): the closed, bounded JSON
Schema/Pydantic contract a Hindsight mental model's structured output must
match, structural validation of documents that could come back, explicit
provisioning of the one target mental model per bank, and the pure
`compile_profile` pass that turns one already-fetched `reflect_response`
into the deterministically ranked, budget-truncated active profile. This
module still does not query Hindsight itself (`provision_profile` acts on a
client its caller supplies; `compile_profile` takes no client at all) and
touches no HTTP route. Later Phase 4 tasks extend this same file with brief
compilation (Task 5) and correction-refresh targeting (Task 6). Task 7 adds
the last section below: the pure, non-persisting evaluation of one upstream
dry-run-refresh preview that `ach-memory profile-check` reports.

Non-negotiable contracts this module enforces (plan "Structured contracts" /
"Non-negotiable contracts"):

- Every object level is closed (`extra="forbid"`); the model cannot invent a
  field or a category key outside the fixed set.
- `claim`/`failure`/`cause`/`reproduction`/`provenance` are bounded single
  lines -- no `importance`, `priority`, `bank`, `profile_eligible`, `score`,
  free-form metadata, Working State or Project Metadata field exists on any
  model here. Ordering is code-derived elsewhere (Task 4), never carried in
  this schema.
- The Phase 3 §6.4 durability matrix is enforced again at the profile
  boundary: within this schema's closed `kind`/`origin` enums, only observed
  `preference` and observed `decision` are excluded (`inferred` and
  `technical_claim` are already excluded by the enums themselves).
- A gotcha is deliverable only with a failure claim, cause or reproducible
  condition, and bounded provenance; every non-gotcha item carries none of
  those three optional fields.
"""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from memory.identifiers import has_control_character

# ---------------------------------------------------------------------------
# Budgets (plan "Non-negotiable contracts": at most 15/25 delivered items).
# Cross-category enforcement of these totals is Task 4's job; here each
# category field is capped at its profile's total budget so the upstream
# JSON Schema response is bounded even before normalization runs -- a
# weaker, per-category bound, not a promise that summing categories can't
# exceed the total.
# ---------------------------------------------------------------------------

USER_PROFILE_BUDGET = 15
PROJECT_PROFILE_BUDGET = 25


def _single_line(value: str) -> str:
    """Reject blank text and control characters. Control characters include
    `\\n`/`\\r`, so a value that passes this check cannot smuggle in a second,
    structurally-unvalidated line -- same technique and rationale as
    `memory.contracts.WorkingStateLine`."""
    if not value.strip() or has_control_character(value):
        raise ValueError("must be a single non-blank line with no control characters")
    return value


# `claim` is the one full-sentence field (max 320 chars); `failure`, `cause`,
# `reproduction` and `provenance` are the shorter explanatory lines the plan
# bounds at 240 chars each.
ProfileClaim = Annotated[str, Field(min_length=1, max_length=320), AfterValidator(_single_line)]
ProfileLine = Annotated[str, Field(min_length=1, max_length=240), AfterValidator(_single_line)]

# Evidence IDs are opaque Hindsight memory IDs (grounding them against
# `reflect_response.based_on.memories` is `compile_profile`'s job, below;
# nothing at the item boundary can know which IDs are real). No format is
# assumed beyond non-empty and defensively bounded; 200 chars comfortably
# covers any realistic Hindsight ID while still rejecting pathological
# payloads. Control characters are rejected for the same reason as any
# other single-line field.
EvidenceId = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_single_line)]
# A tuple, not a list: `frozen=True` on ProfileItem only blocks attribute
# *assignment* (`item.evidence_ids = ...`) -- a `list` field stays mutable
# in place (`item.evidence_ids.append(...)`), which would make "frozen"
# false advertising and the model unhashable. A tuple has no `.append` and
# is itself hashable, so both problems disappear together. Same JSON
# Schema shape as `list` (`type: array` with `minItems`/`maxItems`),
# verified empirically before making this change.
EvidenceIds = Annotated[tuple[EvidenceId, ...], Field(min_length=1, max_length=8)]

ProfileKind = Literal["preference", "decision", "convention", "gotcha"]
# Note: this enum deliberately excludes "inferred" -- an inferred claim
# never reaches a durable profile (SPEC §6.4), so it has no place in a
# schema Hindsight is asked to fill in as durable current truth.
ProfileOrigin = Literal["stated", "confirmed", "observed"]

UserCategory = Literal["interaction", "engineering", "preferences", "constraints"]
ProjectCategory = Literal[
    "architecture", "decisions", "workflow", "testing", "conventions", "gotchas"
]

# ---------------------------------------------------------------------------
# SPEC Phase 3 §6.4 durability matrix, transcribed cell by cell rather than
# derived from a rule (same rationale as
# `memory.capture.classifier._DURABILITY`): "profile-eligible unless
# observed preference/decision" agrees with the spec on 10 of these 12
# cells and is silently wrong on the other 2, so every cell this schema's
# closed kind/origin enums can produce is spelled out instead of inferred
# from a shortcut. `inferred` and `technical_claim` never reach this table
# at all -- the enums above already exclude them.
# ---------------------------------------------------------------------------

_PROFILE = "profile_eligible"
_EVIDENCE = "evidence_only"

_DURABILITY: dict[tuple[ProfileKind, ProfileOrigin], str] = {
    ("preference", "stated"): _PROFILE,
    ("preference", "confirmed"): _PROFILE,
    ("preference", "observed"): _EVIDENCE,
    ("decision", "stated"): _PROFILE,
    ("decision", "confirmed"): _PROFILE,
    ("decision", "observed"): _EVIDENCE,
    ("convention", "stated"): _PROFILE,
    ("convention", "confirmed"): _PROFILE,
    ("convention", "observed"): _PROFILE,
    ("gotcha", "stated"): _PROFILE,
    ("gotcha", "confirmed"): _PROFILE,
    ("gotcha", "observed"): _PROFILE,
}


class ProfileItem(BaseModel):
    """One bounded semantic claim -- the item type shared by every category
    in both the user and project response schemas (plan "Structured
    contracts"). Frozen and closed: nothing downstream can mutate a
    validated item, and Hindsight cannot smuggle a field this schema does
    not define (no `importance`, `priority`, `bank`, `profile_eligible`,
    `score`, or any Working State/Project Metadata field -- those are
    compiled separately by other code and are never accepted here)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: ProfileClaim
    kind: ProfileKind
    origin: ProfileOrigin
    # No default: the plan requires this flag to round-trip faithfully as an
    # explicit decision, not a silently-assumed False. `strict=True` so only
    # an actual JSON boolean validates -- "1"/"yes"/1 are coercion/mangling,
    # not preservation, of an explicit negative constraint.
    negative: bool = Field(strict=True)
    failure: ProfileLine | None = None
    cause: ProfileLine | None = None
    reproduction: ProfileLine | None = None
    provenance: ProfileLine | None = None
    evidence_ids: EvidenceIds

    @field_validator("evidence_ids")
    @classmethod
    def _unique_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_ids must not contain duplicate IDs")
        return value

    @model_validator(mode="after")
    def _durability(self) -> "ProfileItem":
        """SPEC §6.4 enforced again at the profile boundary: observed
        preference and observed decision are evidence, never profile
        truth, even though nothing else about their shape is invalid."""
        if _DURABILITY[(self.kind, self.origin)] != _PROFILE:
            raise ValueError(
                f"kind={self.kind!r} at origin={self.origin!r} is evidence-only "
                "per SPEC §6.4 and can never reach a profile"
            )
        return self

    @model_validator(mode="after")
    def _gotcha_shape(self) -> "ProfileItem":
        """A gotcha is deliverable only with a failure claim, cause or
        reproducible condition, and bounded provenance (plan "Non-negotiable
        contracts"). Every non-gotcha item carries none of those three
        optional fields -- one JSON item is one semantic claim, and a
        convention/decision/preference has no gotcha detail to attach."""
        if self.kind == "gotcha":
            if self.failure is None:
                raise ValueError("a gotcha requires a non-null failure")
            if self.cause is None and self.reproduction is None:
                raise ValueError("a gotcha requires cause or reproduction (or both)")
            if self.provenance is None:
                raise ValueError("a gotcha requires non-null provenance")
        elif self.failure is not None or self.cause is not None or self.reproduction is not None:
            raise ValueError(
                "failure, cause and reproduction must all be null for a non-gotcha item"
            )
        return self


# ---------------------------------------------------------------------------
# Category/kind compatibility (Task 1 Step 3's one open design judgment
# call -- see the Task 1 report for the full rationale).
#
# The category names are topical groupings, not a 1:1 relabeling of the 4
# `kind` values. The one unambiguous rule, driven directly by the plan, is
# that a `gotcha` may live ONLY in the project `gotchas` category: filing a
# gotcha under `architecture`/`workflow`/`testing` would defeat the purpose
# of a dedicated category, and neither user category set has a `gotchas`
# bucket at all, so `kind="gotcha"` is never valid anywhere in a user
# document.
#
# Beyond that unambiguous case, this module makes the following
# consistently-enforced choices:
#
# - `kind="preference"` is inherently personal and never appears in a
#   project document -- a project has conventions and decisions, not
#   "preferences". So the project category table below never lists
#   "preference" as allowed, which structurally excludes it from every
#   project category.
# - User `preferences` is the generic home for kind=preference items that
#   are NOT an explicit negative constraint (see the `constraints` rule
#   below); `interaction` and `engineering` are topical subsets that may
#   also hold preference-kind items (a stated interaction style, a stated
#   tooling preference), plus `engineering` may hold decision/convention
#   kinds for personal technical choices. `interaction` intentionally
#   excludes `decision` -- how a user likes to interact is not naturally
#   phrased as a decision.
# - User `constraints` is the single, exclusive home for every
#   `negative=true` item regardless of kind (preference/decision/
#   convention -- gotcha is excluded everywhere in the user document per
#   the rule above). Centralizing negative items in one category, rather
#   than letting `negative=true` also validate inside `preferences`/
#   `interaction`/`engineering`, removes an otherwise-ambiguous choice for
#   the synthesizing model about where a "never do X" claim belongs, and
#   gives category membership one deterministic answer per item. The
#   corollary: `preferences`/`interaction`/`engineering` accept only
#   `negative=false` items -- an explicit negative claim placed there is
#   rejected in favor of `constraints`.
# - Project `decisions` and `conventions` are the unambiguous 1:1 homes for
#   kind=decision and kind=convention respectively. `architecture`,
#   `workflow` and `testing` are topical categories that may also hold
#   decision/convention kinds (an architecture decision, a testing
#   convention) -- multiple valid homes for the same kind is intentional
#   per the plan ("categories ... may reasonably accept more than one
#   kind"); which topical bucket a given decision/convention lands in is a
#   synthesis-quality question for later tasks/prompting, not something
#   this structural layer disambiguates further.
# - Unlike the user document, no project category restricts on `negative`:
#   the project schema has no dedicated "constraints" bucket, so a negative
#   project convention/decision (e.g. "never run migrations directly
#   against prod") stays wherever it topically belongs.
# ---------------------------------------------------------------------------

_USER_CATEGORY_KINDS: dict[UserCategory, frozenset[ProfileKind]] = {
    "interaction": frozenset({"preference", "convention"}),
    "engineering": frozenset({"preference", "decision", "convention"}),
    "preferences": frozenset({"preference"}),
    "constraints": frozenset({"preference", "decision", "convention"}),
}

_PROJECT_CATEGORY_KINDS: dict[ProjectCategory, frozenset[ProfileKind]] = {
    "architecture": frozenset({"decision", "convention"}),
    "decisions": frozenset({"decision"}),
    "workflow": frozenset({"decision", "convention"}),
    "testing": frozenset({"decision", "convention"}),
    "conventions": frozenset({"convention"}),
    "gotchas": frozenset({"gotcha"}),
}


def _check_user_category(category: UserCategory, item: ProfileItem) -> None:
    allowed = _USER_CATEGORY_KINDS[category]
    if item.kind not in allowed:
        raise ValueError(f"user category {category!r} cannot hold kind={item.kind!r}")
    if category == "constraints":
        if not item.negative:
            raise ValueError("user category 'constraints' holds only negative=true items")
    elif item.negative:
        raise ValueError(f"a negative=true item belongs in 'constraints', not {category!r}")


def _check_project_category(category: ProjectCategory, item: ProfileItem) -> None:
    allowed = _PROJECT_CATEGORY_KINDS[category]
    if item.kind not in allowed:
        raise ValueError(f"project category {category!r} cannot hold kind={item.kind!r}")


def _normalized_claim(claim: str) -> str:
    """Whitespace-collapsed, case-folded claim text used only to detect
    exact duplicates (see `_reject_duplicates`); this is not the claim
    normalization/rephrasing Task 4 performs on delivered items."""
    return " ".join(claim.split()).casefold()


def _reject_duplicates(items: list[tuple[str, ProfileItem]]) -> None:
    """Reject exact duplicate items within one response: same normalized
    claim + category + negative flag appearing twice (plan Task 1 Step 3).
    Deliberately a document-level check (it needs every category's items
    at once) rather than a per-item validator. `kind` is intentionally NOT
    part of the key -- the plan's duplicate key is claim + category +
    negative, so the same claim text filed under the same category with
    two different kinds is still an exact duplicate by this definition."""
    seen: set[tuple[str, str, bool]] = set()
    for category, item in items:
        key = (category, _normalized_claim(item.claim), item.negative)
        if key in seen:
            raise ValueError(
                f"duplicate item in category {category!r}: claim {item.claim!r} "
                "already present with the same category and negative flag"
            )
        seen.add(key)


# Tuples, not lists -- same reasoning as `EvidenceIds` above: a `list`
# field on a `frozen=True` model stays mutable in place, which would let a
# caller push a bare, unvalidated item into `doc.preferences.append(...)`
# after construction. Same JSON Schema shape as `list[ProfileItem]`
# (`type: array` with `maxItems`, items `$ref`ing `ProfileItem`), verified
# empirically before making this change.
_UserCategoryField = Annotated[tuple[ProfileItem, ...], Field(max_length=USER_PROFILE_BUDGET)]
_ProjectCategoryField = Annotated[tuple[ProfileItem, ...], Field(max_length=PROJECT_PROFILE_BUDGET)]


class UserProfileDocument(BaseModel):
    """The `user_profile` response document (plan "Structured contracts").
    Category membership is structural: `extra="forbid"` means the model
    cannot invent a category key outside this fixed set of four, and each
    category is capped at the user profile's total budget of 15.

    JSON Schema alone can `$ref` the same closed `ProfileItem` shape from
    every category, but it cannot express which `kind` values or which
    `negative` value belong in which category -- that compatibility rule
    is enforced by `_validate_categories` below, not by the schema. Each
    field's `description=` states its own rule so a synthesizing model has
    a chance to get it right the first time, instead of only discovering
    the rule for the first time through a validation failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interaction: _UserCategoryField = Field(
        default_factory=tuple,
        description=(
            "Personal interaction/communication-style claims. kind must be "
            "preference or convention. negative=true items belong in "
            "constraints instead, never here."
        ),
    )
    engineering: _UserCategoryField = Field(
        default_factory=tuple,
        description=(
            "Personal technical/tooling claims. kind must be preference, "
            "decision or convention. negative=true items belong in "
            "constraints instead, never here."
        ),
    )
    preferences: _UserCategoryField = Field(
        default_factory=tuple,
        description=(
            "Personal preferences. kind must be preference. negative=true "
            "items belong in constraints instead, never here."
        ),
    )
    constraints: _UserCategoryField = Field(
        default_factory=tuple,
        description=(
            "Explicit negative constraints. The ONLY category whose items "
            "must have negative=true; kind must be preference, decision or "
            "convention (never gotcha)."
        ),
    )

    @model_validator(mode="after")
    def _validate_categories(self) -> "UserProfileDocument":
        items = (
            [("interaction", i) for i in self.interaction]
            + [("engineering", i) for i in self.engineering]
            + [("preferences", i) for i in self.preferences]
            + [("constraints", i) for i in self.constraints]
        )
        for category, item in items:
            _check_user_category(category, item)
        _reject_duplicates(items)
        return self


class ProjectProfileDocument(BaseModel):
    """The `project_profile` response document (plan "Structured
    contracts"). Category membership is structural: `extra="forbid"` means
    the model cannot invent a category key outside this fixed set of six,
    and each category is capped at the project profile's total budget of
    25.

    As with `UserProfileDocument`, the schema cannot express category/kind
    compatibility on its own -- see each field's `description=` and
    `_validate_categories` below."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    architecture: _ProjectCategoryField = Field(
        default_factory=tuple,
        description=(
            "Structural claims. kind must be decision or convention -- "
            "never preference (projects have no preferences) and never "
            "gotcha (gotcha belongs only in the gotchas category)."
        ),
    )
    decisions: _ProjectCategoryField = Field(
        default_factory=tuple,
        description="kind must be decision. This is the only category for decisions.",
    )
    workflow: _ProjectCategoryField = Field(
        default_factory=tuple,
        description=(
            "Process claims. kind must be decision or convention -- never "
            "preference (projects have no preferences) and never gotcha "
            "(gotcha belongs only in the gotchas category)."
        ),
    )
    testing: _ProjectCategoryField = Field(
        default_factory=tuple,
        description=(
            "Testing claims. kind must be decision or convention -- never "
            "preference (projects have no preferences) and never gotcha "
            "(gotcha belongs only in the gotchas category)."
        ),
    )
    conventions: _ProjectCategoryField = Field(
        default_factory=tuple,
        description="kind must be convention. This is the only category for conventions.",
    )
    gotchas: _ProjectCategoryField = Field(
        default_factory=tuple,
        description=(
            "kind must be gotcha. This is the ONLY category that may hold a "
            "gotcha -- a gotcha filed under any other category is invalid."
        ),
    )

    @model_validator(mode="after")
    def _validate_categories(self) -> "ProjectProfileDocument":
        items = (
            [("architecture", i) for i in self.architecture]
            + [("decisions", i) for i in self.decisions]
            + [("workflow", i) for i in self.workflow]
            + [("testing", i) for i in self.testing]
            + [("conventions", i) for i in self.conventions]
            + [("gotchas", i) for i in self.gotchas]
        )
        for category, item in items:
            _check_project_category(category, item)
        _reject_duplicates(items)
        return self


class _UserProfileResponse(BaseModel):
    """Wrapper that keeps the `user_profile` root key in the generated JSON
    Schema. `UserProfileDocument.model_json_schema()` alone describes the
    bare category object, not `{"user_profile": {...}}` -- the shape the
    plan's YAML target and Hindsight's `response_schema` both expect."""

    model_config = ConfigDict(extra="forbid", title="UserProfileResponse")

    user_profile: UserProfileDocument


class _ProjectProfileResponse(BaseModel):
    """Wrapper that keeps the `project_profile` root key in the generated
    JSON Schema -- see `_UserProfileResponse`."""

    model_config = ConfigDict(extra="forbid", title="ProjectProfileResponse")

    project_profile: ProjectProfileDocument


def user_response_schema() -> dict[str, Any]:
    """JSON Schema for the user-scope `ach-memory-profile-v1` structured
    response, keyed as `{"user_profile": {...}}`. Suitable for handing to
    Hindsight as a mental model's `response_schema` (wiring that call is
    Task 2/3's job, not this module's)."""
    return _UserProfileResponse.model_json_schema()


def project_response_schema() -> dict[str, Any]:
    """JSON Schema for the project-scope `ach-memory-profile-v1` structured
    response, keyed as `{"project_profile": {...}}`. See
    `user_response_schema`."""
    return _ProjectProfileResponse.model_json_schema()


# ---------------------------------------------------------------------------
# Provisioning (Task 3): one `ach-memory-profile-v1` mental model per bank,
# created and reconciled only through `POST /v1/admin/profile/{scope}/
# provision` in `api/admin.py` -- see `brief.py`'s module docstring for why
# creation never lives on a read path; this module has no read path for the
# structured profile at all yet (that is Task 4's job -- parsing
# `reflect_response.structured_output`, eligibility, ranking, displacement).
# Task 3 only gets a model with the right query, budget and trigger into
# existence, and keeps it that way across deploys.
# ---------------------------------------------------------------------------

ProfileScope = Literal["user", "project"]

PROFILE_MODEL_NAME = "ach-memory-profile-v1"

# Proportional to each scope's item budget (USER_PROFILE_BUDGET=15,
# PROJECT_PROFILE_BUDGET=25 above) and bounded by Hindsight's own
# Field(ge=256, le=8192) (api/mental_models.py). A structured item costs
# more tokens than brief.py's free-text line: every item pays JSON-object
# overhead (~40 tokens for field names/braces) on top of `claim` (<=320
# chars, ~80 tokens) and `evidence_ids` (up to 8 ids, ~60 tokens) --
# roughly 200 tokens for a plain item. A `gotcha` item (project-only) also
# pays for `failure`/`cause`/`reproduction`/`provenance` (<=240 chars each,
# ~60 tokens apiece), worst case ~450 tokens. 15 user items, none of them
# gotchas, come to ~3000 tokens plus wrapper/category-key overhead; 25
# project items with some share of gotchas come to ~5000-6500. Both rounded
# up for headroom, both comfortably inside the upstream ceiling.
#
# Unmeasured against a live Hindsight instance -- none is available in this
# phase (same situation as Tasks 1/2). Task 7's nightly evaluator is where
# these get corrected against real synthesis output, not guessed here.
USER_PROFILE_MAX_TOKENS = 3200
PROJECT_PROFILE_MAX_TOKENS = 6400

_MAX_TOKENS: dict[ProfileScope, int] = {
    "user": USER_PROFILE_MAX_TOKENS,
    "project": PROJECT_PROFILE_MAX_TOKENS,
}

# Mission text for the structured synthesis, in brief.py's imperative,
# economical style. Unlike brief.USER_QUERY/PROJECT_QUERY -- tuned against
# real memories over months of live use, per that module's own comment --
# these are drafted from the plan's explicit textual requirements alone:
# nothing in this environment can run them against a live Hindsight instance
# to measure what they actually produce. Task 7's nightly evaluator is where
# empirical refinement belongs; treat these as a first, defensible draft,
# not a tuned prompt.
#
# Named distinctly from brief.USER_QUERY/PROJECT_QUERY (not reused, not
# shadowed) -- this module accumulates prompts for several profile-related
# tasks across Phase 4, so the PROFILE_ prefix keeps a later grep for
# "USER_QUERY" from returning two unrelated missions.
PROFILE_USER_QUERY = (
    "Synthesize this user's current standing profile -- what is true now, "
    "not a history of how understanding changed over time. An explicit "
    "correction supersedes what it corrects. Draw every item only from "
    "evidence marked stated, confirmed or observed; an observed preference "
    "or observed decision is never eligible for a profile item, but an "
    "observed convention is. "
    "Never synthesize from inferred evidence or from a bare technical_claim. "
    "Each item is exactly one atomic claim -- never combine two independent "
    "statements into one item, even when the same memory states both. "
    "Every item must cite the specific evidence memory IDs (1 to 8) it is "
    "drawn from; never state a claim its cited evidence does not support. "
    "Place every item in exactly one of the four fixed categories "
    "(interaction, engineering, preferences, constraints); never invent a "
    "category. State only what the memories say."
)
PROFILE_PROJECT_QUERY = (
    "Synthesize this project's current standing profile -- what is true "
    "now, not a history of how understanding changed over time. An "
    "explicit correction supersedes what it corrects. Draw every item only "
    "from evidence marked stated, confirmed or observed; an observed "
    "decision is never eligible for a profile item, but an observed "
    "convention or gotcha is. Never synthesize from inferred evidence or "
    "from a bare technical_claim. Each item is exactly one atomic claim -- "
    "never combine two independent statements into one item, even when the "
    "same memory states both. A gotcha item must state the failure and at "
    "least its cause or how to reproduce it, plus provenance -- never a "
    "bare warning with no detail. Every item must cite the specific "
    "evidence memory IDs (1 to 8) it is drawn from; never state a claim its "
    "cited evidence does not support. Write every claim as how this project "
    "works, never as this person's preference -- personal preference "
    "belongs only in the user profile, even for a fact that could be "
    "phrased either way. Do not describe what the repository's files would "
    "already show. Place every item in exactly one of the six fixed "
    "categories (architecture, decisions, workflow, testing, conventions, "
    "gotchas); never invent a category."
)

_SOURCE_QUERY: dict[ProfileScope, str] = {
    "user": PROFILE_USER_QUERY,
    "project": PROFILE_PROJECT_QUERY,
}


def _schema_for(scope: ProfileScope) -> dict[str, Any]:
    return user_response_schema() if scope == "user" else project_response_schema()


def _profile_trigger(scope: ProfileScope) -> dict[str, Any]:
    """The Phase 4 trigger for `ach-memory-profile-v1`: `full` mode, no
    cron, no automatic post-consolidation refresh (plan "Hindsight target
    and safe rollout") -- Phase 4 provisions this model, it never refreshes
    it on its own. `response_schema` is the scope's own canonical schema, so
    Hindsight's structured output round-trips into
    `UserProfileDocument`/`ProjectProfileDocument`. `keep_trace` is kept for
    the same diagnostic reason as `brief.TRIGGER`'s: the only way to see why
    a refresh (however it gets triggered) did what it did, after the fact.

    This is NOT the eventual Phase 0 trigger -- see `desired_phase0_trigger`
    for the observation-only, exact-tag trigger that stays represented but
    unapplied until a separately authorized rollout step.
    """
    return {
        "mode": "full",
        "refresh_after_consolidation": False,
        "response_schema": _schema_for(scope),
        "keep_trace": True,
    }


def _find_profile(client, bank_id: str) -> dict | None:
    """Same list-and-match shape as `brief._find`."""
    listed = client.list_mental_models(bank_id, detail="full")
    models = listed.get("mental_models") or listed.get("items") or []
    for model in models:
        if model.get("name") == PROFILE_MODEL_NAME:
            return model
    return None


def _reconcile_profile(
    client,
    bank_id: str,
    model: dict,
    source_query: str,
    max_tokens: int,
    trigger: dict[str, Any],
) -> None:
    """Bring an existing profile model back in line with the constants
    above. Same merge-not-replace reasoning as `brief._reconcile`: the
    trigger is merged onto whatever Hindsight already stores there and
    compared only on the keys this module sets, so a field Hindsight adds on
    its own side survives untouched.

    `response_schema` is one of those compared keys, unlike anything
    `brief._reconcile` ever had to handle: a deploy that changes
    `user_response_schema()`/`project_response_schema()`, or a model that
    somehow ended up holding the OTHER scope's schema, is corrected the same
    way a changed `mode` or `keep_trace` would be. Compared by full dict
    equality, not presence -- two schemas can share every top-level key and
    still differ underneath.

    `max_tokens` is reconciled too, for `brief._reconcile`'s own recorded
    reason (see that docstring): "Only the query was reconciled here at
    first, which meant a changed TRIGGER silently applied to new banks
    alone" -- the same failure mode, here, for a budget already flagged as
    an unmeasured first-pass draft that is expected to change once Task 7's
    evaluator has real output to correct it against.
    """
    changed: dict[str, object] = {}
    if model.get("source_query") != source_query:
        changed["source_query"] = source_query

    if model.get("max_tokens") != max_tokens:
        changed["max_tokens"] = max_tokens

    stored = model.get("trigger") or {}
    if any(stored.get(key) != value for key, value in trigger.items()):
        changed["trigger"] = {**stored, **trigger}

    if changed:
        client.update_mental_model(bank_id, model["id"], **changed)


def provision_profile(client, bank_id: str, scope: ProfileScope) -> str:
    """Create the bank's `ach-memory-profile-v1` model, or bring an existing
    one back in line.

    Reached only through `POST /v1/admin/profile/{scope}/provision` -- Task
    3 wires no read path to this at all, so unlike `brief.provision_section`
    there is no sibling read function here yet whose docstring needs to
    explain why it never calls this.

    `tags=[]` is sent explicitly on create, unlike `brief.provision_section`
    (which never sends `tags`) -- Task 2 built exactly this capability: an
    omitted `tags` kwarg sends no key at all, while an explicit `[]` sends
    one. This matches the plan's literal Phase 4 target, `tags: []` at the
    top level of the create body; no final tag filter is installed until
    Phase 0 (see `desired_phase0_trigger`).

    Returns "created" or "reconciled".
    """
    source_query = _SOURCE_QUERY[scope]
    max_tokens = _MAX_TOKENS[scope]
    trigger = _profile_trigger(scope)

    model = _find_profile(client, bank_id)
    if model is None:
        client.create_mental_model(
            bank_id,
            name=PROFILE_MODEL_NAME,
            source_query=source_query,
            max_tokens=max_tokens,
            trigger=trigger,
            tags=[],
        )
        return "created"

    _reconcile_profile(client, bank_id, model, source_query, max_tokens, trigger)
    return "reconciled"


def desired_phase0_trigger(scope: ProfileScope) -> dict[str, Any]:
    """The trigger Phase 0 will eventually install: `delta` mode over
    observation-only facts tagged `profile_eligible` with exact tag
    matching -- what is meant to make correction refresh and continuous
    synthesis safe to turn on (plan "Hindsight target and safe rollout").

    Represented here so its shape is pinned in code and tested. Pure: no
    client argument, no side effects, nothing upstream is called. Not
    applied by anything -- `provision_profile` builds and sends only
    `_profile_trigger`'s `full`, untagged trigger, and nothing in this
    module or any application code path calls this function; only its own
    tests do. Wiring this trigger up is a separately authorized later
    rollout step (plan "Rollout order"), never a side effect of adding the
    builder that describes it.
    """
    return {
        "mode": "delta",
        "fact_types": ["observation"],
        "tags": ["profile_eligible"],
        "tags_match": "exact",
        "refresh_after_consolidation": False,
        "response_schema": _schema_for(scope),
        "keep_trace": True,
    }


# ---------------------------------------------------------------------------
# Normalization, ranking and displacement (Task 4; plan "Eligibility,
# ordering and displacement").
#
# `compile_profile` is the whole read path from one already-fetched
# `reflect_response` to the active profile. Three properties shape every
# decision below.
#
# 1. It works on RAW, UNTYPED dicts, not on `UserProfileDocument` /
#    `ProjectProfileDocument`. Those models are all-or-nothing by design: a
#    single bad item anywhere raises and takes every other item with it.
#    That is right when validating a document we are about to send or that
#    must be internally self-consistent as a whole, and wrong here, where
#    the plan's eligibility table ends in "everything else -> reject from
#    active profile" -- one item, not the response. So each raw item is
#    validated on its own inside a try/except, which reuses every
#    `ProfileItem` validator (durability matrix, gotcha shape, unique
#    evidence IDs, bounded single lines, `extra="forbid"`) at exactly the
#    per-item granularity the plan asks for. `_check_user_category` /
#    `_check_project_category` are then applied per item for the same
#    reason. `_reject_duplicates` is deliberately not used: duplicates here
#    must MERGE, and it can only reject.
#
# 2. It is pure. No Hindsight client, no I/O, no persisted state between
#    calls: "reads never write". The whole profile is recomputed from one
#    snapshot every time, which is also what "profiles represent current
#    truth" means -- displacement is simply the sorted prefix, not a
#    stateful eviction protocol.
#
# 3. Nothing the model supplies decides delivery. Support is counted from
#    grounded evidence IDs this module intersects itself; order comes from
#    `kind_rank`/`support_count`/`claim_key` only. Array position,
#    timestamps and any count-shaped field are never read (a count-shaped
#    key cannot even survive `extra="forbid"`).
# ---------------------------------------------------------------------------


_ROOT_KEY: dict[ProfileScope, str] = {
    "user": "user_profile",
    "project": "project_profile",
}

# The categories each scope actually owns. Iterating this fixed tuple --
# rather than whatever keys the response happens to carry -- means an
# invented category contributes nothing instead of being trusted.
_SCOPE_CATEGORIES: dict[ProfileScope, tuple[str, ...]] = {
    "user": tuple(_USER_CATEGORY_KINDS),
    "project": tuple(_PROJECT_CATEGORY_KINDS),
}

_SCOPE_BUDGET: dict[ProfileScope, int] = {
    "user": USER_PROFILE_BUDGET,
    "project": PROJECT_PROFILE_BUDGET,
}

_CATEGORY_CHECK: dict[ProfileScope, Callable[[Any, ProfileItem], None]] = {
    "user": _check_user_category,
    "project": _check_project_category,
}

# The plan's ranking table, spelled out cell by cell for the same reason
# `_DURABILITY` is: the rule is short enough to state exactly, and a
# derived shortcut would hide the one part that is easy to get wrong --
# `negative=True` pulls ANY kind up to tier 0, because an explicit negative
# constraint is the same class of "prevents a mistake" material as a
# gotcha. Every (kind, negative) pair in the closed enum appears here, so a
# lookup can never fall through.
#
#   0  valid gotcha or explicit negative constraint
#   1  decision
#   2  convention
#   3  stated/confirmed preference
_KIND_RANK: dict[tuple[ProfileKind, bool], int] = {
    ("gotcha", False): 0,
    ("gotcha", True): 0,
    ("decision", True): 0,
    ("convention", True): 0,
    ("preference", True): 0,
    ("decision", False): 1,
    ("convention", False): 2,
    ("preference", False): 3,
}

# Bumped whenever a change here could alter what a `CompiledProfileItem`
# means without changing a single one of its field values -- a new closed
# enum member, a new field, a changed durability/category rule. Brief
# fingerprints (src/memory/brief.py `_content_fingerprint`) mix this in
# alongside `PROFILE_RENDER_VERSION` so a schema change invalidates cached
# revisions even when no already-delivered item's compiled fields moved.
PROFILE_SCHEMA_VERSION = "profile-schema-v1"


@dataclass(frozen=True)
class CompiledProfileItem:
    """One delivered item plus the compiler metadata the plan requires.

    Deliberately a plain frozen dataclass and not a Pydantic model: every
    value here is computed by this module from an already-validated
    `ProfileItem`, so there is nothing left to validate and nothing that is
    ever serialized back to Hindsight.

    The wrapper's own `claim`, `evidence_ids` and `support_count` are the
    authoritative compiled values, and are the ones rendering and
    fingerprinting must use.

    `representative` is deliberately NOT named `item`: it is not "the
    thing", it is the single group member that was chosen to supply the
    typed fields the wrapper does not carry (`kind`, `origin`, `negative`,
    gotcha `failure`/`cause`/`reproduction`, `provenance`). Two of its
    fields are shadowed by authoritative wrapper fields and must not be
    read off it:

    - `representative.claim` is that one member's raw text, before
      whitespace normalization and before a merge chose between two
      spellings of the same claim -- use `claim`;
    - `representative.evidence_ids` is that one member's raw, ungrounded
      list, neither filtered against `based_on` nor unioned across the
      merge group -- use `evidence_ids`, and `support_count` for its size.
    """

    category: str
    # Whitespace-collapsed, case-preserving claim text: the plan's
    # "normalize whitespace without rephrasing". Two upstream spellings of
    # one claim merge into a single item, so the delivered text must not
    # depend on which spelling won.
    claim: str
    claim_key: str
    kind_rank: int
    support_count: int
    evidence_ids: tuple[str, ...]
    representative: ProfileItem


def _collapse_whitespace(value: str) -> str:
    """Collapse runs of whitespace without touching anything else. Case and
    wording are preserved: normalization here never rephrases."""
    return " ".join(value.split())


def _based_on_memory_ids(reflect_response: Any) -> frozenset[str]:
    """The set of Hindsight memory IDs this reflection was actually built
    from, read defensively out of `reflect_response.based_on.memories`.

    No live Hindsight instance is available to pin the exact shape, so both
    shapes seen in this repository's fixtures are accepted: a bare ID
    string, or an object carrying an `id` string (the shape every other
    Hindsight list endpoint uses). Anything else contributes nothing.

    A missing or malformed block yields an empty set, which is the correct
    fail-closed outcome: with nothing to ground against, every item loses
    all of its evidence and the section degrades to empty rather than
    delivering ungrounded claims.
    """
    if not isinstance(reflect_response, dict):
        return frozenset()
    based_on = reflect_response.get("based_on")
    if not isinstance(based_on, dict):
        return frozenset()
    memories = based_on.get("memories")
    if not isinstance(memories, list):
        return frozenset()

    memory_ids: set[str] = set()
    for memory in memories:
        if isinstance(memory, str):
            candidate: Any = memory
        elif isinstance(memory, dict):
            candidate = memory.get("id")
        else:
            continue
        if isinstance(candidate, str) and candidate:
            memory_ids.add(candidate)
    return frozenset(memory_ids)


def _structured_document(scope: ProfileScope, reflect_response: Any) -> dict[str, Any]:
    """The scope's profile document out of `reflect_response.structured_output`,
    or `{}` when anything on the way there is missing or the wrong type.

    The other scope's root key is never read: a project document handed to
    a user compile is not partially salvaged, it is empty.
    """
    if not isinstance(reflect_response, dict):
        return {}
    structured_output = reflect_response.get("structured_output")
    if not isinstance(structured_output, dict):
        return {}
    document = structured_output.get(_ROOT_KEY[scope])
    if not isinstance(document, dict):
        return {}
    return document


def _claim_key(category: str, item: ProfileItem) -> str:
    """`sha256(normalized category + claim + negative flag)`.

    The three components are JSON-encoded as a list before hashing so the
    encoding is unambiguous: JSON quotes and escapes each string, so no
    claim text can impersonate a category boundary the way a bare
    concatenation would allow (`"ab" + "c"` vs `"a" + "bc"`).

    `_normalized_claim` is reused verbatim rather than reimplemented, so
    the identity this key expresses is exactly the identity Task 1's
    `_reject_duplicates` already uses. `kind` and `origin` are excluded for
    the same reason they are excluded there: one claim restated as a
    different kind is still one claim.
    """
    payload = json.dumps([category, _normalized_claim(item.claim), item.negative])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _item_fingerprint(item: ProfileItem) -> str:
    """A content-only digest of a whole item, used solely to break merge
    ties deterministically. It depends on the item's fields and nothing
    else -- never on array position, arrival order or a timestamp."""
    payload = json.dumps(item.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compile_item(
    scope: ProfileScope,
    category: str,
    raw_item: Any,
    grounded: frozenset[str],
) -> CompiledProfileItem | None:
    """One raw upstream item, or `None` if it may not reach the profile.

    Every rejection here is scoped to this item alone; the caller keeps
    going. The four gates, in order: structural/durability validity,
    category compatibility, grounded evidence, and non-empty support.
    """
    try:
        item = ProfileItem.model_validate(raw_item)
    except ValidationError:
        return None

    try:
        _CATEGORY_CHECK[scope](category, item)
    except ValueError:
        return None

    # Intersecting with the grounded set both rejects references outside
    # `based_on` and deduplicates, before support is counted. An item left
    # with no grounded reference has no support at all and must not reach
    # the active profile: delivering it would break the invariant that
    # every delivered claim is backed by evidence this reflection actually
    # saw.
    evidence_ids = tuple(sorted(grounded.intersection(item.evidence_ids)))
    if not evidence_ids:
        return None

    return CompiledProfileItem(
        category=category,
        claim=_collapse_whitespace(item.claim),
        claim_key=_claim_key(category, item),
        kind_rank=_KIND_RANK[(item.kind, item.negative)],
        support_count=len(evidence_ids),
        evidence_ids=evidence_ids,
        representative=item,
    )


def _merge_duplicates(candidates: list[CompiledProfileItem]) -> list[CompiledProfileItem]:
    """Collapse equal `claim_key`s into one item holding the union of every
    member's valid evidence references.

    Support is the size of that union, never the sum of the members'
    counts: an evidence ID cited by two restatements of one claim is one
    piece of support, not two.

    `claim_key` covers category, normalized claim and the negative flag
    only, so members of a group can still differ in `kind`, `origin` and
    gotcha detail. The representative that supplies those fields is chosen
    by lowest `kind_rank` first -- the most risk-forward member wins, so
    merging a gotcha with a bare convention keeps the failure/cause/
    provenance detail rather than discarding it -- and ties are broken by
    ascending `_item_fingerprint`, a function of content alone. Nothing
    positional is consulted, so a permuted upstream array produces the same
    representative.
    """
    groups: dict[str, list[CompiledProfileItem]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.claim_key, []).append(candidate)

    # Group iteration order is insertion order, but it cannot leak into the
    # result: `claim_key` is unique after merging, so the final sort is a
    # total order.
    merged: list[CompiledProfileItem] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        representative = min(
            group, key=lambda entry: (entry.kind_rank, _item_fingerprint(entry.representative))
        )
        evidence_ids = tuple(sorted({ref for entry in group for ref in entry.evidence_ids}))
        merged.append(
            replace(representative, evidence_ids=evidence_ids, support_count=len(evidence_ids))
        )
    return merged


def _profile_candidates(
    scope: ProfileScope, reflect_response: Any
) -> list[CompiledProfileItem]:
    """Every raw item that survives eligibility, in upstream order.

    Split out of `compile_profile` (unchanged logic) so Task 7's evaluator
    can see this intermediate: how many items were offered, how many were
    eligible and grounded, and -- with `_rank_candidates` below -- how many
    were then merged away or displaced by the budget. `compile_profile`
    itself only ever returns the final prefix, which cannot answer any of
    those questions.
    """
    grounded = _based_on_memory_ids(reflect_response)
    document = _structured_document(scope, reflect_response)

    candidates: list[CompiledProfileItem] = []
    for category in _SCOPE_CATEGORIES[scope]:
        raw_items = document.get(category)
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            compiled = _compile_item(scope, category, raw_item, grounded)
            if compiled is not None:
                candidates.append(compiled)
    return candidates


def _rank_candidates(candidates: list[CompiledProfileItem]) -> list[CompiledProfileItem]:
    """Merged duplicates in the plan's total order, before truncation. See
    `_profile_candidates` for why this is a named step."""
    return sorted(
        _merge_duplicates(candidates),
        key=lambda entry: (entry.kind_rank, -entry.support_count, entry.claim_key),
    )


def compile_profile(
    scope: ProfileScope, reflect_response: Any
) -> tuple[CompiledProfileItem, ...]:
    """The active profile for `scope`, compiled from one `reflect_response`.

    Takes the response already fetched by the caller (Task 5 reads it off a
    `detail=full` mental model); this function performs no I/O and mutates
    nothing. Returns items sorted by ascending `kind_rank`, descending
    `support_count`, then ascending `claim_key`, truncated to the scope's
    15/25 budget -- the sorted prefix *is* the active profile, so a
    higher-ranked item enters exactly by displacing the current last one.

    Anything invalid fails closed at the smallest possible granularity: a
    bad item drops alone, a bad category drops alone, and an unusable
    response yields an empty profile.
    """
    ranked = _rank_candidates(_profile_candidates(scope, reflect_response))
    return tuple(ranked[: _SCOPE_BUDGET[scope]])


# ---------------------------------------------------------------------------
# Non-persisting evaluation (Task 7; plan "Hindsight target and safe
# rollout": run the profile evaluator nightly for at least seven
# representative runs per scope and record token/duration/quality ceilings
# before enabling structured delivery anywhere).
#
# This half of the module reads one upstream `dry-run-refresh` response --
# Hindsight's own non-persisting preview of what a refresh WOULD produce --
# and reduces it to numbers. It writes nothing, upstream or locally, and
# like the rest of this module performs no I/O of its own: the caller
# (`ach-memory profile-check`) makes the two read-shaped calls and hands the
# response here.
#
# Everything that leaves this section is content-free, per the plan's
# non-negotiable contract that "metrics, evaluator JSON and logs contain
# counts, timings, modes and error codes only": no claim text, no evidence
# or memory IDs, no bank id, user id or project slug. The preview document
# exists transiently inside `evaluate_dry_run` and never reaches the result.
# ---------------------------------------------------------------------------

DeliveryMode = Literal["legacy", "structured"]

EvaluationOutcome = Literal[
    # Measured, and nothing to report against it.
    "ok",
    # The bank has no `ach-memory-profile-v1` to preview: nothing to measure
    # and, under structured delivery, nothing that would be served either.
    "no_model",
    # `preview_content` was not JSON, not this scope's document, or did not
    # satisfy the response schema the model was provisioned with.
    "schema_invalid",
    # The preview cost more output tokens than the scope's provisioned
    # `max_tokens` ceiling. Item-count conformance is not an outcome: the
    # compiler truncates to the 15/25 budget by construction, so it cannot
    # be exceeded -- `displacement_count` reports how hard it had to cut.
    "budget_exceeded",
    # A valid document that delivers no items at all. Under structured
    # delivery this bank would serve an empty profile section, so a run that
    # is meant to represent real traffic reports it as a failure rather than
    # as a pass with a zero in it.
    "empty_profile",
    # Hindsight refused one of the two read calls. A finding about the run,
    # not about the profile.
    "upstream_error",
]

# Non-fatal observations, as a closed set of codes. Never a message: a
# formatted string is how claim text and identifiers escape into logs.
WARNING_CODES = frozenset(
    {
        # The dry-run response carried no usable `based_on` block, so
        # grounding could not be verified against the memories the synthesis
        # actually read. See `evaluate_dry_run` for what that costs.
        "NO_BASED_ON",
        # No usable `usage` / `duration_ms` / `diff` block: the corresponding
        # measurement is absent rather than guessed at.
        "NO_USAGE",
        "NO_DURATION",
        "NO_DIFF",
        # `preview_content` was missing, not a string, or not JSON.
        "PREVIEW_NOT_JSON",
        # Parsed, but this scope's root key was absent or not an object --
        # including the other scope's document, which is never partially
        # salvaged.
        "PREVIEW_ROOT_MISSING",
        # Parsed and rooted correctly, but the document did not satisfy the
        # response schema the model was provisioned with.
        "DOCUMENT_INVALID",
        # The document offered no items in any of this scope's categories.
        "EMPTY_PREVIEW",
        # At least one offered item failed structural, durability or
        # category validation and was dropped on its own.
        "INELIGIBLE_ITEMS_DROPPED",
        # At least one otherwise-valid item lost every evidence reference
        # against a real `based_on` (only reachable when one was present).
        "UNGROUNDED_ITEMS_DROPPED",
        # At least two candidates shared a claim key and merged into one.
        "DUPLICATE_ITEMS_MERGED",
        # The ranked list was longer than the budget, so its tail was cut.
        "BUDGET_TRUNCATED",
    }
)


@dataclass(frozen=True)
class EvaluationResult:
    """One non-persisting measurement of one bank's structured profile.

    A dataclass rather than a Pydantic model: this never crosses a wire
    boundary that needs a JSON Schema, it is constructed in exactly one
    place, and `CompiledProfileItem` above already sets the convention. Every
    field after `outcome` defaults to "not measured" so a run that stops
    early (no model, upstream refusal) reports honest absence instead of
    zeros that read like measurements.

    Field notes that are not obvious from the names:

    - `requested_mode` / `effective_mode`: which delivery mode was asked for
      and which was actually evaluated. For `profile-check` both are always
      `structured` -- it exists to measure the structured path, and Task 5
      wired no fallback from structured delivery to the legacy prose section
      (`memory/api/brief.py`), so there is nothing to degrade to. They are
      reported anyway because this is the shared result shape the curated
      delivery gate also reports through, where one case is run in each mode
      and the two are compared.
    - `retrieved_fact_count` / `used_fact_count`: only populated when the
      response carried a `based_on` block, since without one there is no
      statement of what the synthesis read. Never synthesized from the
      preview itself.
    - `curated_case_pass_count` / `curated_case_fail_count`: always None
      here. `profile-check` measures one real bank's real synthesis output,
      which has no relationship to a curated adversarial corpus; those two
      fields belong to the curated delivery gate that shares this shape.
    - `displacement_count`: items that were eligible, grounded, deduplicated
      and ranked, and still fell outside the budget prefix -- the plan's own
      sense of displacement ("a new item enters only by displacing the
      current last item"). An item dropped for ineligibility or lost
      grounding never reached the ranked list, so it displaced nothing and
      is not counted here.
    """

    scope: ProfileScope
    outcome: EvaluationOutcome
    requested_mode: DeliveryMode = "structured"
    effective_mode: DeliveryMode = "structured"
    would_persist: bool = False
    retrieved_fact_count: int | None = None
    used_fact_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None
    schema_valid: bool = False
    candidate_item_count: int = 0
    delivered_item_count: int = 0
    displacement_count: int = 0
    curated_case_pass_count: int | None = None
    curated_case_fail_count: int | None = None
    warning_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable view, for `profile-check --json`."""
        return asdict(self)


_RESPONSE_MODEL: dict[ProfileScope, type[BaseModel]] = {
    "user": _UserProfileResponse,
    "project": _ProjectProfileResponse,
}


def _count_or_none(value: Any) -> int | None:
    """A non-negative integer measurement, or None when the response did not
    report one. `bool` is an `int` subclass and a JSON `true` is not a token
    count, so it is rejected explicitly."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def _diff_would_persist(diff: Any) -> tuple[bool, bool]:
    """`(would_persist, diff_reported)` from the preview's own `diff` block.

    A real refresh writes new content exactly when the preview says the
    document would change, so the upstream diff is the answer rather than
    anything this module could infer. Every integer entry counts, whatever
    it is named, so an `updated`/`changed` key this repository has not seen
    still registers.
    """
    if not isinstance(diff, dict):
        return False, False
    counts = [
        count
        for count in (_count_or_none(value) for value in diff.values())
        if count is not None
    ]
    if not counts:
        return False, False
    return any(counts), True


def _preview_document(
    scope: ProfileScope, preview_content: Any
) -> tuple[dict[str, Any], bool, str | None]:
    """`(document, schema_valid, warning_code)` for one `preview_content`.

    `schema_valid` is whole-document validity against the response schema
    the model was provisioned with, not a per-item verdict: Hindsight was
    handed that exact schema, so one item it could not satisfy is a real
    finding for the rollout gate. The document is still returned in that
    case, because the normalizer drops bad items one at a time and the
    surviving counts are what say how bad the damage is.
    """
    if not isinstance(preview_content, str):
        return {}, False, "PREVIEW_NOT_JSON"
    try:
        parsed = json.loads(preview_content)
    except (json.JSONDecodeError, ValueError):
        return {}, False, "PREVIEW_NOT_JSON"

    # An empty document is either a missing/non-object root, which is not
    # schema-valid, or this scope's genuinely empty profile, which is (and
    # is reported as an empty profile below instead).
    document = _structured_document(scope, {"structured_output": parsed})
    if not document and (
        not isinstance(parsed, dict) or not isinstance(parsed.get(_ROOT_KEY[scope]), dict)
    ):
        return {}, False, "PREVIEW_ROOT_MISSING"

    try:
        _RESPONSE_MODEL[scope].model_validate(parsed)
    except ValidationError:
        return document, False, "DOCUMENT_INVALID"
    return document, True, None


def _cited_evidence_ids(scope: ProfileScope, document: dict[str, Any]) -> set[str]:
    """Every evidence ID the preview document cites, read defensively.

    Used only as the permissive grounding set when the response carries no
    `based_on` -- see `evaluate_dry_run`.
    """
    cited: set[str] = set()
    for category in _SCOPE_CATEGORIES[scope]:
        raw_items = document.get(category)
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            evidence_ids = raw_item.get("evidence_ids")
            if not isinstance(evidence_ids, list):
                continue
            cited.update(value for value in evidence_ids if isinstance(value, str))
    return cited


def evaluate_dry_run(scope: ProfileScope, dry_run_response: Any) -> EvaluationResult:
    """Measure one upstream dry-run-refresh preview. Pure; writes nothing.

    The response shape this reads is the one Task 2 pinned against the
    client (`usage`, `duration_ms`, `diff`, and `preview_content` as a JSON
    *string*), every field read defensively: a preview is upstream output,
    not a contract this repository controls, and a nightly evaluator that
    raises on a surprise field measures nothing.

    Grounding is the one gate a preview cannot verify. Task 2's pinned shape
    carries no `based_on`, and no live Hindsight instance exists here to say
    whether the real response mirrors the persisted listing's block. So:
    when the response does carry one it is used exactly as delivery uses it,
    and grounding is genuinely measured (`retrieved_fact_count`,
    `used_fact_count`, `UNGROUNDED_ITEMS_DROPPED`); when it does not, the
    IDs the preview itself cites stand in as the grounding set, the fact
    counts are reported as absent, and `NO_BASED_ON` says so.

    The alternative -- grounding against an empty set -- would fail every
    item on every real call and report an empty profile for a bank whose
    delivery is fine, which is worse than useless for a gate that exists to
    measure quality. Declaring one dimension unmeasured keeps the other
    dimensions (structural validity, category compatibility, deduplication,
    ranking and budget displacement) honest and running through the exact
    same normalizer real delivery uses.
    """
    warnings: set[str] = set()
    response = dry_run_response if isinstance(dry_run_response, dict) else {}

    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _count_or_none(usage.get("input_tokens"))
    output_tokens = _count_or_none(usage.get("output_tokens"))
    total_tokens = _count_or_none(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if input_tokens is None and output_tokens is None:
        warnings.add("NO_USAGE")

    duration_ms = _count_or_none(response.get("duration_ms"))
    if duration_ms is None:
        warnings.add("NO_DURATION")

    diff_persists, diff_reported = _diff_would_persist(response.get("diff"))
    if not diff_reported:
        warnings.add("NO_DIFF")

    document, schema_valid, preview_warning = _preview_document(
        scope, response.get("preview_content")
    )
    if preview_warning is not None:
        warnings.add(preview_warning)

    candidate_item_count = 0
    for category in _SCOPE_CATEGORIES[scope]:
        raw_items = document.get(category)
        if isinstance(raw_items, list):
            candidate_item_count += len(raw_items)
    if candidate_item_count == 0:
        warnings.add("EMPTY_PREVIEW")

    structured_output = {_ROOT_KEY[scope]: document}
    # Permissive grounding: nothing an item cites can be missing from it, so
    # the only gates left are structural, durability and category ones --
    # which makes this exactly the eligible-item count.
    eligible = _profile_candidates(
        scope,
        {
            "structured_output": structured_output,
            "based_on": {"memories": sorted(_cited_evidence_ids(scope, document))},
        },
    )
    if len(eligible) < candidate_item_count:
        warnings.add("INELIGIBLE_ITEMS_DROPPED")

    based_on = response.get("based_on")
    grounding_reported = isinstance(based_on, dict) and isinstance(
        based_on.get("memories"), list
    )
    if grounding_reported:
        grounded_response = {
            "structured_output": structured_output,
            "based_on": based_on,
        }
        candidates = _profile_candidates(scope, grounded_response)
        retrieved_fact_count: int | None = len(_based_on_memory_ids(grounded_response))
        if len(candidates) < len(eligible):
            warnings.add("UNGROUNDED_ITEMS_DROPPED")
    else:
        warnings.add("NO_BASED_ON")
        candidates = eligible
        retrieved_fact_count = None

    ranked = _rank_candidates(candidates)
    if len(ranked) < len(candidates):
        warnings.add("DUPLICATE_ITEMS_MERGED")

    budget = _SCOPE_BUDGET[scope]
    delivered = ranked[:budget]
    displacement_count = max(0, len(ranked) - budget)
    if displacement_count:
        warnings.add("BUDGET_TRUNCATED")

    used_fact_count = (
        len({reference for item in delivered for reference in item.evidence_ids})
        if grounding_reported
        else None
    )

    if not schema_valid:
        outcome: EvaluationOutcome = "schema_invalid"
    elif output_tokens is not None and output_tokens > _MAX_TOKENS[scope]:
        outcome = "budget_exceeded"
    elif not delivered:
        outcome = "empty_profile"
    else:
        outcome = "ok"

    return EvaluationResult(
        scope=scope,
        outcome=outcome,
        # A refresh that reports no diff of its own would still have written
        # whatever it just synthesized, so a usable preview stands in for
        # the missing statement rather than claiming nothing would change.
        would_persist=diff_persists if diff_reported else outcome == "ok",
        retrieved_fact_count=retrieved_fact_count,
        used_fact_count=used_fact_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        duration_ms=duration_ms,
        schema_valid=schema_valid,
        candidate_item_count=candidate_item_count,
        delivered_item_count=len(delivered),
        displacement_count=displacement_count,
        warning_codes=tuple(sorted(warnings)),
    )
