from dataclasses import dataclass
from unittest import mock
from uuid import uuid4

import pytest

from memory import ids, model_registry
from memory.builtin_models import USER_CONTEXT
from memory.errors import (
    BuiltinModelImmutable,
    ContextBudgetExceeded,
    CurationNeedsOperator,
    IdempotencyConflict,
    MentalModelNotFound,
    MentalModelQuotaExceeded,
)
from memory.hindsight.client import HindsightClient
from memory.mental_model_service import (
    CustomModelCreateRequest,
    CustomModelUpdateRequest,
    _payload_hash,
    create_custom_model,
    delete_model,
    get_model,
    list_models,
    refresh_model,
    resume_model_mutation,
    update_model,
)
from memory.models import User
from memory.retained_records import LogicalBankRef

REQUIRED_TAGS = ("schema:ach-retain-v1", "validity:indefinite")
# Hindsight 0.9.2 represents a manually refreshed model by omitting its
# trigger. ACH keeps an empty object in its non-null registry column and
# normalizes that object at the client boundary.
TRIGGER = {}


@pytest.fixture
def bank(session, tenant):
    user = User(id="usr_svc", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return LogicalBankRef(tenant, "user", user.id, None, user.bank_id)


@pytest.fixture
def hindsight():
    client = mock.MagicMock(spec=HindsightClient)
    counter = iter(range(1, 1000))
    client.create_mental_model.side_effect = lambda *a, **k: {
        "mental_model_id": f"mm-upstream-{next(counter)}",
        "operation_id": f"op-upstream-{next(counter)}",
    }
    return client


def custom_request(
    *, name="review-context", max_tokens=256, always_in_context=True, operation_id=None
) -> CustomModelCreateRequest:
    return CustomModelCreateRequest(
        name=name,
        source_query="Summarize review conventions.",
        source_tags=REQUIRED_TAGS,
        tags_match="all",
        max_tokens=max_tokens,
        trigger=TRIGGER,
        always_in_context=always_in_context,
        operation_id=operation_id or str(uuid4()),
    )


CUSTOM_REQUEST = CustomModelCreateRequest(
    name="overflow",
    source_query="Summarize overflow.",
    source_tags=REQUIRED_TAGS,
    tags_match="all",
    max_tokens=256,
    trigger=TRIGGER,
    always_in_context=False,
    operation_id=str(uuid4()),
)


@pytest.fixture
def create_request():
    return custom_request(always_in_context=False)


@pytest.fixture
def five_custom_models(session, bank):
    for index in range(5):
        model_registry.register_model(
            session,
            bank,
            origin="user",
            model_key=f"mm_{index:032x}",
            name=f"m{index}",
            source_query="Summarize.",
            source_tags=list(REQUIRED_TAGS),
            tags_match="all",
            max_tokens=256,
            trigger=TRIGGER,
            always_in_context=False,
            delivery_state="ready",
        )
    session.commit()


@pytest.fixture
def user_bank_with_builtin(session, bank):
    definition = USER_CONTEXT
    model_registry.register_model(
        session,
        bank,
        origin="builtin",
        model_key=definition.key,
        name="User context",
        source_query=definition.source_query,
        source_tags=list(definition.source_tags),
        tags_match=definition.tags_match,
        max_tokens=definition.max_tokens,
        trigger=dict(definition.trigger),
        builtin_key=definition.key,
        definition_version=definition.version,
        always_in_context=definition.always_in_context,
        delivery_state="ready",
    )
    session.commit()
    return bank


@dataclass
class PendingRegistration:
    model_key: str
    operation_id: str
    source_query: str
    max_tokens: int
    trigger: dict
    source_tags: tuple[str, ...]

    def exact_upstream(self) -> dict:
        return {
            "id": "mm-resumed-upstream",
            "name": f"ach:{self.model_key}",
            "source_query": self.source_query,
            "max_tokens": self.max_tokens,
            "trigger": {
                "mode": "full",
                "refresh_after_consolidation": False,
                "exclude_mental_models": False,
                "keep_trace": False,
            },
            "tags": list(self.source_tags),
        }


@pytest.fixture
def pending_registration(session, bank):
    operation_id = str(uuid4())
    model_key = ids.new_model_key()
    model_registry.register_model(
        session,
        bank,
        origin="user",
        model_key=model_key,
        name="pending",
        source_query="Summarize pending state.",
        source_tags=list(REQUIRED_TAGS),
        tags_match="all",
        max_tokens=256,
        trigger=TRIGGER,
        lifecycle_state="creating",
        mutation_operation_id=operation_id,
        mutation_payload_hash="unused-in-this-test",
        always_in_context=False,
        delivery_state="ready",
    )
    session.commit()
    return PendingRegistration(
        model_key=model_key,
        operation_id=operation_id,
        source_query="Summarize pending state.",
        max_tokens=256,
        trigger=TRIGGER,
        source_tags=REQUIRED_TAGS,
    )


# ---------------------------------------------------------------------------
# Create: key generation, quota, budget, idempotency
# ---------------------------------------------------------------------------


def test_custom_create_returns_ach_key_and_hides_upstream_id(session, bank, hindsight, create_request):
    result = create_custom_model(session, bank, create_request, client=hindsight)

    assert result.model_key.startswith("mm_")
    assert result.name == create_request.name
    assert hindsight.create_mental_model.call_args.kwargs["name"] == f"ach:{result.model_key}"
    assert not hasattr(result, "upstream_model_id")
    assert not hasattr(result, "bank_id")


def test_custom_create_omits_an_empty_manual_trigger_upstream(
    session, bank, hindsight, create_request
):
    create_custom_model(session, bank, create_request, client=hindsight)

    assert hindsight.create_mental_model.call_args.kwargs["trigger"] is None


def test_custom_create_withholds_generated_content_until_its_operation_completes(
    session, bank, hindsight, create_request
):
    result = create_custom_model(session, bank, create_request, client=hindsight)

    assert result.delivery_state == "withheld"
    assert result.refresh_status == "pending"


def test_sixth_custom_create_is_rejected_before_hindsight(session, bank, hindsight, five_custom_models):
    with pytest.raises(MentalModelQuotaExceeded):
        create_custom_model(session, bank, CUSTOM_REQUEST, client=hindsight)
    hindsight.create_mental_model.assert_not_called()


def test_always_in_context_budget_is_enforced_separately_from_quota(
    session, user_bank_with_builtin, hindsight
):
    for index in range(2):
        create_custom_model(
            session,
            user_bank_with_builtin,
            custom_request(name=f"selected-{index}", max_tokens=256),
            client=hindsight,
        )

    with pytest.raises(ContextBudgetExceeded):
        create_custom_model(
            session,
            user_bank_with_builtin,
            custom_request(name="over-budget", max_tokens=256),
            client=hindsight,
        )


def test_create_retry_with_same_operation_id_and_payload_is_idempotent(session, bank, hindsight):
    request = custom_request(always_in_context=False, operation_id=str(uuid4()))

    first = create_custom_model(session, bank, request, client=hindsight)
    second = create_custom_model(session, bank, request, client=hindsight)

    assert first.model_key == second.model_key
    hindsight.create_mental_model.assert_called_once()


def test_create_retry_with_same_operation_id_and_different_payload_conflicts(session, bank, hindsight):
    operation_id = str(uuid4())
    create_custom_model(
        session, bank, custom_request(name="first", operation_id=operation_id), client=hindsight
    )

    with pytest.raises(IdempotencyConflict):
        create_custom_model(
            session, bank, custom_request(name="different", operation_id=operation_id), client=hindsight
        )


def test_create_retry_resumes_a_still_creating_row_to_active(session, bank, hindsight):
    """The bug this closes: `create_custom_model` found the matching
    `creating` duplicate and just echoed it back verbatim -- a retry after a
    lost create response never actually recovered to `active`."""
    operation_id = "11111111-1111-4111-8111-111111111111"
    request = custom_request(operation_id=operation_id)
    model_key = ids.new_model_key()
    digest = _payload_hash(bank, request)
    model_registry.register_model(
        session, bank, origin="user", model_key=model_key, name=request.name,
        source_query=request.source_query, source_tags=list(request.source_tags),
        tags_match=request.tags_match, max_tokens=request.max_tokens, trigger=request.trigger,
        lifecycle_state="creating", mutation_operation_id=operation_id,
        mutation_payload_hash=digest, always_in_context=request.always_in_context,
        delivery_state="ready",
    )
    session.commit()
    hindsight.list_mental_models.return_value = {
        "items": [
            {
                "id": "mm-resumed-upstream",
                "name": f"ach:{model_key}",
                "source_query": request.source_query,
                "max_tokens": request.max_tokens,
                "trigger": {
                    "mode": "full", "refresh_after_consolidation": False,
                    "exclude_mental_models": False, "keep_trace": False,
                },
                "tags": list(request.source_tags),
            }
        ]
    }

    create_custom_model(session, bank, request, client=hindsight)

    row = model_registry.get_registered_model(session, bank, model_key)
    assert row.lifecycle_state == "active"
    hindsight.create_mental_model.assert_not_called()


def test_create_rejects_a_source_selection_missing_a_required_tag():
    with pytest.raises(ValueError):
        CustomModelCreateRequest(
            name="bad",
            source_query="q",
            source_tags=("schema:ach-retain-v1",),
            tags_match="all",
            max_tokens=256,
            trigger=TRIGGER,
            always_in_context=False,
            operation_id=str(uuid4()),
        )


def test_create_rejects_the_nonexistent_manual_trigger_mode():
    with pytest.raises(ValueError, match="trigger mode"):
        CustomModelCreateRequest(
            name="bad-trigger",
            source_query="q",
            source_tags=REQUIRED_TAGS,
            tags_match="all",
            max_tokens=256,
            trigger={"mode": "manual"},
            always_in_context=False,
            operation_id=str(uuid4()),
        )


# ---------------------------------------------------------------------------
# Resume: crash recovery for a lost create response
# ---------------------------------------------------------------------------


def test_create_retry_adopts_only_exact_matching_upstream_model(session, bank, hindsight, pending_registration):
    hindsight.list_mental_models.return_value = {"items": [pending_registration.exact_upstream()]}

    result = resume_model_mutation(session, bank, pending_registration.operation_id, client=hindsight)

    assert result.model_key == pending_registration.model_key
    hindsight.create_mental_model.assert_not_called()


def test_resume_retries_creation_when_no_upstream_model_exists(session, bank, hindsight, pending_registration):
    hindsight.list_mental_models.return_value = {"items": []}
    hindsight.create_mental_model.return_value = {
        "mental_model_id": "mm-freshly-created",
        "operation_id": "op-freshly-created",
    }

    result = resume_model_mutation(session, bank, pending_registration.operation_id, client=hindsight)

    assert result.model_key == pending_registration.model_key
    hindsight.create_mental_model.assert_called_once()


def test_resume_requires_operator_action_on_multiple_matches(session, bank, hindsight, pending_registration):
    upstream = pending_registration.exact_upstream()
    hindsight.list_mental_models.return_value = {"items": [upstream, {**upstream, "id": "mm-other"}]}

    with pytest.raises(CurationNeedsOperator):
        resume_model_mutation(session, bank, pending_registration.operation_id, client=hindsight)
    hindsight.create_mental_model.assert_not_called()


# ---------------------------------------------------------------------------
# List / get: registry metadata plus unknown-upstream inventory
# ---------------------------------------------------------------------------


def test_list_models_reports_unknown_upstream_count_without_adopting_it(
    session, bank, hindsight, create_request
):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.list_mental_models.return_value = {
        "items": [
            {"id": "mm-upstream-1", "name": f"ach:{created.model_key}"},
            {"id": "mm-unknown", "name": "some-legacy-model"},
        ]
    }

    result = list_models(session, bank, client=hindsight)

    assert [m.model_key for m in result.models] == [created.model_key]
    assert result.unknown_upstream_count == 1


def test_get_model_raises_for_unknown_key(session, bank):
    with pytest.raises(MentalModelNotFound):
        get_model(session, bank, "mm_does_not_exist")


# ---------------------------------------------------------------------------
# Update / delete / refresh
# ---------------------------------------------------------------------------


def test_update_changes_source_query_and_forwards_it_upstream(session, bank, hindsight, create_request):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-update-refresh"}

    updated = update_model(
        session,
        bank,
        created.model_key,
        CustomModelUpdateRequest(source_query="Summarize new conventions.", operation_id=str(uuid4())),
        client=hindsight,
    )

    assert updated.source_query == "Summarize new conventions."
    assert updated.model_key == created.model_key
    hindsight.update_mental_model.assert_called_once_with(
        bank.bank_id, "mm-upstream-1", source_query="Summarize new conventions."
    )
    assert updated.delivery_state == "withheld"
    assert updated.refresh_status == "pending"
    row = model_registry.get_registered_model(session, bank, created.model_key)
    assert row.refresh_operation_id == "op-update-refresh"


def test_update_name_only_does_not_withhold_or_refresh(session, bank, hindsight, create_request):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    before = model_registry.get_registered_model(session, bank, created.model_key)
    before_refresh_operation_id = before.refresh_operation_id

    updated = update_model(
        session, bank, created.model_key,
        CustomModelUpdateRequest(name="renamed", operation_id=str(uuid4())),
        client=hindsight,
    )

    assert updated.name == "renamed"
    hindsight.refresh_mental_model.assert_not_called()
    row = model_registry.get_registered_model(session, bank, created.model_key)
    assert row.refresh_operation_id == before_refresh_operation_id


def test_update_to_manual_refresh_explicitly_disables_upstream_automation(
    session, bank, hindsight
):
    created = create_custom_model(
        session,
        bank,
        custom_request(always_in_context=False).model_copy(
            update={
                "trigger": {
                    "mode": "delta",
                    "refresh_after_consolidation": True,
                    "min_refresh_interval_seconds": 300,
                }
            }
        ),
        client=hindsight,
    )
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-trigger-refresh"}

    updated = update_model(
        session,
        bank,
        created.model_key,
        CustomModelUpdateRequest(trigger={}, operation_id=str(uuid4())),
        client=hindsight,
    )

    assert updated.trigger == {}
    assert hindsight.update_mental_model.call_args.kwargs["trigger"] == {
        "mode": "full",
        "refresh_after_consolidation": False,
        "refresh_cron": None,
    }


def test_update_with_a_lost_upstream_response_leaves_model_required_for_repair(
    session, bank, hindsight, create_request
):
    from memory.errors import HindsightError

    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.update_mental_model.side_effect = HindsightError("backend unreachable")

    with pytest.raises(HindsightError):
        update_model(
            session, bank, created.model_key,
            CustomModelUpdateRequest(source_query="new query", operation_id=str(uuid4())),
            client=hindsight,
        )

    row = model_registry.get_registered_model(session, bank, created.model_key)
    assert row.delivery_state == "withheld"
    assert row.refresh_status == "required"
    assert row.source_query != "new query"


def test_update_refresh_submission_failure_leaves_model_required(
    session, bank, hindsight, create_request
):
    from memory.errors import HindsightError

    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.refresh_mental_model.side_effect = HindsightError("backend unreachable")

    with pytest.raises(HindsightError):
        update_model(
            session, bank, created.model_key,
            CustomModelUpdateRequest(source_query="new query", operation_id=str(uuid4())),
            client=hindsight,
        )

    row = model_registry.get_registered_model(session, bank, created.model_key)
    assert row.delivery_state == "withheld"
    assert row.refresh_status == "required"


def test_update_cannot_change_a_builtin(session, user_bank_with_builtin, hindsight):
    with pytest.raises(BuiltinModelImmutable):
        update_model(
            session,
            user_bank_with_builtin,
            USER_CONTEXT.key,
            CustomModelUpdateRequest(name="renamed", operation_id=str(uuid4())),
            client=hindsight,
        )


def test_delete_is_idempotent_and_a_404_upstream_satisfies_it(session, bank, hindsight, create_request):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.delete_mental_model.side_effect = MentalModelNotFound("gone")

    delete_model(session, bank, created.model_key, operation_id=str(uuid4()), client=hindsight)
    # Second call, a DIFFERENT operation id: the ledger records it as its
    # own fresh, immediately-completed entry (no conflict, since nothing
    # existed under this id yet), and the row's own already-deleted
    # lifecycle_state is what makes it a no-op either way.
    delete_model(session, bank, created.model_key, operation_id=str(uuid4()), client=hindsight)

    with pytest.raises(MentalModelNotFound):
        get_model(session, bank, created.model_key)


def test_delete_reuse_of_an_operation_id_against_an_already_deleted_model_still_conflicts(
    session, bank, hindsight, create_request
):
    """The ledger is consulted BEFORE the already-deleted short-circuit:
    reusing an operation_id for a genuinely different model after the
    first one is already gone must still raise IdempotencyConflict, not
    silently succeed as a second no-op."""
    first = create_custom_model(session, bank, create_request, client=hindsight)
    second = create_custom_model(
        session, bank, custom_request(name="second"), client=hindsight
    )
    operation_id = str(uuid4())
    delete_model(session, bank, first.model_key, operation_id=operation_id, client=hindsight)

    with pytest.raises(IdempotencyConflict):
        delete_model(session, bank, second.model_key, operation_id=operation_id, client=hindsight)


def test_delete_a_builtin_is_rejected(session, user_bank_with_builtin, hindsight):
    with pytest.raises(BuiltinModelImmutable):
        delete_model(
            session, user_bank_with_builtin, USER_CONTEXT.key,
            operation_id=str(uuid4()), client=hindsight,
        )


def test_delete_exact_retry_calls_upstream_once(session, bank, hindsight, create_request):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    operation_id = "11111111-1111-4111-8111-111111111111"

    delete_model(session, bank, created.model_key, operation_id=operation_id, client=hindsight)
    delete_model(session, bank, created.model_key, operation_id=operation_id, client=hindsight)

    hindsight.delete_mental_model.assert_called_once()


def test_refresh_withholds_delivery_until_the_operation_completes(session, bank, hindsight, create_request):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-123"}

    refreshed = refresh_model(
        session, bank, created.model_key, operation_id=str(uuid4()), client=hindsight
    )

    assert refreshed.delivery_state == "withheld"
    assert refreshed.refresh_status == "pending"


def test_refresh_exact_retry_calls_upstream_once(session, bank, hindsight, create_request):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-123"}
    operation_id = "11111111-1111-4111-8111-111111111111"

    refresh_model(session, bank, created.model_key, operation_id=operation_id, client=hindsight)
    refresh_model(session, bank, created.model_key, operation_id=operation_id, client=hindsight)

    hindsight.refresh_mental_model.assert_called_once()


def test_update_exact_retry_calls_upstream_once(session, bank, hindsight, create_request):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    hindsight.refresh_mental_model.return_value = {"operation_id": "op-retry-refresh"}
    operation_id = "11111111-1111-4111-8111-111111111111"
    request = CustomModelUpdateRequest(source_query="Summarize new conventions.", operation_id=operation_id)

    first = update_model(session, bank, created.model_key, request, client=hindsight)
    second = update_model(session, bank, created.model_key, request, client=hindsight)

    assert first.source_query == second.source_query == "Summarize new conventions."
    hindsight.update_mental_model.assert_called_once()
    hindsight.refresh_mental_model.assert_called_once()


def test_update_retry_with_same_operation_id_and_different_payload_conflicts(
    session, bank, hindsight, create_request
):
    created = create_custom_model(session, bank, create_request, client=hindsight)
    operation_id = str(uuid4())
    update_model(
        session, bank, created.model_key,
        CustomModelUpdateRequest(name="first", operation_id=operation_id),
        client=hindsight,
    )

    with pytest.raises(IdempotencyConflict):
        update_model(
            session, bank, created.model_key,
            CustomModelUpdateRequest(name="different", operation_id=operation_id),
            client=hindsight,
        )
