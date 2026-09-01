"""Tasks 1 and 4: the closed user/project structured-profile response
schemas, and the deterministic normalization/ranking/displacement pass
over one `reflect_response` (SPEC §4.2, §6.4, §10-§13, §17; plan
"Structured contracts" and "Eligibility, ordering and displacement").

This file exercises schema shape, structural/cross-field validation and
the pure `compile_profile` compiler on `memory.profiles`. It does not
exercise Hindsight queries or rendering -- those belong to later Phase 4
tasks and their own test additions to this file.
"""

import hashlib
import json

import pytest
from pydantic import ValidationError

from memory.profiles import (
    PROJECT_PROFILE_BUDGET,
    USER_PROFILE_BUDGET,
    CompiledProfileItem,
    ProfileItem,
    ProjectProfileDocument,
    UserProfileDocument,
    compile_profile,
    project_response_schema,
    user_response_schema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_item(**overrides) -> dict:
    """A structurally valid non-gotcha item (the plan's own example)."""
    fields = {
        "claim": "Run the focused tests before the full suite.",
        "kind": "convention",
        "origin": "confirmed",
        "negative": False,
        "failure": None,
        "cause": None,
        "reproduction": None,
        "provenance": "Accepted workflow convention.",
        "evidence_ids": ["mem-1"],
    }
    fields.update(overrides)
    return fields


def _gotcha_item(**overrides) -> dict:
    """A structurally valid gotcha item: failure + cause + provenance."""
    fields = _base_item(
        claim="Deploy script fails when DATABASE_URL is unset.",
        kind="gotcha",
        origin="observed",
        failure="Deploy script exits with a stack trace.",
        cause="DATABASE_URL is not exported in the deploy shell.",
        provenance="Observed twice in CI logs.",
    )
    fields.update(overrides)
    return fields


def _item_of_kind(kind: str, *, negative: bool = False) -> dict:
    """One structurally valid item of the given kind, at a durable origin,
    used by the category/kind compatibility matrix below (durability
    itself is exercised separately)."""
    if kind == "gotcha":
        return _gotcha_item(negative=negative)
    return _base_item(kind=kind, origin="stated", negative=negative)


# ---------------------------------------------------------------------------
# Step 1: schema shape
# ---------------------------------------------------------------------------


def test_plan_example_item_is_valid():
    item = ProfileItem(
        claim="Run the focused tests before the full suite.",
        kind="convention",
        origin="confirmed",
        negative=False,
        failure=None,
        cause=None,
        reproduction=None,
        provenance="Accepted workflow convention.",
        evidence_ids=["<hindsight-memory-id>"],
    )
    assert item.kind == "convention"


def test_user_document_category_keys():
    assert set(UserProfileDocument.model_fields) == {
        "interaction",
        "engineering",
        "preferences",
        "constraints",
    }


def test_project_document_category_keys():
    assert set(ProjectProfileDocument.model_fields) == {
        "architecture",
        "decisions",
        "workflow",
        "testing",
        "conventions",
        "gotchas",
    }


def test_empty_user_profile_document_is_valid():
    doc = UserProfileDocument()
    assert doc.interaction == ()
    assert doc.engineering == ()
    assert doc.preferences == ()
    assert doc.constraints == ()


def test_empty_project_profile_document_is_valid():
    doc = ProjectProfileDocument()
    assert doc.architecture == ()
    assert doc.decisions == ()
    assert doc.workflow == ()
    assert doc.testing == ()
    assert doc.conventions == ()
    assert doc.gotchas == ()


def test_profile_item_is_frozen():
    item = ProfileItem(**_base_item())
    with pytest.raises(ValidationError):
        item.claim = "changed"


def test_user_profile_document_is_frozen():
    doc = UserProfileDocument()
    with pytest.raises(ValidationError):
        doc.preferences = [ProfileItem(**_base_item())]


def test_project_profile_document_is_frozen():
    doc = ProjectProfileDocument()
    with pytest.raises(ValidationError):
        doc.conventions = [ProfileItem(**_base_item())]


# --- frozen means frozen: list fields would stay mutable in place (review
# finding 1) -- `evidence_ids` and every document category field are tuples,
# not lists, specifically so there is no in-place mutation path left open
# and so a validated item/document is hashable. -------------------------


def test_evidence_ids_is_a_tuple_with_no_append():
    item = ProfileItem(**_base_item())
    assert isinstance(item.evidence_ids, tuple)
    with pytest.raises(AttributeError):
        item.evidence_ids.append("mem-2")


def test_document_category_fields_are_tuples_with_no_append():
    doc = UserProfileDocument(preferences=[_base_item(kind="preference", origin="stated")])
    assert isinstance(doc.preferences, tuple)
    with pytest.raises(AttributeError):
        doc.preferences.append("not an item")


def test_profile_item_is_hashable():
    item_a = ProfileItem(**_base_item())
    item_b = ProfileItem(**_base_item())
    assert hash(item_a) == hash(item_b)
    assert {item_a, item_b} == {item_a}


def test_user_profile_document_is_hashable():
    doc_a = UserProfileDocument(preferences=[_base_item(kind="preference", origin="stated")])
    doc_b = UserProfileDocument(preferences=[_base_item(kind="preference", origin="stated")])
    assert hash(doc_a) == hash(doc_b)
    assert {doc_a, doc_b} == {doc_a}


def test_project_profile_document_is_hashable():
    doc_a = ProjectProfileDocument(conventions=[_base_item(kind="convention", origin="stated")])
    doc_b = ProjectProfileDocument(conventions=[_base_item(kind="convention", origin="stated")])
    assert hash(doc_a) == hash(doc_b)
    assert {doc_a, doc_b} == {doc_a}


def test_user_response_schema_root_key():
    schema = user_response_schema()
    assert set(schema["properties"]) == {"user_profile"}
    assert schema["required"] == ["user_profile"]
    assert schema["additionalProperties"] is False


def test_project_response_schema_root_key():
    schema = project_response_schema()
    assert set(schema["properties"]) == {"project_profile"}
    assert schema["required"] == ["project_profile"]
    assert schema["additionalProperties"] is False


def _assert_additional_properties_false_everywhere(schema: dict) -> None:
    for candidate in (schema, *schema.get("$defs", {}).values()):
        if candidate.get("type") == "object" or "properties" in candidate:
            assert candidate.get("additionalProperties") is False, candidate


def test_user_schema_closed_at_every_object_level():
    _assert_additional_properties_false_everywhere(user_response_schema())


def test_project_schema_closed_at_every_object_level():
    _assert_additional_properties_false_everywhere(project_response_schema())


def test_user_category_max_items_matches_budget():
    schema = user_response_schema()
    doc_schema = schema["$defs"]["UserProfileDocument"]
    for category in ("interaction", "engineering", "preferences", "constraints"):
        assert doc_schema["properties"][category]["maxItems"] == USER_PROFILE_BUDGET


def test_project_category_max_items_matches_budget():
    schema = project_response_schema()
    doc_schema = schema["$defs"]["ProjectProfileDocument"]
    categories = ("architecture", "decisions", "workflow", "testing", "conventions", "gotchas")
    for category in categories:
        assert doc_schema["properties"][category]["maxItems"] == PROJECT_PROFILE_BUDGET


def test_budgets_match_the_plan():
    assert USER_PROFILE_BUDGET == 15
    assert PROJECT_PROFILE_BUDGET == 25


# --- category/kind/negative compatibility rules are discoverable in the
# schema text itself (review finding 2): every category `$ref`s the same
# unconstrained ProfileItem, so JSON Schema structure alone cannot express
# "gotcha only in gotchas" or "negative=true only in constraints". These
# tests assert that a synthesizing model reading `description` text on the
# category fields has a chance to learn the strictest rules before ever
# producing invalid output, not only after a validation failure. -----------


def test_project_gotchas_description_states_gotcha_only_rule():
    schema = project_response_schema()
    doc_schema = schema["$defs"]["ProjectProfileDocument"]
    gotchas_description = doc_schema["properties"]["gotchas"]["description"].lower()
    assert "gotcha" in gotchas_description
    assert "only" in gotchas_description

    # The topical categories that accept more than one kind (and so could
    # plausibly tempt a misplaced gotcha) explicitly disclaim it; the two
    # single-kind categories (decisions/conventions) already exclude a
    # gotcha implicitly by naming exactly one allowed kind each -- see
    # test_every_category_field_has_a_kind_description.
    for category in ("architecture", "workflow", "testing"):
        description = doc_schema["properties"][category]["description"].lower()
        assert "gotcha" in description
        assert "never" in description


def test_user_constraints_description_states_negative_only_rule():
    schema = user_response_schema()
    doc_schema = schema["$defs"]["UserProfileDocument"]
    constraints_description = doc_schema["properties"]["constraints"]["description"].lower()
    assert "negative=true" in constraints_description
    assert "only" in constraints_description

    # The other three user categories should each point negative=true
    # items back at constraints, not stay silent about it.
    for category in ("interaction", "engineering", "preferences"):
        description = doc_schema["properties"][category]["description"].lower()
        assert "constraints" in description
        assert "negative=true" in description


def test_every_category_field_has_a_kind_description():
    user_schema = user_response_schema()["$defs"]["UserProfileDocument"]
    for category in ("interaction", "engineering", "preferences", "constraints"):
        assert "kind" in user_schema["properties"][category]["description"].lower()

    project_schema = project_response_schema()["$defs"]["ProjectProfileDocument"]
    categories = ("architecture", "decisions", "workflow", "testing", "conventions", "gotchas")
    for category in categories:
        assert "kind" in project_schema["properties"][category]["description"].lower()


# --- bounded one-line strings -----------------------------------------------


def test_claim_rejects_blank():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(claim="   "))


