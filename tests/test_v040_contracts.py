from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from memory.ids import new_model_key
from memory.v040_contracts import (
    LoadContextRequest,
    RetainEvidence,
    TypedRetainRequest,
    TypedRetainResponse,
)


@pytest.fixture
def valid_uuid():
    return uuid4()


@pytest.fixture
def valid_workspace_id():
    return f"ws_{uuid4().hex}"


def test_typed_retain_rejects_unknown_fields_and_requires_evidence():
    with pytest.raises(ValidationError):
        TypedRetainRequest(
            scope="project",
            project_slug="ach-memory",
            content="x",
            memory_type="fact",
            basis="human_explicit",
            trigger="agent_proactive",
            evidence=[],
            operation_id="bad",
            privileged_tag="forbidden",
        )


def test_typed_retain_accepts_future_expiry_with_offset(valid_uuid):
    body = TypedRetainRequest(
        scope="user",
        content="The user prefers concise status reports.",
        memory_type="preference",
        basis="human_explicit",
        trigger="user_requested",
        valid_until=datetime.now(UTC) + timedelta(days=1),
        operation_id=valid_uuid,
        evidence=[
            RetainEvidence(
                kind="user_quote", raw="Keep status reports concise."
            )
        ],
    )
    assert body.valid_until is not None and body.valid_until.utcoffset() is not None


def test_typed_retain_response_requires_derived_lifecycle(valid_uuid):
    response = TypedRetainResponse(
        record_id="rec_123",
        operation_id=valid_uuid,
        document_id="doc_123",
        status="completed",
        recorded_at=datetime.now(UTC),
        valid_until=None,
        lifecycle="active",
    )

    assert response.lifecycle == "active"


def test_load_context_requires_project_when_workspace_is_present(valid_workspace_id):
    with pytest.raises(ValidationError):
        LoadContextRequest(workspace_id=valid_workspace_id)


def test_load_context_rejects_empty_project_when_workspace_is_present(valid_workspace_id):
    with pytest.raises(ValidationError):
        LoadContextRequest(project_slug="", workspace_id=valid_workspace_id)


def test_model_key_is_public_stable_shape():
    assert new_model_key().startswith("mm_")
    assert len(new_model_key()) == 35
