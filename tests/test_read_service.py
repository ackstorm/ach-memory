"""Contract tests for `read_models.py`: the closed request/response schemas
and the deterministic view/kinds -> Hindsight-filter mapping. No Hindsight
client and no `read_context` resolver are exercised here -- this file is
schema-and-pure-function only, matching Task 2's scope. The FastAPI boundary
(Task 4) and the extended Hindsight client (Task 3) get their own files.
"""

import pytest
from pydantic import ValidationError

from memory import read_models
from memory.read_models import (
    CurrentFact,
    HistoryChange,
    HistoryRequest,
    HistoryResponse,
    RecallHit,
    RecallRequest,
    RecallResponse,
    SourceFact,
    build_history_response,
    build_recall_response,
    resolve_filters,
)


def _hit(memory_id="mem-1", text="A claim.", **overrides) -> RecallHit:
    fields = {
        "memory_id": memory_id,
        "text": text,
        "fact_type": "observation",
        "state": "valid",
    }
    fields.update(overrides)
    return RecallHit(**fields)


# -- RecallRequest: query


def test_query_must_be_present():
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", query="")


def test_query_of_only_whitespace_is_blank():
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", query="   ")


def test_query_over_the_length_ceiling_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", query="x" * (read_models.MAX_QUERY_LENGTH + 1))


def test_query_at_the_length_ceiling_is_accepted():
    request = RecallRequest(scope="user", query="x" * read_models.MAX_QUERY_LENGTH)
    assert len(request.query) == read_models.MAX_QUERY_LENGTH


# -- RecallRequest: max_results


@pytest.mark.parametrize("value", [0, -1, read_models.MAX_RESULTS_CEILING + 1])
def test_max_results_outside_1_to_20_is_rejected(value):
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", query="q", max_results=value)


@pytest.mark.parametrize("value", [1, 10, read_models.MAX_RESULTS_CEILING])
def test_max_results_inside_1_to_20_is_accepted(value):
    request = RecallRequest(scope="user", query="q", max_results=value)
    assert request.max_results == value


def test_max_results_defaults_to_ten():
    assert RecallRequest(scope="user", query="q").max_results == 10


# -- Scope fields: exact set, no git_locator, no raw Hindsight vocabulary


def test_scope_is_required():
    with pytest.raises(ValidationError):
        RecallRequest(query="q")


@pytest.mark.parametrize(
    "extra_field",
    ["git_locator", "tags", "tag_groups", "bank_id", "tenant_id", "temporal_anchor"],
)
def test_a_recall_request_rejects_every_unknown_field(extra_field):
    with pytest.raises(ValidationError):
        RecallRequest(scope="project", project_slug="acme", query="q", **{extra_field: "x"})


@pytest.mark.parametrize(
    "extra_field", ["git_locator", "tags", "bank_id", "tenant_id"]
)
def test_a_history_request_rejects_every_unknown_field(extra_field):
    with pytest.raises(ValidationError):
        HistoryRequest(
            scope="project", project_slug="acme", memory_id="mem-1", **{extra_field: "x"}
        )


def test_project_slug_over_the_column_width_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(
            scope="project",
            project_slug="x" * (read_models.MAX_PROJECT_SLUG_LENGTH + 1),
            query="q",
        )


def test_a_control_character_in_project_slug_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(scope="project", project_slug="acme\x00", query="q")


def test_a_control_character_in_user_id_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", user_id="usr_\x01", query="q")


def test_a_control_character_in_memory_id_is_rejected():
    with pytest.raises(ValidationError):
        HistoryRequest(scope="user", memory_id="mem\x1f")


def test_user_id_is_accepted_at_the_schema_layer_for_delegated_master_reads():
    """The schema stays permissive on purpose: whether `user_id` is actually
    allowed for THIS caller is `read_context.resolve_read_bank`'s job (it
    already enforces master-only delegation via `banks.resolve_user_bank`),
    not something a request model -- which never sees the authenticated
    principal -- could check."""
    request = RecallRequest(scope="user", user_id="usr_target", query="q")
    assert request.user_id == "usr_target"


# -- RecallRequest: view and kinds are closed enums


def test_view_defaults_to_current():
    assert RecallRequest(scope="user", query="q").view == "current"


@pytest.mark.parametrize("view", ["current", "evidence", "all"])
def test_every_documented_view_is_accepted(view):
    assert RecallRequest(scope="user", query="q", view=view).view == view


def test_an_undocumented_view_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", query="q", view="everything")


def test_every_memory_type_is_an_accepted_recall_kind():
    request = RecallRequest(
        scope="user",
        query="q",
        kinds=["preference", "constraint", "decision", "convention", "fact", "gotcha"],
    )
    assert set(request.kinds) == {
        "preference",
        "constraint",
        "decision",
        "convention",
        "fact",
        "gotcha",
    }


def test_an_undocumented_kind_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", query="q", kinds=["urgent"])


def test_more_kinds_than_exist_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(
            scope="user",
            query="q",
            kinds=[
                "preference",
                "constraint",
                "decision",
                "convention",
                "fact",
                "gotcha",
                "preference",
            ],
        )


# -- RecallHit / RecallResponse: whitelisted fields only


def test_a_hit_with_only_whitelisted_fields_constructs():
    hit = _hit(kind="decision", origin="agent_verified")
    assert hit.kind == "decision"


@pytest.mark.parametrize(
    "extra_field", ["bank_id", "tenant_id", "tags", "tag_groups", "embedding", "chunks"]
)
def test_a_hit_rejects_every_undocumented_field(extra_field):
    with pytest.raises(ValidationError):
        _hit(**{extra_field: "x"})


