import pytest

from memory import working_state
from memory.auth import provisioning
from memory.auth.principal import Principal
from memory.errors import InvalidRequest


@pytest.fixture
def principal(db) -> Principal:
    user = provisioning.link_identity(db, issuer="https://ach.example.com", subject="juan@example.com")
    return Principal(user_id=user.id)


def test_put_then_get_roundtrips(db, principal):
    state = {"objective": "ship task 2.3", "next_steps": "write tests"}
    working_state.put(
        db, principal, working_state.PutWorkingStateRequest(workspace_id="ws1", state=state)
    )

    result = working_state.get(db, principal, working_state.WorkingStateRequest(workspace_id="ws1"))

    assert result.state == state
    assert result.updated_at is not None


def test_put_replaces_the_whole_document(db, principal):
    working_state.put(
        db, principal,
        working_state.PutWorkingStateRequest(workspace_id="ws1", state={"objective": "a", "decisions": "b"}),
    )

    working_state.put(
        db, principal, working_state.PutWorkingStateRequest(workspace_id="ws1", state={"objective": "c"})
    )

    result = working_state.get(db, principal, working_state.WorkingStateRequest(workspace_id="ws1"))
    assert result.state == {"objective": "c"}


def test_delete_clears_the_checkpoint_and_is_a_noop_if_absent(db, principal):
    working_state.put(
        db, principal, working_state.PutWorkingStateRequest(workspace_id="ws1", state={"objective": "a"})
    )

    working_state.delete(db, principal, working_state.WorkingStateRequest(workspace_id="ws1"))
    working_state.delete(db, principal, working_state.WorkingStateRequest(workspace_id="ws1"))  # no-op

    result = working_state.get(db, principal, working_state.WorkingStateRequest(workspace_id="ws1"))
    assert result.state is None


def test_unknown_state_key_is_rejected():
    with pytest.raises(InvalidRequest):
        working_state.PutWorkingStateRequest(workspace_id="ws1", state={"bogus": "x"})


def test_one_user_cannot_read_another_users_workspace(db, principal):
    working_state.put(
        db, principal, working_state.PutWorkingStateRequest(workspace_id="ws1", state={"objective": "a"})
    )

    other = Principal(user_id="usr_someone_else")
    result = working_state.get(db, other, working_state.WorkingStateRequest(workspace_id="ws1"))

    assert result.state is None