def test_claim_rejects_too_long():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(claim="x" * 321))


def test_claim_accepts_max_length():
    item = ProfileItem(**_base_item(claim="x" * 320))
    assert len(item.claim) == 320


def test_claim_rejects_multiline():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(claim="line one\nline two"))


@pytest.mark.parametrize("field_name", ["failure", "cause", "reproduction", "provenance"])
def test_optional_lines_reject_too_long(field_name):
    fields = _gotcha_item()
    fields[field_name] = "x" * 241
    with pytest.raises(ValidationError):
        ProfileItem(**fields)


@pytest.mark.parametrize("field_name", ["failure", "cause", "reproduction", "provenance"])
def test_optional_lines_accept_max_length(field_name):
    fields = _gotcha_item()
    fields[field_name] = "x" * 240
    item = ProfileItem(**fields)
    assert len(getattr(item, field_name)) == 240


@pytest.mark.parametrize("field_name", ["failure", "cause", "reproduction", "provenance"])
def test_optional_lines_reject_multiline(field_name):
    fields = _gotcha_item()
    fields[field_name] = "one\ntwo"
    with pytest.raises(ValidationError):
        ProfileItem(**fields)


@pytest.mark.parametrize("field_name", ["failure", "cause", "reproduction", "provenance"])
def test_optional_lines_reject_blank_string(field_name):
    # None means "absent"; an empty/whitespace string is neither a real
    # line nor a real null, so it is rejected rather than silently kept.
    fields = _gotcha_item()
    fields[field_name] = "   "
    with pytest.raises(ValidationError):
        ProfileItem(**fields)


# --- closed enums -------------------------------------------------------


def test_kind_enum_rejects_technical_claim():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(kind="technical_claim"))


def test_kind_enum_rejects_working_state():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(kind="working_state"))


def test_origin_enum_rejects_inferred():
    # Deliberate regression test: `inferred` never reaches a durable
    # profile (SPEC §6.4), and this enum was explicitly designed to
    # exclude it rather than reject it via the durability matrix.
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(origin="inferred"))


# --- evidence_ids ---------------------------------------------------------


def test_evidence_ids_requires_at_least_one():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(evidence_ids=[]))


def test_evidence_ids_accepts_the_eight_item_cap():
    ProfileItem(**_base_item(evidence_ids=[f"mem-{i}" for i in range(8)]))


def test_evidence_ids_rejects_nine_items():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(evidence_ids=[f"mem-{i}" for i in range(9)]))


def test_evidence_ids_rejects_duplicates():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(evidence_ids=["mem-1", "mem-1"]))


def test_evidence_ids_rejects_blank_entry():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(evidence_ids=[""]))


def test_evidence_ids_rejects_overlong_entry():
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(evidence_ids=["x" * 201]))


# --- absence checks (plan "Non-negotiable contracts") ----------------------

_FORBIDDEN_METADATA_FIELDS = ["importance", "priority", "bank", "profile_eligible", "score"]
_WORKING_STATE_OR_PROJECT_METADATA_FIELDS = [
    "current_task",
    "next_steps",
    "name",
    "locator",
    "canonical_spec",
    "purpose",
]


@pytest.mark.parametrize("field_name", _FORBIDDEN_METADATA_FIELDS)
def test_profile_item_rejects_forbidden_metadata_fields(field_name):
    fields = _base_item()
    fields[field_name] = "anything"
    with pytest.raises(ValidationError):
        ProfileItem(**fields)


def test_profile_item_schema_has_no_forbidden_metadata_fields():
    properties = ProfileItem.model_json_schema()["properties"]
    for field_name in _FORBIDDEN_METADATA_FIELDS:
        assert field_name not in properties


@pytest.mark.parametrize("field_name", _WORKING_STATE_OR_PROJECT_METADATA_FIELDS)
def test_profile_item_rejects_working_state_and_project_metadata_fields(field_name):
    fields = _base_item()
    fields[field_name] = "anything"
    with pytest.raises(ValidationError):
        ProfileItem(**fields)


@pytest.mark.parametrize("field_name", _WORKING_STATE_OR_PROJECT_METADATA_FIELDS)
def test_user_document_rejects_working_state_and_project_metadata_fields(field_name):
    with pytest.raises(ValidationError):
        UserProfileDocument(**{field_name: "anything"})


