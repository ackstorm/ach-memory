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
EvidenceIds = Annotated[list[EvidenceId], Field(min_length=1, max_length=8)]

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
    def _unique_evidence_ids(cls, value: list[str]) -> list[str]:
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


_UserCategoryField = Annotated[list[ProfileItem], Field(max_length=USER_PROFILE_BUDGET)]
_ProjectCategoryField = Annotated[list[ProfileItem], Field(max_length=PROJECT_PROFILE_BUDGET)]


class UserProfileDocument(BaseModel):
    """The `user_profile` response document (plan "Structured contracts").
    Category membership is structural: `extra="forbid"` means the model
    cannot invent a category key outside this fixed set of four, and each
    category is capped at the user profile's total budget of 15."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interaction: _UserCategoryField = Field(default_factory=list)
    engineering: _UserCategoryField = Field(default_factory=list)
    preferences: _UserCategoryField = Field(default_factory=list)
    constraints: _UserCategoryField = Field(default_factory=list)

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
    25."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    architecture: _ProjectCategoryField = Field(default_factory=list)
    decisions: _ProjectCategoryField = Field(default_factory=list)
    workflow: _ProjectCategoryField = Field(default_factory=list)
    testing: _ProjectCategoryField = Field(default_factory=list)
    conventions: _ProjectCategoryField = Field(default_factory=list)
    gotchas: _ProjectCategoryField = Field(default_factory=list)

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
