"""Phase 4 structured profile contracts (SPEC §4.2, §6.4, §10-§13, §17).

Scope of this module *so far* (Task 1 only): the closed, bounded JSON
Schema/Pydantic contract a Hindsight mental model's structured output must
match, plus structural validation of documents that could come back. This
module does not query Hindsight, does not compute eligibility/ranking/
displacement across a document (only the single-item durability check that
belongs at the item boundary), and does not touch any HTTP route. Later
Phase 4 tasks extend this same file with provisioning (Task 3), normalization/
ranking/displacement (Task 4), brief compilation (Task 5), correction-refresh
targeting (Task 6) and evaluation (Task 7).

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

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

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
# `reflect_response.based_on.memories` is Task 4's job). No format is
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
    "is never eligible for a profile item, but an observed convention is. "
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
    client, bank_id: str, model: dict, source_query: str, trigger: dict[str, Any]
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
    """
    changed: dict[str, object] = {}
    if model.get("source_query") != source_query:
        changed["source_query"] = source_query

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
    trigger = _profile_trigger(scope)

    model = _find_profile(client, bank_id)
    if model is None:
        client.create_mental_model(
            bank_id,
            name=PROFILE_MODEL_NAME,
            source_query=source_query,
            max_tokens=_MAX_TOKENS[scope],
            trigger=trigger,
            tags=[],
        )
        return "created"

    _reconcile_profile(client, bank_id, model, source_query, trigger)
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