@pytest.mark.parametrize("field_name", _WORKING_STATE_OR_PROJECT_METADATA_FIELDS)
def test_project_document_rejects_working_state_and_project_metadata_fields(field_name):
    with pytest.raises(ValidationError):
        ProjectProfileDocument(**{field_name: "anything"})


# ---------------------------------------------------------------------------
# Step 3: cross-field validation
# ---------------------------------------------------------------------------

# --- durability matrix (SPEC §6.4, enforced again at the profile boundary) -

_ALL_KINDS = ["preference", "decision", "convention", "gotcha"]
_ALL_ORIGINS = ["stated", "confirmed", "observed"]

# Independently transcribed from SPEC §6.4 -- NOT imported from
# `memory.profiles._DURABILITY` -- so a bug in that private table would
# still be caught here. Within this schema's closed kind/origin enums,
# only observed preference and observed decision are excluded; `inferred`
# and `technical_claim` are already excluded by the enums themselves and
# so are covered by the separate closed-enum tests above, not this table.
_DURABILITY_EXPECTED_ELIGIBLE = {
    ("preference", "stated"): True,
    ("preference", "confirmed"): True,
    ("preference", "observed"): False,
    ("decision", "stated"): True,
    ("decision", "confirmed"): True,
    ("decision", "observed"): False,
    ("convention", "stated"): True,
    ("convention", "confirmed"): True,
    ("convention", "observed"): True,
    ("gotcha", "stated"): True,
    ("gotcha", "confirmed"): True,
    ("gotcha", "observed"): True,
}


@pytest.mark.parametrize("origin", _ALL_ORIGINS)
@pytest.mark.parametrize("kind", _ALL_KINDS)
def test_durability_matrix_within_closed_enums(kind, origin):
    if kind == "gotcha":
        fields = _gotcha_item(origin=origin)
    else:
        fields = _base_item(kind=kind, origin=origin)

    if _DURABILITY_EXPECTED_ELIGIBLE[(kind, origin)]:
        item = ProfileItem(**fields)
        assert (item.kind, item.origin) == (kind, origin)
    else:
        with pytest.raises(ValidationError):
            ProfileItem(**fields)


# --- gotcha shape ---------------------------------------------------------


def test_gotcha_requires_failure():
    with pytest.raises(ValidationError):
        ProfileItem(**_gotcha_item(failure=None))


def test_gotcha_requires_cause_or_reproduction():
    with pytest.raises(ValidationError):
        ProfileItem(**_gotcha_item(cause=None, reproduction=None))


def test_gotcha_valid_with_cause_only():
    item = ProfileItem(**_gotcha_item(cause="Env var missing.", reproduction=None))
    assert item.cause is not None
    assert item.reproduction is None


def test_gotcha_valid_with_reproduction_only():
    item = ProfileItem(
        **_gotcha_item(cause=None, reproduction="Run deploy without DATABASE_URL set.")
    )
    assert item.cause is None
    assert item.reproduction is not None


def test_gotcha_valid_with_both_cause_and_reproduction():
    item = ProfileItem(
        **_gotcha_item(cause="Env var missing.", reproduction="Run deploy without it set.")
    )
    assert item.cause is not None
    assert item.reproduction is not None


def test_gotcha_requires_provenance():
    with pytest.raises(ValidationError):
        ProfileItem(**_gotcha_item(provenance=None))


@pytest.mark.parametrize("field_name", ["failure", "cause", "reproduction"])
def test_non_gotcha_rejects_gotcha_only_fields(field_name):
    fields = _base_item()
    fields[field_name] = "should not be set on a non-gotcha item"
    with pytest.raises(ValidationError):
        ProfileItem(**fields)


def test_non_gotcha_allows_null_provenance():
    item = ProfileItem(**_base_item(provenance=None))
    assert item.provenance is None


# --- explicit-negative preservation -----------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_negative_flag_round_trips_exactly(value):
    item = ProfileItem(**_base_item(negative=value))
    assert item.negative is value
    assert item.model_dump()["negative"] is value


def test_negative_field_is_required():
    fields = _base_item()
    del fields["negative"]
    with pytest.raises(ValidationError):
        ProfileItem(**fields)


@pytest.mark.parametrize("value", [1, 0, "true", "false", "yes"])
def test_negative_field_rejects_non_boolean_coercion(value):
    with pytest.raises(ValidationError):
        ProfileItem(**_base_item(negative=value))


# --- category/kind compatibility (Task 1's documented design judgment) -----

# Independently transcribed from the mapping documented in
# `memory.profiles` (not imported from the private `_USER_CATEGORY_KINDS`/
# `_PROJECT_CATEGORY_KINDS` dicts), so a bug in those dicts would still be
# caught here.
_EXPECTED_USER_CATEGORY_KINDS = {
    "interaction": {"preference", "convention"},
    "engineering": {"preference", "decision", "convention"},
    "preferences": {"preference"},
    "constraints": {"preference", "decision", "convention"},
}
_EXPECTED_PROJECT_CATEGORY_KINDS = {
    "architecture": {"decision", "convention"},
    "decisions": {"decision"},
    "workflow": {"decision", "convention"},
    "testing": {"decision", "convention"},
    "conventions": {"convention"},
    "gotchas": {"gotcha"},
}


@pytest.mark.parametrize("kind", _ALL_KINDS)
@pytest.mark.parametrize("category", sorted(_EXPECTED_USER_CATEGORY_KINDS))
def test_user_category_kind_compatibility_matrix(category, kind):
    # `constraints` is the exclusive home for negative=true items; every
    # other user category holds only negative=false items (see the
    # dedicated negative-flag tests below for the other half of that rule).
    negative = category == "constraints"
    doc_kwargs = {category: [_item_of_kind(kind, negative=negative)]}

    if kind in _EXPECTED_USER_CATEGORY_KINDS[category]:
        doc = UserProfileDocument(**doc_kwargs)
        assert len(getattr(doc, category)) == 1
    else:
        with pytest.raises(ValidationError):
            UserProfileDocument(**doc_kwargs)


@pytest.mark.parametrize("kind", _ALL_KINDS)
@pytest.mark.parametrize("category", sorted(_EXPECTED_PROJECT_CATEGORY_KINDS))
def test_project_category_kind_compatibility_matrix(category, kind):
    doc_kwargs = {category: [_item_of_kind(kind)]}

    if kind in _EXPECTED_PROJECT_CATEGORY_KINDS[category]:
        doc = ProjectProfileDocument(**doc_kwargs)
        assert len(getattr(doc, category)) == 1
    else:
        with pytest.raises(ValidationError):
            ProjectProfileDocument(**doc_kwargs)


def test_gotcha_rejected_outside_the_gotchas_category():
    with pytest.raises(ValidationError):
        ProjectProfileDocument(architecture=[_gotcha_item()])


def test_gotcha_accepted_in_the_gotchas_category():
    doc = ProjectProfileDocument(gotchas=[_gotcha_item()])
    assert doc.gotchas[0].kind == "gotcha"