def test_a_hit_rejects_an_undocumented_fact_type():
    with pytest.raises(ValidationError):
        _hit(fact_type="tool_trace")


def test_a_hit_rejects_an_undocumented_state():
    with pytest.raises(ValidationError):
        _hit(state="superseded")


def test_a_recall_response_rejects_an_undocumented_top_level_field():
    with pytest.raises(ValidationError):
        RecallResponse(project_slug="acme", hits=(), bank_id="bank_should_not_be_here")


# -- Payload caps and explicit truncation: recall


def test_build_recall_response_keeps_hits_under_the_count_cap_untruncated():
    hits = [_hit(memory_id=f"mem-{n}") for n in range(read_models.MAX_HITS)]
    response = build_recall_response(project_slug="acme", resolved_from=None, hits=hits)
    assert len(response.hits) == read_models.MAX_HITS
    assert response.truncated is False


def test_build_recall_response_drops_whole_hits_past_the_count_cap():
    hits = [_hit(memory_id=f"mem-{n}") for n in range(read_models.MAX_HITS + 3)]
    response = build_recall_response(project_slug="acme", resolved_from=None, hits=hits)
    assert len(response.hits) == read_models.MAX_HITS
    assert response.truncated is True


def test_build_recall_response_drops_whole_hits_past_the_byte_budget():
    """One claim per hit, never a fragment: a hit that does not fit is
    dropped whole, and nothing after it is considered either."""
    huge = "x" * (read_models.MAX_HIT_TEXT_LENGTH - 1)
    hits = [_hit(memory_id=f"mem-{n}", text=huge) for n in range(read_models.MAX_HITS)]

    response = build_recall_response(project_slug="acme", resolved_from=None, hits=hits)

    assert 0 < len(response.hits) < len(hits)
    assert response.truncated is True
    for hit in response.hits:
        assert hit.text == huge  # whole, never cut mid-claim


def test_build_recall_response_reports_no_truncation_when_everything_fits():
    response = build_recall_response(
        project_slug="acme", resolved_from=None, hits=[_hit(), _hit(memory_id="mem-2")]
    )
    assert response.truncated is False
    assert len(response.hits) == 2


# -- Payload caps and explicit truncation: history


def _change(text="a change", n=0) -> HistoryChange:
    return HistoryChange(
        text=text,
        valid_from="2026-08-01T00:00:00Z",
        source_facts=(SourceFact(memory_id=f"src-{n}", text="source text"),),
    )


def test_a_history_change_caps_source_facts_at_construction():
    with pytest.raises(ValidationError):
        HistoryChange(
            text="t",
            valid_from="2026-08-01T00:00:00Z",
            source_facts=tuple(
                SourceFact(memory_id=f"src-{n}", text="t")
                for n in range(read_models.MAX_SOURCE_FACTS_PER_CHANGE + 1)
            ),
        )


def test_build_history_response_drops_whole_changes_past_the_count_cap():
    changes = [_change(n=n) for n in range(read_models.MAX_HISTORY_CHANGES + 2)]
    response = build_history_response(
        project_slug="acme",
        resolved_from=None,
        memory_id="mem-1",
        current=CurrentFact(text="current", state="valid"),
        changes=changes,
    )
    assert len(response.changes) == read_models.MAX_HISTORY_CHANGES
    assert response.truncated is True


def test_build_history_response_drops_whole_changes_past_the_byte_budget():
    huge = "x" * (read_models.MAX_HIT_TEXT_LENGTH - 1)
    changes = [_change(text=huge, n=n) for n in range(read_models.MAX_HISTORY_CHANGES)]

    response = build_history_response(
        project_slug="acme",
        resolved_from=None,
        memory_id="mem-1",
        current=CurrentFact(text="current", state="valid"),
        changes=changes,
    )

    assert 0 < len(response.changes) < len(changes)
    assert response.truncated is True
    for change in response.changes:
        assert change.text == huge


def test_a_history_response_rejects_an_undocumented_top_level_field():
    with pytest.raises(ValidationError):
        HistoryResponse(
            memory_id="mem-1",
            current=CurrentFact(text="t", state="valid"),
            bank_id="bank_should_not_be_here",
        )


# -- resolve_filters: deterministic view/memory_types -> Hindsight filter
# mapping (v0.4.0: schema:ach-retain-v1 always)


def test_current_view_always_carries_the_schema_tag_and_no_experience_type():
    filters = resolve_filters("current", None)
    assert set(filters.types) == {"world", "observation"}
    assert filters.tags == ("schema:ach-retain-v1",)
    assert filters.tags_match == "all_strict"


@pytest.mark.parametrize("view", ["current", "evidence", "all"])
def test_every_documented_view_resolves_to_the_same_v040_filter(view):
    """The views share one exact retained corpus in v0.4.0."""
    assert resolve_filters(view, None) == resolve_filters("current", None)


def test_memory_types_become_bounded_type_tags_after_the_schema_tag():
    filters = resolve_filters("current", ("decision", "gotcha"))
    assert filters.tags == ("schema:ach-retain-v1", "type:decision", "type:gotcha")


def test_no_memory_types_means_only_the_schema_tag():
    filters = resolve_filters("current", None)
    assert filters.tags == ("schema:ach-retain-v1",)


def test_resolve_filters_never_lets_a_caller_choose_tag_syntax_directly():
    """There is no parameter here through which a `RecallRequest` value ever
    reaches Hindsight's own tag/filter DSL: `resolve_filters` only accepts a
    closed `view` and closed `memory_types`, and only ever emits the fixed
    `schema:ach-retain-v1`/`type:<memory_type>` shapes."""
    import inspect

    signature = inspect.signature(resolve_filters)
    assert set(signature.parameters) == {"view", "memory_types"}
