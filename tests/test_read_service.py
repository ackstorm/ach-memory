"""Contract tests for `read_models.py`: the closed request/response schemas
and the deterministic view/memory_types -> Hindsight-filter mapping. No Hindsight
client and no `read_context` resolver are exercised here -- this file is
schema-and-pure-function only, matching Task 2's scope. The FastAPI boundary
(Task 4) and the extended Hindsight client (Task 3) get their own files.

One exception, at the end: `history`'s ACH provenance/curation half (QA
F-13/F-14) is read from ACH's own rows, so those tests seed a database and
stub the Hindsight client.
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
    ["git_locator", "tag_groups", "tags_match", "bank_id", "tenant_id", "temporal_anchor"],
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


# -- RecallRequest: view and memory_types are closed enums


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
        memory_types=["preference", "constraint", "decision", "convention", "fact", "gotcha"],
    )
    assert set(request.memory_types) == {
        "preference",
        "constraint",
        "decision",
        "convention",
        "fact",
        "gotcha",
    }


def test_an_undocumented_kind_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(scope="user", query="q", memory_types=["urgent"])


def test_more_memory_types_than_exist_is_rejected():
    with pytest.raises(ValidationError):
        RecallRequest(
            scope="user",
            query="q",
            memory_types=[
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
    hit = _hit(memory_type="decision", basis="agent_verified")
    assert hit.memory_type == "decision"
    assert hit.basis == "agent_verified"


@pytest.mark.parametrize("old_name", ["kind", "origin"])
def test_a_hit_no_longer_answers_to_the_pre_release_names(old_name):
    """QA F-17: the response names are `retain`'s input names. The old ones
    are gone, not aliased, so a caller cannot keep reading them by accident."""
    with pytest.raises(ValidationError):
        _hit(**{old_name: "decision"})


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
    assert filters.tag_groups == (
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
    )


@pytest.mark.parametrize("view", ["current", "evidence", "all"])
def test_every_documented_view_resolves_to_the_same_v040_filter(view):
    """The views share one exact retained corpus in v0.4.0."""
    assert resolve_filters(view, None) == resolve_filters("current", None)


def test_several_memory_types_are_ored_against_each_other():
    """A memory carries exactly one `type:` tag, so ANDing two of them can
    never match. Flat `all_strict` over both is what made a two-type recall
    return nothing at all, on the default mode."""
    filters = resolve_filters("current", ("decision", "gotcha"))
    assert filters.tag_groups == (
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
        {"tags": ["type:decision", "type:gotcha"], "match": "any_strict"},
    )


def test_no_memory_types_means_only_the_schema_group():
    filters = resolve_filters("current", None)
    assert filters.tag_groups == (
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
    )


def test_caller_tags_form_their_own_group_after_the_server_groups():
    """Groups are ANDed, so a caller tag narrows the schema-scoped set and
    never excludes a memory for carrying more tags."""
    filters = resolve_filters("current", None, caller_tags=("repo:group/app",))
    assert filters.tag_groups == (
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
        {"tags": ["repo:group/app"], "match": "all_strict"},
    )


def test_every_group_keeps_its_own_match_mode():
    """Why this is three groups and not one flat list. Flat, one mode had to
    serve every axis: ORing swept in `schema:ach-retain-v1`, which every ACH
    memory carries, so a request to NARROW returned the whole corpus; ANDing
    joined the `type:` tags, and a memory carries exactly one, so asking for
    two kinds could never match anything. Grouped, each axis gets the mode it
    needs -- and the caller's own tags are ANDed, with no mode to loosen
    them."""
    filters = resolve_filters(
        "current", ("decision", "convention"), caller_tags=("repo:group/app", "area:auth")
    )
    assert filters.tag_groups == (
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
        {"tags": ["type:decision", "type:convention"], "match": "any_strict"},
        {"tags": ["repo:group/app", "area:auth"], "match": "all_strict"},
    )


def test_caller_tags_come_after_the_memory_type_group():
    filters = resolve_filters("current", ("decision",), caller_tags=("repo:group/app",))
    assert filters.tag_groups == (
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
        {"tags": ["type:decision"], "match": "any_strict"},
        {"tags": ["repo:group/app"], "match": "all_strict"},
    )


def test_no_caller_tags_means_the_schema_tag_is_unchanged():
    assert resolve_filters("current", None) == resolve_filters("current", None, caller_tags=())


def test_recall_request_normalizes_caller_tags_the_same_way_retain_does():
    """The symmetry that makes the convention work at all: normalisation
    happens once, on the request model, so `resolve_filters` never needs to
    (and never re-validates) what it is handed."""
    request = RecallRequest(scope="user", query="q", tags_filter=[" Repo:Group/App "])
    assert request.tags_filter == ("repo:group/app",)


def test_resolve_filters_never_lets_a_caller_choose_tag_syntax_directly():
    """There is no parameter here through which a `RecallRequest` value ever
    reaches Hindsight's own tag/filter DSL: `resolve_filters` only accepts a
    closed `view`, a closed `memory_types` and already-normalised
    `caller_tags` (`RecallRequest.tags_filter`, validated by
    `memory.tags.normalize_caller_tags` before it ever reaches here) -- never
    raw `tags_match` syntax, and never a match mode either, now that every
    group's mode is server-owned. It only ever emits the fixed
    `schema:ach-retain-v1`/`type:<memory_type>`/caller-tag shapes."""
    import inspect

    signature = inspect.signature(resolve_filters)
    assert set(signature.parameters) == {"view", "memory_types", "caller_tags"}


# -- history: ACH provenance and curation events (QA F-13/F-14). The one
# section here that touches the database and a (stubbed) Hindsight client:
# what `memory_history` adds over Hindsight's own revision list is read from
# ACH's rows, so there is nothing pure to test it against.


def _provenance(**overrides) -> read_models.Provenance:
    fields = {
        "record_id": "rec-1",
        "basis": "human_explicit",
        "trigger": "user_requested",
        "recorded_at": "2026-08-01T00:00:00+00:00",
        "lifecycle": "active",
    }
    fields.update(overrides)
    return read_models.Provenance(**fields)


def _event(n=0, **overrides) -> read_models.CurationEvent:
    fields = {
        "operation_id": f"op-{n}",
        "action": "forget",
        "state": "completed",
        "created_at": "2026-08-02T00:00:00+00:00",
    }
    fields.update(overrides)
    return read_models.CurationEvent(**fields)


def test_build_history_response_charges_provenance_and_curation_before_changes():
    """Provenance is what the caller asked `memory_history` for; Hindsight's
    consolidation edits are the tail. A budget that only fits one of them
    keeps the provenance and drops the change, never the other way round."""
    huge = "x" * (read_models.MAX_HIT_TEXT_LENGTH - 1)
    changes = [_change(text=huge, n=n) for n in range(read_models.MAX_HISTORY_CHANGES)]
    curation = [_event(n=n, desired_content=huge) for n in range(4)]

    response = build_history_response(
        project_slug=None,
        resolved_from=None,
        memory_id="mem-1",
        current=CurrentFact(text="current", state="valid"),
        changes=changes,
        provenance=_provenance(),
        curation=curation,
    )

    assert response.provenance is not None
    assert len(response.curation) == len(curation)
    assert 0 < len(response.changes) < len(changes)
    assert response.truncated is True


def test_build_history_response_drops_whole_curation_events_past_the_count_cap():
    curation = [_event(n=n) for n in range(read_models.MAX_CURATION_EVENTS + 1)]
    response = build_history_response(
        project_slug=None,
        resolved_from=None,
        memory_id="mem-1",
        current=CurrentFact(text="current", state="valid"),
        changes=[],
        curation=curation,
    )
    assert len(response.curation) == read_models.MAX_CURATION_EVENTS
    assert response.truncated is True


@pytest.fixture
def owner(session, tenant):
    from memory import ids
    from memory.auth.principal import Principal
    from memory.models import User

    user = User(id="usr_history", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return Principal(
        tenant_id=tenant, user_id=user.id, credential_id="ext_history", subject="history@test"
    )


@pytest.fixture
def hindsight(monkeypatch):
    from unittest.mock import create_autospec

    from memory import read_service
    from memory.hindsight.client import HindsightClient

    client = create_autospec(HindsightClient, instance=True)
    client.get_memory.return_value = {"text": "current", "state": "valid"}
    client.get_memory_history.return_value = []
    monkeypatch.setattr(read_service, "get_client", lambda: client)
    return client


def _seed_record(session, owner, memory_id: str):
    from datetime import UTC, datetime
    from uuid import uuid4

    from memory.models import CurationOperation, RetainedRecord, User

    bank_id = session.get(User, owner.user_id).bank_id
    record = RetainedRecord(
        tenant_id=owner.tenant_id,
        scope="user",
        user_id=owner.user_id,
        project_internal_id=None,
        operation_id=str(uuid4()),
        payload_hash="h" * 64,
        document_id=f"ach-retain-{uuid4().hex}",
        source_memory_id=memory_id,
        canonical_content="Deploys need two approvals.",
        memory_type="constraint",
        basis="human_explicit",
        caller_tags=["repo:x"],
        trigger="user_requested",
        sanitized_evidence=[
            {"kind": "user_quote", "raw": "two approvals", "source_ref": None},
            {"kind": "tool_result", "raw": "CODEOWNERS", "source_ref": "git:CODEOWNERS"},
        ],
        valid_until=datetime(2027, 1, 1, tzinfo=UTC),
        lifecycle="forgotten",
        upstream_state="completed",
        created_by_credential="ext_history",
    )
    session.add(record)
    session.flush()
    session.add(
        CurationOperation(
            operation_id="ach-curate-stale",
            retained_record_id=record.id,
            tenant_id=owner.tenant_id,
            scope="user",
            user_id=owner.user_id,
            project_internal_id=None,
            action="forget",
            state="completed",
            reason="stale",
        )
    )
    session.flush()
    return bank_id


def test_history_carries_the_retained_record_provenance_and_curation(
    session, owner, hindsight
):
    from memory import read_service

    memory_id = "33333333-3333-3333-3333-333333333333"
    _seed_record(session, owner, memory_id)

    response = read_service.history(
        session, owner, None, HistoryRequest(scope="user", memory_id=memory_id)
    )

    assert response.provenance is not None
    assert response.provenance.basis == "human_explicit"
    assert response.provenance.trigger == "user_requested"
    assert [e.kind for e in response.provenance.evidence] == ["user_quote", "tool_result"]
    assert response.provenance.valid_until is not None
    assert response.provenance.tags == ("repo:x",)
    assert response.provenance.lifecycle == "forgotten"
    assert [(c.action, c.state, c.reason) for c in response.curation] == [
        ("forget", "completed", "stale")
    ]


def test_history_of_a_forgotten_memory_still_serves_ach_provenance(session, owner, hindsight):
    """Hindsight's history route 404s once a memory is invalidated although
    get_memory still returns it. A forget is exactly when the reason is
    wanted, so the upstream 404 means "no revisions", not "no memory"."""
    from memory import read_service
    from memory.errors import MemoryNotFound

    memory_id = "55555555-5555-5555-5555-555555555555"
    _seed_record(session, owner, memory_id)
    hindsight.get_memory.return_value = {"text": "current", "state": "invalidated"}
    hindsight.get_memory_history.side_effect = MemoryNotFound("no such object in this memory")

    response = read_service.history(
        session, owner, None, HistoryRequest(scope="user", memory_id=memory_id)
    )

    assert response.current.state == "invalidated"
    assert response.changes == ()
    assert [(c.action, c.reason) for c in response.curation] == [("forget", "stale")]


def test_history_of_an_untracked_memory_has_no_provenance(session, owner, hindsight):
    """A memory Hindsight derived on its own, or one retained before typed
    retain, has no ACH row: absent provenance, not an invented one."""
    from memory import read_service

    response = read_service.history(
        session,
        owner,
        None,
        HistoryRequest(scope="user", memory_id="44444444-4444-4444-4444-444444444444"),
    )

    assert response.provenance is None
    assert response.curation == ()