def test_user_negative_item_rejected_outside_constraints():
    fields = _base_item(kind="preference", origin="stated", negative=True)
    with pytest.raises(ValidationError):
        UserProfileDocument(preferences=[fields])


def test_user_non_negative_item_rejected_in_constraints():
    fields = _base_item(kind="preference", origin="stated", negative=False)
    with pytest.raises(ValidationError):
        UserProfileDocument(constraints=[fields])


def test_project_negative_item_is_not_specially_restricted():
    # Unlike the user document, the project schema has no dedicated
    # "constraints" bucket, so a negative-flagged item stays wherever it
    # topically belongs.
    fields = _base_item(kind="convention", origin="stated", negative=True)
    doc = ProjectProfileDocument(conventions=[fields])
    assert doc.conventions[0].negative is True


# --- no exact duplicates within one response --------------------------------


def test_duplicate_items_rejected_same_category_and_negative_flag():
    fields = _base_item(kind="preference", origin="stated")
    item_a = dict(fields, evidence_ids=["mem-1"])
    item_b = dict(fields, evidence_ids=["mem-2"])
    with pytest.raises(ValidationError):
        UserProfileDocument(preferences=[item_a, item_b])


def test_duplicate_detection_normalizes_whitespace_and_case():
    fields = _base_item(kind="preference", origin="stated")
    item_a = dict(fields, claim="Run   the Focused tests before the full suite.")
    item_b = dict(fields, claim="run the focused tests before the full suite.")
    with pytest.raises(ValidationError):
        UserProfileDocument(preferences=[item_a, item_b])


def test_duplicate_claim_allowed_across_different_categories():
    # The dedup key is (category, normalized claim, negative) -- the same
    # claim filed under two different, both-valid categories is not an
    # "exact duplicate" by that definition.
    fields = _base_item(kind="preference", origin="stated")
    doc = UserProfileDocument(preferences=[fields], interaction=[fields])
    assert len(doc.preferences) == 1
    assert len(doc.interaction) == 1


def test_non_duplicate_claims_in_same_category_are_both_kept():
    fields_a = _base_item(kind="preference", origin="stated", claim="Prefers tabs.")
    fields_b = _base_item(kind="preference", origin="stated", claim="Prefers dark mode.")
    doc = UserProfileDocument(preferences=[fields_a, fields_b])
    assert len(doc.preferences) == 2


# ---------------------------------------------------------------------------
# Step 4: stable schema serialization
#
# These hashes pin the exact JSON Schema Hindsight is asked to fill in as a
# mental model `response_schema`. They exist so an accidental Pydantic/
# Field/config change to ProfileItem, UserProfileDocument or
# ProjectProfileDocument -- a renamed field, a changed bound, a dropped
# `additionalProperties: false` -- fails a test instead of silently
# reaching Hindsight as a different contract than the one this task
# specified. If a change to this schema is intentional, recompute the hash
# with `_schema_hash(...)` below and update the literal in the same commit
# as the schema change; do not treat a failure here as flaky and retry it.
# ---------------------------------------------------------------------------


