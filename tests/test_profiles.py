"""Task 1: the closed user/project structured-profile response schemas
(SPEC §4.2, §6.4, §10-§13, §17; plan "Structured contracts").

This file only exercises schema shape and structural/cross-field
validation on `memory.profiles`. It does not exercise Hindsight queries,
ranking/displacement or rendering -- those belong to later Phase 4 tasks
and their own test additions to this file.
"""

import hashlib
import json

import pytest
from pydantic import ValidationError

from memory.profiles import (
    PROJECT_PROFILE_BUDGET,
    USER_PROFILE_BUDGET,
    ProfileItem,
    ProjectProfileDocument,
    UserProfileDocument,
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
