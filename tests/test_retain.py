from unittest.mock import Mock
from uuid import uuid4

import pytest

from memory import audit, retain
from memory.auth.principal import Principal
from memory.backend.fake import FakeBackend
from memory.errors import ContentRejectedBySanitizer, InvalidRequest


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id="usr_juan")


@pytest.fixture
def request_() -> retain.RetainRequest:
    return retain.RetainRequest(
        scope="user",
        content="Project slugs remain stable through aliases.",
        memory_type="constraint",
        basis="human_explicit",
        operation_id=uuid4(),
    )


def test_happy_path_stores_server_then_caller_tags(db, principal, backend):
    request = retain.RetainRequest(
        scope="user",
        content="The user prefers concise status reports.",
        memory_type="preference",
        basis="human_explicit",
        operation_id=uuid4(),
        tags=["repo:group/app"],
        wait=True,
    )

    result = retain.submit(db, principal, request, backend=backend)

    assert result.memory_id.startswith("mem_")
    assert result.status == "completed"
    stored = backend.get("user_usr_juan", result.memory_id)
    assert stored is not None
    assert stored.text == "The user prefers concise status reports."
    assert stored.tags == (
        "type:preference",
        "basis:human_explicit",
        "schema:ach-retain-v1",
        "repo:group/app",
    )


def test_replay_returns_the_same_memory_id_and_does_not_call_the_backend_twice(
    db, principal, request_, backend
):
    backend.retain = Mock(wraps=backend.retain)

    first = retain.submit(db, principal, request_, backend=backend)
    second = retain.submit(db, principal, request_, backend=backend)

    assert second.memory_id == first.memory_id
    assert second.status == "replayed"
    backend.retain.assert_called_once()


def test_secret_content_is_rejected_and_nothing_is_retained(db, principal, backend):
    request = retain.RetainRequest(
        scope="user",
        content="the key is sk-1234567890abcdef1234",
        memory_type="fact",
        basis="human_explicit",
        operation_id=uuid4(),
    )

    with pytest.raises(ContentRejectedBySanitizer):
        retain.submit(db, principal, request, backend=backend)

    assert backend._data.get("user_usr_juan", {}) == {}
    assert audit.find_retain(db, str(request.operation_id)) is None


def test_oversize_content_is_rejected(db, principal, backend, monkeypatch):
    from memory import retain as retain_module

    settings = retain_module.get_settings()
    monkeypatch.setattr(settings, "max_content_bytes", 5)
    request = retain.RetainRequest(
        scope="user",
        content="far too long for the limit",
        memory_type="fact",
        basis="human_explicit",
        operation_id=uuid4(),
    )

    with pytest.raises(InvalidRequest):
        retain.submit(db, principal, request, backend=backend)


def test_project_created_notice_on_first_retain_to_a_new_slug(db, principal, backend):
    request = retain.RetainRequest(
        scope="project",
        project_slug="brand-new",
        content="A durable project fact.",
        memory_type="fact",
        basis="agent_verified",
        operation_id=uuid4(),
    )

    result = retain.submit(db, principal, request, backend=backend)

    assert result.notice == "PROJECT_CREATED"


def test_journal_row_carries_operation_content_hash_and_owner(db, principal, request_, backend):
    result = retain.submit(db, principal, request_, backend=backend)

    row = audit.find_retain(db, str(request_.operation_id))
    assert row is not None
    assert row.resource == result.memory_id
    assert row.details["content_hash"]
    assert row.details["owner"] == "usr_juan"