def _schema_hash(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest()


def test_user_response_schema_hash_is_stable():
    assert (
        _schema_hash(user_response_schema())
        == "645f19c0ca6ac8e84cf27ac1e054e4b7cdb85c88c17fbbec0084ffbb4cd2735d"
    )


def test_project_response_schema_hash_is_stable():
    assert (
        _schema_hash(project_response_schema())
        == "bef57794af3bcae45cffc57a38f407187670da42aca4a88c0758724353995dbc"
    )


# ---------------------------------------------------------------------------
# Task 4: normalization, ranking and displacement
#
# Everything below drives the real entry point, `compile_profile(scope,
# reflect_response)`, with raw upstream-shaped dicts -- never by handing it
# already-constructed `ProfileItem`s. That is the point of the compiler: it
# is the only place that has to survive a partially-invalid upstream
# response, dropping exactly the offending item and keeping the rest.
# ---------------------------------------------------------------------------


_ROOT_KEY_FOR_SCOPE = {"user": "user_profile", "project": "project_profile"}


def _grounded(*memory_ids: str) -> dict:
    """A `based_on` block in the shape the compiler assumes: a list of
    memory objects each carrying an `id`."""
    return {"memories": [{"id": memory_id} for memory_id in memory_ids]}


def _evidence_in(categories: dict) -> list[str]:
    """Every evidence ID any item in the fixture cites, in first-seen order.
    Used only to build the default `based_on`, so a test has to be explicit
    about grounding exactly when grounding is what it is testing."""
    found: list[str] = []
    for items in categories.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for evidence_id in item.get("evidence_ids") or []:
                if isinstance(evidence_id, str) and evidence_id not in found:
                    found.append(evidence_id)
    return found


# Distinct from `None`, which several tests below pass deliberately as a
# malformed `based_on`.
_AUTO_GROUNDING = object()


def _response(scope: str, categories: dict, based_on: object = _AUTO_GROUNDING) -> dict:
    """One full-detail `reflect_response`, in the shape Task 5 will read off
    a `detail=full` mental model and hand to the compiler."""
    if based_on is _AUTO_GROUNDING:
        based_on = _grounded(*_evidence_in(categories))
    return {
        "structured_output": {_ROOT_KEY_FOR_SCOPE[scope]: categories},
        "based_on": based_on,
    }


# --- Step 1: every matrix combination, at the pipeline level ---------------

# The category each kind is filed under for the pipeline matrix below. A
# gotcha has no valid home in a user document and a preference has no valid
# home in a project document; those two rows use the closest plausible
# category and expect the item to be dropped, which is exactly the
# category/kind rule Task 1 already enforces, now proven per-item through
# the compiler instead of as a whole-document rejection.
_PIPELINE_CATEGORY = {
    "user": {
        "preference": "preferences",
        "decision": "engineering",
        "convention": "interaction",
        "gotcha": "engineering",
    },
    "project": {
        "preference": "workflow",
        "decision": "decisions",
        "convention": "conventions",
        "gotcha": "gotchas",
    },
}

_PIPELINE_CATEGORY_VALID = {
    ("user", "preference"): True,
    ("user", "decision"): True,
    ("user", "convention"): True,
    ("user", "gotcha"): False,
    ("project", "preference"): False,
    ("project", "decision"): True,
    ("project", "convention"): True,
    ("project", "gotcha"): True,
}


@pytest.mark.parametrize("origin", _ALL_ORIGINS)
@pytest.mark.parametrize("kind", _ALL_KINDS)
@pytest.mark.parametrize("scope", ["user", "project"])
def test_compile_enforces_every_matrix_combination(scope, kind, origin):
    category = _PIPELINE_CATEGORY[scope][kind]
    if kind == "gotcha":
        fields = _gotcha_item(origin=origin)
    else:
        fields = _base_item(kind=kind, origin=origin)

    compiled = compile_profile(scope, _response(scope, {category: [fields]}))

    delivered = (
        _DURABILITY_EXPECTED_ELIGIBLE[(kind, origin)] and _PIPELINE_CATEGORY_VALID[(scope, kind)]
    )
    assert len(compiled) == (1 if delivered else 0)
    if delivered:
        assert compiled[0].category == category
        assert compiled[0].representative.kind == kind
        assert compiled[0].representative.origin == origin


def test_one_ineligible_item_drops_alone_and_the_rest_of_the_batch_survives():
    # The whole reason the compiler works on raw dicts: constructing a
    # UserProfileDocument from this pair would raise and lose both items.
    evidence_only = _base_item(claim="Prefers tabs.", kind="preference", origin="observed")
    eligible = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile(
        "user", _response("user", {"preferences": [evidence_only, eligible]})
    )

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


def test_compiled_item_is_frozen():
    compiled = compile_profile(
        "user",
        _response("user", {"preferences": [_base_item(kind="preference", origin="stated")]}),
    )
    assert isinstance(compiled[0], CompiledProfileItem)
    with pytest.raises(AttributeError):
        compiled[0].support_count = 99


# --- Step 2: grounding against based_on -----------------------------------


def test_item_with_no_grounded_evidence_is_dropped():
    fields = _base_item(kind="preference", origin="stated", evidence_ids=["ghost-1", "ghost-2"])

    compiled = compile_profile(
        "user", _response("user", {"preferences": [fields]}, based_on=_grounded("mem-1"))
    )

    assert compiled == ()


def test_support_count_uses_only_distinct_grounded_references():
    fields = _base_item(
        kind="preference", origin="stated", evidence_ids=["mem-1", "ghost-1", "mem-2"]
    )

    compiled = compile_profile(
        "user",
        _response("user", {"preferences": [fields]}, based_on=_grounded("mem-1", "mem-2", "mem-9")),
    )

    assert compiled[0].evidence_ids == ("mem-1", "mem-2")
    assert compiled[0].support_count == 2


def test_repeated_evidence_reference_inside_one_item_drops_only_that_item():
    # `ProfileItem._unique_evidence_ids` already rejects this shape; what
    # matters here is that the rejection stays per-item instead of taking
    # the whole batch down with it.
    repeated = _base_item(
        claim="Prefers tabs.", kind="preference", origin="stated", evidence_ids=["mem-1", "mem-1"]
    )
    clean = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile("user", _response("user", {"preferences": [repeated, clean]}))

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


def test_based_on_accepts_plain_id_strings():
    # Defensive: this repository's own Hindsight fixtures show
    # `based_on.memories` as bare ID strings, while other list endpoints
    # return objects. Both are accepted; neither is required.
    fields = _base_item(kind="preference", origin="stated")

    compiled = compile_profile(
        "user", _response("user", {"preferences": [fields]}, based_on={"memories": ["mem-1"]})
    )

    assert compiled[0].support_count == 1


@pytest.mark.parametrize(
    "based_on",
    [
        None,
        {},
        {"memories": None},
        {"memories": []},
        {"memories": "mem-1"},
        {"memories": [{"identifier": "mem-1"}]},
        {"memories": [{"id": 7}]},
        {"memories": [None]},
        "memories",
        [],
    ],
    ids=[
        "null",
        "empty-object",
        "null-memories",
        "empty-memories",
        "memories-not-a-list",
        "no-id-field",
        "non-string-id",
        "null-entry",
        "based-on-a-string",
        "based-on-a-list",
    ],
)
def test_unusable_based_on_fails_closed(based_on):
    fields = _base_item(kind="preference", origin="stated")

    compiled = compile_profile(
        "user", _response("user", {"preferences": [fields]}, based_on=based_on)
    )

    assert compiled == ()


@pytest.mark.parametrize("count_field", ["proof_count", "support_count", "source_count"])
def test_an_upstream_supplied_count_is_never_accepted(count_field):
    # The compiler must derive support itself. A raw dict carrying any
    # count-shaped key is rejected outright by `extra="forbid"`, so there
    # is no path by which such a value could be read.
    fields = _base_item(kind="preference", origin="stated")
    fields[count_field] = 9

    compiled = compile_profile("user", _response("user", {"preferences": [fields]}))

    assert compiled == ()


def test_evidence_ids_outside_based_on_do_not_inflate_support():
    strong = _base_item(
        claim="Prefers dark mode.",
        kind="preference",
        origin="stated",
        evidence_ids=["mem-1", "mem-2", "mem-3"],
    )
    padded = _base_item(
        claim="Prefers tabs.",
        kind="preference",
        origin="stated",
        evidence_ids=["mem-4", "ghost-1", "ghost-2", "ghost-3", "ghost-4"],
    )

    compiled = compile_profile(
        "user",
        _response(
            "user",
            {"preferences": [padded, strong]},
            based_on=_grounded("mem-1", "mem-2", "mem-3", "mem-4"),
        ),
    )

    assert [(entry.claim, entry.support_count) for entry in compiled] == [
        ("Prefers dark mode.", 3),
        ("Prefers tabs.", 1),
    ]


# --- Step 3: canonical keys, merging, ordering and displacement -----------


def test_duplicate_claim_keys_merge_with_the_union_of_valid_evidence():
    first = _base_item(kind="preference", origin="stated", evidence_ids=["mem-1", "mem-2"])
    second = _base_item(kind="preference", origin="confirmed", evidence_ids=["mem-2", "mem-3"])

    compiled = compile_profile("user", _response("user", {"preferences": [first, second]}))

    assert len(compiled) == 1
    # mem-2 is shared, so support is the size of the union (3), never the
    # sum of the two individual counts (4).
    assert compiled[0].evidence_ids == ("mem-1", "mem-2", "mem-3")
    assert compiled[0].support_count == 3


def test_duplicate_claim_keys_ignore_whitespace_and_case():
    first = _base_item(
        claim="Run   the Focused tests.", kind="preference", origin="stated", evidence_ids=["mem-1"]
    )
    second = _base_item(
        claim="run the focused tests.", kind="preference", origin="stated", evidence_ids=["mem-2"]
    )

    compiled = compile_profile("user", _response("user", {"preferences": [first, second]}))

    assert len(compiled) == 1
    assert compiled[0].support_count == 2
    # Normalized whitespace, never rephrased: casing is preserved exactly
    # as the representative wrote it.
    assert compiled[0].claim in {"Run the Focused tests.", "run the focused tests."}
    assert "   " not in compiled[0].claim


def test_merge_keeps_the_lowest_kind_rank_member_as_representative():
    # Same category, same claim text, same negative flag -- so the same
    # claim_key -- but two different kinds. The decision (rank 1)
    # represents the merged item, not the convention (rank 2).
    decision = _base_item(
        claim="Uses uv for dependency management.",
        kind="decision",
        origin="stated",
        evidence_ids=["mem-1"],
    )
    convention = _base_item(
        claim="Uses uv for dependency management.",
        kind="convention",
        origin="stated",
        evidence_ids=["mem-2"],
    )

    forward = compile_profile("user", _response("user", {"engineering": [decision, convention]}))
    backward = compile_profile("user", _response("user", {"engineering": [convention, decision]}))

    assert len(forward) == 1
    assert forward[0].representative.kind == "decision"
    assert forward[0].kind_rank == 1
    assert forward == backward


def test_duplicate_claims_in_different_categories_do_not_merge():
    # claim_key includes the category, matching `_reject_duplicates`'s own
    # key, so the same claim filed under two valid categories stays two
    # items.
    fields = _base_item(kind="preference", origin="stated")

    compiled = compile_profile(
        "user", _response("user", {"preferences": [fields], "interaction": [fields]})
    )

    assert sorted(entry.category for entry in compiled) == ["interaction", "preferences"]


def test_the_negative_flag_is_part_of_the_claim_key():
    # Project categories place no restriction on `negative`, so the same
    # claim text can appear twice under one category with opposite flags.
    # They are different claims and must not merge.
    positive = _base_item(
        claim="Squash-merge to main.", kind="convention", origin="stated", evidence_ids=["mem-1"]
    )
    negative = _base_item(
        claim="Squash-merge to main.",
        kind="convention",
        origin="stated",
        negative=True,
        evidence_ids=["mem-2"],
    )

    compiled = compile_profile(
        "project", _response("project", {"conventions": [positive, negative]})
    )

    assert len(compiled) == 2
    assert len({entry.claim_key for entry in compiled}) == 2


def test_claim_key_encoding_survives_punctuation_that_could_break_a_naive_encoding():
    # Claim text containing the very characters a hand-rolled delimiter or
    # a JSON-ish encoding would use must still key two distinct claims
    # distinctly.
    first = _base_item(
        claim='Use ", false] verbatim.', kind="convention", origin="stated", evidence_ids=["mem-1"]
    )
    second = _base_item(
        claim='Use ", true] verbatim.', kind="convention", origin="stated", evidence_ids=["mem-2"]
    )

    compiled = compile_profile("project", _response("project", {"conventions": [first, second]}))

    assert len({entry.claim_key for entry in compiled}) == 2


def test_ordering_is_ascending_kind_rank_first():
    gotcha = _gotcha_item(evidence_ids=["mem-1"])
    decision = _base_item(
        claim="Ships behind a feature flag.",
        kind="decision",
        origin="stated",
        evidence_ids=["mem-2"],
    )
    convention = _base_item(
        claim="Names branches feat/<slug>.",
        kind="convention",
        origin="stated",
        evidence_ids=["mem-3"],
    )

    compiled = compile_profile(
        "project",
        _response(
            "project",
            {
                "conventions": [convention],
                "decisions": [decision],
                "gotchas": [gotcha],
            },
        ),
    )

    assert [entry.kind_rank for entry in compiled] == [0, 1, 2]
    assert [entry.representative.kind for entry in compiled] == ["gotcha", "decision", "convention"]


def test_a_single_evidence_gotcha_outranks_a_heavily_supported_convention():
    # kind_rank dominates support_count: risk beats popularity. (The plan
    # says "gotcha displacing a weak preference/convention"; a project
    # document has no preference category, so the convention is the
    # comparison here and the user-scope negative constraint below covers
    # the preference side.)
    gotcha = _gotcha_item(evidence_ids=["mem-1"])
    convention = _base_item(
        claim="Names branches feat/<slug>.",
        kind="convention",
        origin="stated",
        evidence_ids=[f"mem-{index}" for index in range(2, 10)],
    )

    compiled = compile_profile(
        "project", _response("project", {"conventions": [convention], "gotchas": [gotcha]})
    )

    assert [(entry.representative.kind, entry.support_count) for entry in compiled] == [
        ("gotcha", 1),
        ("convention", 8),
    ]


def test_a_negative_constraint_outranks_a_heavily_supported_preference():
    constraint = _base_item(
        claim="Never force-push to main.",
        kind="preference",
        origin="stated",
        negative=True,
        evidence_ids=["mem-1"],
    )
    preference = _base_item(
        claim="Prefers dark mode.",
        kind="preference",
        origin="stated",
        evidence_ids=[f"mem-{index}" for index in range(2, 10)],
    )

    compiled = compile_profile(
        "user",
        _response("user", {"preferences": [preference], "constraints": [constraint]}),
    )

    assert [(entry.kind_rank, entry.support_count) for entry in compiled] == [(0, 1), (3, 8)]
    # The explicit negative flag survives the pipeline intact.
    assert compiled[0].representative.negative is True
    assert compiled[0].category == "constraints"


def test_a_negative_project_item_also_ranks_at_tier_zero():
    # Project categories place no restriction on `negative`, so a negative
    # convention keeps its own topical category and still ranks with the
    # gotchas.
    negative = _base_item(
        claim="Never run migrations directly against prod.",
        kind="convention",
        origin="stated",
        negative=True,
        evidence_ids=["mem-1"],
    )

    compiled = compile_profile("project", _response("project", {"conventions": [negative]}))

    assert compiled[0].kind_rank == 0
    assert compiled[0].category == "conventions"
    assert compiled[0].representative.negative is True


def test_support_count_breaks_a_kind_rank_tie_descending():
    weak = _base_item(
        claim="Prefers dark mode.", kind="preference", origin="stated", evidence_ids=["mem-1"]
    )
    strong = _base_item(
        claim="Prefers tabs.",
        kind="preference",
        origin="stated",
        evidence_ids=["mem-2", "mem-3", "mem-4"],
    )

    compiled = compile_profile("user", _response("user", {"preferences": [weak, strong]}))

    assert [entry.support_count for entry in compiled] == [3, 1]


def test_claim_key_breaks_a_support_count_tie_ascending():
    first = _base_item(
        claim="Prefers dark mode.", kind="preference", origin="stated", evidence_ids=["mem-1"]
    )
    second = _base_item(
        claim="Prefers tabs.", kind="preference", origin="stated", evidence_ids=["mem-2"]
    )

    forward = compile_profile("user", _response("user", {"preferences": [first, second]}))
    backward = compile_profile("user", _response("user", {"preferences": [second, first]}))

    keys = [entry.claim_key for entry in forward]
    assert keys == sorted(keys)
    assert [entry.kind_rank for entry in forward] == [3, 3]
    assert [entry.support_count for entry in forward] == [1, 1]
    # Nothing about the delivered order depends on the upstream array.
    assert forward == backward


def test_ordering_is_stable_under_permuted_upstream_arrays_and_categories():
    gotcha = _gotcha_item(evidence_ids=["mem-1"])
    decision = _base_item(
        claim="Ships behind a feature flag.",
        kind="decision",
        origin="stated",
        evidence_ids=["mem-2", "mem-3"],
    )
    other_decision = _base_item(
        claim="Stores sessions in Postgres.",
        kind="decision",
        origin="confirmed",
        evidence_ids=["mem-4", "mem-5"],
    )
    convention = _base_item(
        claim="Names branches feat/<slug>.",
        kind="convention",
        origin="observed",
        evidence_ids=["mem-6"],
    )
    other_convention = _base_item(
        claim="Keeps line length at 100.",
        kind="convention",
        origin="stated",
        evidence_ids=["mem-7"],
    )
    based_on = _grounded(*[f"mem-{index}" for index in range(1, 8)])

    forward = compile_profile(
        "project",
        _response(
            "project",
            {
                "gotchas": [gotcha],
                "decisions": [decision, other_decision],
                "conventions": [convention, other_convention],
            },
            based_on=based_on,
        ),
    )
    permuted = compile_profile(
        "project",
        _response(
            "project",
            {
                "conventions": [other_convention, convention],
                "gotchas": [gotcha],
                "decisions": [other_decision, decision],
            },
            based_on=based_on,
        ),
    )

    assert len(forward) == 5
    assert forward == permuted


def _claim_key_of(scope: str, category: str, fields: dict, based_on: object) -> str:
    """The compiler's own key for one item, learned by compiling that item
    alone -- so the budget tests below can predict which items survive
    without reimplementing the hash."""
    compiled = compile_profile(scope, _response(scope, {category: [fields]}, based_on=based_on))
    return compiled[0].claim_key


def test_user_profile_truncates_to_fifteen_by_the_declared_order():
    items = [
        _base_item(
            claim=f"Preference number {index}.",
            kind="preference",
            origin="stated",
            evidence_ids=[f"mem-{index}"],
        )
        for index in range(16)
    ]
    based_on = _grounded(*[f"mem-{index}" for index in range(16)])

    compiled = compile_profile("user", _response("user", {"preferences": items}, based_on=based_on))

    assert len(compiled) == USER_PROFILE_BUDGET
    # Every candidate ties on kind_rank (3) and support_count (1), so the
    # 15 survivors are exactly the 15 lowest claim_keys.
    all_keys = sorted(
        _claim_key_of("user", "preferences", fields, based_on) for fields in items
    )
    assert [entry.claim_key for entry in compiled] == all_keys[:USER_PROFILE_BUDGET]


def test_project_profile_truncates_to_twenty_five_by_the_declared_order():
    items = [
        _base_item(
            claim=f"Convention number {index}.",
            kind="convention",
            origin="stated",
            evidence_ids=[f"mem-{index}"],
        )
        for index in range(26)
    ]
    based_on = _grounded(*[f"mem-{index}" for index in range(26)])

    compiled = compile_profile(
        "project", _response("project", {"conventions": items}, based_on=based_on)
    )

    assert len(compiled) == PROJECT_PROFILE_BUDGET
    all_keys = sorted(
        _claim_key_of("project", "conventions", fields, based_on) for fields in items
    )
    assert [entry.claim_key for entry in compiled] == all_keys[:PROJECT_PROFILE_BUDGET]


def test_the_budget_is_a_total_across_categories():
    preferences = [
        _base_item(
            claim=f"Preference number {index}.",
            kind="preference",
            origin="stated",
            evidence_ids=[f"mem-{index}"],
        )
        for index in range(10)
    ]
    interaction = [
        _base_item(
            claim=f"Interaction convention number {index}.",
            kind="convention",
            origin="stated",
            evidence_ids=[f"other-{index}"],
        )
        for index in range(10)
    ]
    based_on = _grounded(
        *[f"mem-{index}" for index in range(10)], *[f"other-{index}" for index in range(10)]
    )

    compiled = compile_profile(
        "user",
        _response(
            "user", {"preferences": preferences, "interaction": interaction}, based_on=based_on
        ),
    )

    assert len(compiled) == USER_PROFILE_BUDGET
    # Conventions (rank 2) displace every preference (rank 3): all ten
    # conventions survive and only five preferences fit behind them.
    assert sum(1 for entry in compiled if entry.representative.kind == "convention") == 10
    assert sum(1 for entry in compiled if entry.representative.kind == "preference") == 5


def test_a_higher_ranked_new_item_displaces_the_current_last_item():
    # The 15 incumbents are all preferences; a gotcha-tier negative
    # constraint arriving as the 16th enters at the top and pushes the
    # last-ranked incumbent out.
    incumbents = [
        _base_item(
            claim=f"Preference number {index}.",
            kind="preference",
            origin="stated",
            evidence_ids=[f"mem-{index}"],
        )
        for index in range(15)
    ]
    based_on = _grounded(*[f"mem-{index}" for index in range(15)], "mem-new")

    before = compile_profile(
        "user", _response("user", {"preferences": incumbents}, based_on=based_on)
    )
    assert len(before) == USER_PROFILE_BUDGET

    newcomer = _base_item(
        claim="Never force-push to main.",
        kind="preference",
        origin="stated",
        negative=True,
        evidence_ids=["mem-new"],
    )
    after = compile_profile(
        "user",
        _response(
            "user", {"preferences": incumbents, "constraints": [newcomer]}, based_on=based_on
        ),
    )

    assert len(after) == USER_PROFILE_BUDGET
    assert after[0].claim == "Never force-push to main."
    # Exactly one incumbent -- the last-ranked one -- was displaced.
    displaced = {entry.claim_key for entry in before} - {entry.claim_key for entry in after}
    assert displaced == {before[-1].claim_key}


# --- superseded history ----------------------------------------------------
#
# The compiler only ever sees one `reflect_response` snapshot; it holds no
# prior state to diff against, so it cannot detect supersession on its own.
# Two mechanisms it does own keep superseded material out of the delivered
# profile, and both are pinned below:
#
# 1. a superseded item and its correction that share a claim_key collapse
#    into one delivered item, so no "we used to think X, now Y" pair is
#    ever rendered;
# 2. an item grounded only in memories that are no longer in this
#    reflection's `based_on` set is dropped outright.
#
# A correction that genuinely rewrites the claim text produces a different
# claim_key and is therefore a synthesis-quality question, answered by the
# source query's "current truth, not history" mission (Task 3) and measured
# by the evaluator gate (Task 7) -- deliberately not by a supersession
# mechanism invented here. The last test states that boundary explicitly.


def test_a_correction_sharing_a_claim_key_collapses_into_one_current_item():
    superseded = _base_item(
        claim="Deploys run from the release branch.",
        kind="decision",
        origin="stated",
        evidence_ids=["mem-old"],
    )
    correction = _base_item(
        claim="Deploys run from the release branch.",
        kind="decision",
        origin="confirmed",
        evidence_ids=["mem-new"],
    )

    compiled = compile_profile(
        "project",
        _response(
            "project",
            {"decisions": [superseded, correction]},
            based_on=_grounded("mem-old", "mem-new"),
        ),
    )

    assert len(compiled) == 1
    assert compiled[0].evidence_ids == ("mem-new", "mem-old")


def test_an_item_grounded_only_in_evidence_outside_this_reflection_is_dropped():
    stale = _base_item(
        claim="Deploys run from the release branch.",
        kind="decision",
        origin="stated",
        evidence_ids=["mem-old"],
    )
    current = _base_item(
        claim="Deploys run from a tagged commit.",
        kind="decision",
        origin="confirmed",
        evidence_ids=["mem-new"],
    )

    compiled = compile_profile(
        "project",
        _response("project", {"decisions": [stale, current]}, based_on=_grounded("mem-new")),
    )

    assert [entry.claim for entry in compiled] == ["Deploys run from a tagged commit."]


def test_a_rewritten_correction_is_left_to_synthesis_not_to_the_compiler():
    # Documented boundary, not a defect: "always X" superseded by "never X"
    # differs in both claim text and negative flag, so the two have
    # different claim_keys and both are delivered. Detecting that one
    # supersedes the other is the synthesis mission's job (Task 3) and the
    # evaluator's gate (Task 7); the compiler must not guess.
    old = _base_item(
        claim="Always squash-merge to main.",
        kind="convention",
        origin="stated",
        evidence_ids=["mem-old"],
    )
    new = _base_item(
        claim="Never squash-merge to main.",
        kind="convention",
        origin="confirmed",
        negative=True,
        evidence_ids=["mem-new"],
    )

    compiled = compile_profile(
        "project",
        _response("project", {"conventions": [old, new]}, based_on=_grounded("mem-old", "mem-new")),
    )

    assert len(compiled) == 2


# --- malformed and missing structured output fail closed ------------------


@pytest.mark.parametrize(
    "reflect_response",
    [
        {},
        {"based_on": {"memories": [{"id": "mem-1"}]}},
        {"structured_output": None, "based_on": {"memories": [{"id": "mem-1"}]}},
        {"structured_output": "{}", "based_on": {"memories": [{"id": "mem-1"}]}},
        {"structured_output": {}, "based_on": {"memories": [{"id": "mem-1"}]}},
        {"structured_output": {"user_profile": None}, "based_on": {"memories": [{"id": "mem-1"}]}},
        {"structured_output": {"user_profile": []}, "based_on": {"memories": [{"id": "mem-1"}]}},
        None,
        "structured_output",
    ],
    ids=[
        "empty-response",
        "no-structured-output",
        "null-structured-output",
        "structured-output-as-text",
        "no-root-key",
        "null-document",
        "document-as-a-list",
        "response-is-null",
        "response-is-a-string",
    ],
)
def test_unusable_structured_output_fails_closed(reflect_response):
    assert compile_profile("user", reflect_response) == ()


def test_the_other_scopes_root_key_is_never_read():
    fields = _base_item(kind="convention", origin="stated")
    response = _response("project", {"conventions": [fields]})

    assert compile_profile("user", response) == ()
    assert len(compile_profile("project", response)) == 1


def test_a_category_that_is_not_a_list_fails_closed_without_taking_the_rest():
    good = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile(
        "user",
        _response(
            "user",
            {"preferences": [good], "interaction": {"claim": "not a list"}},
            based_on=_grounded("mem-1"),
        ),
    )

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


def test_an_invented_category_key_contributes_nothing():
    good = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")
    smuggled = _base_item(claim="Smuggled.", kind="preference", origin="stated")

    compiled = compile_profile(
        "user",
        _response(
            "user",
            {"preferences": [good], "working_state": [smuggled]},
            based_on=_grounded("mem-1"),
        ),
    )

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


@pytest.mark.parametrize(
    "raw_item",
    [None, "a claim", 7, ["a claim"], {}],
    ids=["null", "string", "number", "list", "empty"],
)
def test_a_non_item_entry_drops_without_taking_the_rest(raw_item):
    good = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile(
        "user",
        _response("user", {"preferences": [raw_item, good]}, based_on=_grounded("mem-1")),
    )

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


# ---------------------------------------------------------------------------
# Step 4: curated atomicity cases
#
# These pin the boundary between what code enforces and what only a
# semantic evaluator can catch (plan: "Semantic packing cannot be proven by
# JSON Schema alone ... Do not add a brittle blanket ban on words such as
# `and`"). Structural violations fail closed here; semantic failures pass
# through untouched on purpose and are the evaluator gate's business
# (Task 7), never a second classifier bolted onto this compiler.
# ---------------------------------------------------------------------------


def test_packed_independent_claims_pass_through_unchanged():
    # Structurally impeccable, semantically two prescriptions in one item.
    # The compiler deliberately does not try to catch this.
    packed = _base_item(
        claim="Run tests before committing and use tabs not spaces.",
        kind="convention",
        origin="stated",
    )

    compiled = compile_profile("user", _response("user", {"interaction": [packed]}))

    assert compiled[0].claim == "Run tests before committing and use tabs not spaces."


def test_a_valid_gotcha_cause_may_contain_the_word_and():
    # The negative of the rule above: no blanket word ban exists, so a
    # legitimate multi-part cause survives.
    gotcha = _gotcha_item(cause="The migration ran twice and left orphaned rows.")

    compiled = compile_profile("project", _response("project", {"gotchas": [gotcha]}))

    assert compiled[0].representative.cause == "The migration ran twice and left orphaned rows."


def test_list_like_claim_text_passes_through_unchanged():
    listed = _base_item(claim="1. Do X 2. Do Y", kind="convention", origin="stated")

    compiled = compile_profile("user", _response("user", {"interaction": [listed]}))

    assert compiled[0].claim == "1. Do X 2. Do Y"


def test_an_actual_list_payload_in_a_claim_is_dropped():
    # A real type mismatch, not text that merely looks like a list: this is
    # a structural violation and fails closed.
    injected = _base_item(claim=["Do X", "Do Y"], kind="preference", origin="stated")
    good = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile("user", _response("user", {"preferences": [injected, good]}))

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


@pytest.mark.parametrize("payload", ["Do X.\nDo Y.", "Do X.\r\nDo Y.", "Do X.\tDo Y."])
def test_multiline_or_control_character_claims_are_dropped(payload):
    multiline = _base_item(claim=payload, kind="convention", origin="stated")

    compiled = compile_profile("user", _response("user", {"interaction": [multiline]}))

    assert compiled == ()


def test_a_cheap_technical_fact_is_dropped():
    technical = _base_item(claim="The API runs on port 8000.", kind="technical_claim")
    good = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile("user", _response("user", {"preferences": [technical, good]}))

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


def test_an_inferred_preference_is_dropped():
    inferred = _base_item(claim="Probably prefers vim.", kind="preference", origin="inferred")
    good = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile("user", _response("user", {"preferences": [inferred, good]}))

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]


@pytest.mark.parametrize(
    "field_name", ["current_task", "next_steps", "working_state", "blockers", "last_activity_at"]
)
def test_current_task_material_smuggled_onto_an_item_is_dropped(field_name):
    smuggled = _base_item(claim="Prefers tabs.", kind="preference", origin="stated")
    smuggled[field_name] = "Finishing the profile compiler."
    good = _base_item(claim="Prefers dark mode.", kind="preference", origin="stated")

    compiled = compile_profile("user", _response("user", {"preferences": [smuggled, good]}))

    assert [entry.claim for entry in compiled] == ["Prefers dark mode."]
