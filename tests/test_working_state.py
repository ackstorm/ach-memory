from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from memory import ids
from memory.models import Project, User, WorkingSession, WorkingState


def _project(tenant: str) -> Project:
    return Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=tenant,
        project_slug="acme-api",
        owner_type="user",
        owner_id="usr_x",
        bank_id=ids.new_project_bank_id(),
    )


def _user(tenant: str) -> User:
    return User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())


def _state(*, tenant: str, user_id: str, project_internal_id: str, workspace_id: str) -> WorkingState:
    return WorkingState(
        tenant_id=tenant,
        user_id=user_id,
        project_internal_id=project_internal_id,
        workspace_id=workspace_id,
        objective="ship the feature",
        current_direction=None,
        recent_decisions=["use approach A"],
        open_questions=["is B in scope?"],
        next_steps=["write tests"],
        updated_at=datetime.now(UTC),
        session_id="sess-1",
        session_epoch=1,
        checkpoint_seq=1,
    )


def test_two_workspaces_of_one_user_project_each_hold_their_own_state(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    session.add(
        _state(
            tenant=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "a" * 32,
        )
    )
    session.add(
        _state(
            tenant=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "b" * 32,
        )
    )
    session.flush()

    assert session.query(WorkingState).count() == 2


def test_a_duplicate_session_scope_cannot_allocate_a_second_row(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    def _row() -> WorkingSession:
        return WorkingSession(
            tenant_id=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "a" * 32,
            session_id="sess-1",
        )

    session.add(_row())
    session.flush()
    session.add(_row())

    with pytest.raises(IntegrityError):
        session.flush()


def test_session_epochs_increase_across_distinct_session_ids(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    first = WorkingSession(
        tenant_id=tenant,
        user_id=user.id,
        project_internal_id=project.internal_id,
        workspace_id="ws_" + "a" * 32,
        session_id="sess-1",
    )
    session.add(first)
    session.flush()

    second = WorkingSession(
        tenant_id=tenant,
        user_id=user.id,
        project_internal_id=project.internal_id,
        workspace_id="ws_" + "a" * 32,
        session_id="sess-2",
    )
    session.add(second)
    session.flush()

    assert second.session_epoch > first.session_epoch


def test_working_state_json_fields_round_trip_and_updated_at_is_aware(session, tenant):
    project = _project(tenant)
    user = _user(tenant)
    session.add_all([project, user])
    session.flush()

    session.add(
        _state(
            tenant=tenant,
            user_id=user.id,
            project_internal_id=project.internal_id,
            workspace_id="ws_" + "a" * 32,
        )
    )
    session.flush()
    session.expire_all()

    stored = session.query(WorkingState).one()
    assert stored.recent_decisions == ["use approach A"]
    assert stored.open_questions == ["is B in scope?"]
    assert stored.next_steps == ["write tests"]
    assert stored.updated_at.tzinfo is not None
