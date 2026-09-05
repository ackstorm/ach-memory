from itertools import count
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from memory import ids
from memory.auth.principal import Principal
from memory.errors import HindsightError, IdempotencyConflict, ProjectNotFound
from memory.hindsight.client import HindsightClient
from memory.models import Project, ProjectSlug, User
from memory.retained_records import get_by_operation
from memory.retention import submit_retain
from memory.v040_contracts import RetainEvidence, TypedRetainRequest


@pytest.fixture
def principal(session, tenant) -> Principal:
    user = User(id="usr_retention", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return Principal(
        tenant_id=tenant,
        user_id=user.id,
        is_master=False,
        key_id="key_retention",
        credential_id="key_retention",
    )


@pytest.fixture
def typed_request() -> TypedRetainRequest:
    return TypedRetainRequest(
        scope="user",
        content="Project slugs remain stable through aliases.",
        memory_type="constraint",
        basis="human_explicit",
        trigger="user_requested",
        evidence=[RetainEvidence(kind="user_quote", raw="Slugs must stay stable.")],
        operation_id=uuid4(),
    )


@pytest.fixture
def client():
    mock = create_autospec(HindsightClient, instance=True)
    mock.retain_items.side_effect = lambda bank_id, items, *, operation_id, is_async=True: {
        "operation_id": operation_id,
        "status": "pending",
    }
    return mock


def test_submit_retain_sends_only_claim_and_reserved_tags(session, principal, typed_request, client):
    result = submit_retain(session, principal, typed_request, wait=False, client=client)

    item = client.retain_items.call_args.args[1][0]
    assert item.content == "Project slugs remain stable through aliases."
    assert item.context is None
    assert item.metadata == {"ach_record_id": result.record_id}
    assert item.tags == [
        "type:constraint",
        "basis:human_explicit",
        "schema:ach-retain-v1",
        "validity:indefinite",
    ]
    assert item.observation_scopes is None
    assert item.strategy == "ach-exact-v1"
    assert "evidence" not in item.to_payload()
    assert result.status == "accepted"


def test_retry_reuses_document_and_operation(session, principal, typed_request, client):
    first = submit_retain(session, principal, typed_request, wait=False, client=client)
    second = submit_retain(session, principal, typed_request, wait=False, client=client)

    assert first.record_id == second.record_id
    assert first.document_id == second.document_id
    assert client.retain_items.call_args_list[0].kwargs["operation_id"] == str(typed_request.operation_id)
    assert client.retain_items.call_args_list[1].kwargs["operation_id"] == str(typed_request.operation_id)


def test_different_payload_at_same_operation_id_conflicts(session, principal, typed_request, client):
    submit_retain(session, principal, typed_request, wait=False, client=client)
    conflicting = typed_request.model_copy(update={"content": "A different claim entirely."})

    with pytest.raises(IdempotencyConflict):
        submit_retain(session, principal, conflicting, wait=False, client=client)


def test_wait_polls_until_completed_and_hydrates_source_memory_id(session, principal, typed_request, client):
    ticks = count(0.0, 1.0)
    client.get_operation.return_value = {
        "status": "completed",
        "result": {"memory_id": "mem-123"},
    }

    result = submit_retain(
        session, principal, typed_request, wait=True, client=client,
        clock=lambda: next(ticks), sleeper=lambda _seconds: None,
    )

    assert result.status == "completed"
    row = get_by_operation(
        session,
        _bank_for(session, principal),
        str(typed_request.operation_id),
    )
    assert row.source_memory_id == "mem-123"
    assert row.upstream_state == "completed"


def test_wait_gives_up_after_deadline_without_failing(session, principal, typed_request, client):
    client.get_operation.return_value = {"status": "pending"}
    ticks = count(0.0, 10.0)

    result = submit_retain(
        session, principal, typed_request, wait=True, client=client,
        clock=lambda: next(ticks), sleeper=lambda _seconds: None,
    )

    assert result.status == "pending"


def test_mismatched_upstream_operation_identity_is_a_hindsight_error(session, principal, typed_request, client):
    client.retain_items.side_effect = None
    client.retain_items.return_value = {"operation_id": str(uuid4())}

    with pytest.raises(HindsightError):
        submit_retain(session, principal, typed_request, wait=False, client=client)


def test_project_scope_never_creates_an_unknown_project(session, principal, client):
    request = TypedRetainRequest(
        scope="project",
        project_slug="does-not-exist",
        content="A durable project fact.",
        memory_type="fact",
        basis="human_explicit",
        trigger="agent_proactive",
        evidence=[RetainEvidence(kind="tool_result", raw="pytest: 1 passed")],
        operation_id=uuid4(),
    )

    with pytest.raises(ProjectNotFound):
        submit_retain(session, principal, request, wait=False, client=client)

    assert session.query(Project).filter_by(tenant_id=principal.tenant_id).count() == 0


def test_project_scope_resolves_an_existing_project(session, tenant, principal, client):
    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        owner_type="user",
        owner_id=principal.user_id,
        bank_id=ids.new_project_bank_id(),
    )
    session.add(project)
    session.flush()
    session.add(ProjectSlug(tenant_id=tenant, slug="existing-project", project_internal_id=project.internal_id))
    session.flush()

    request = TypedRetainRequest(
        scope="project",
        project_slug="existing-project",
        content="A durable project fact.",
        memory_type="fact",
        basis="human_explicit",
        trigger="agent_proactive",
        evidence=[RetainEvidence(kind="tool_result", raw="pytest: 1 passed")],
        operation_id=uuid4(),
    )

    result = submit_retain(session, principal, request, wait=False, client=client)

    assert result.status == "accepted"


def _bank_for(session, principal):
    from memory.retained_records import LogicalBankRef

    user = session.get(User, principal.user_id)
    return LogicalBankRef(principal.tenant_id, "user", user.id, None, user.bank_id)
