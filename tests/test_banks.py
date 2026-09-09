import pytest

from memory import banks, ids
from memory.auth.principal import Principal
from memory.errors import ProjectNotFound
from memory.models import User


@pytest.fixture
def principal(session, tenant):
    user = User(id="usr_banks", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return Principal(
        tenant_id=tenant, user_id=user.id, is_master=False,
        key_id="key_banks", credential_id="key_banks",
    )


def test_project_bank_resolution_does_not_create_by_default(session, principal):
    """The only caller that may create a project says so explicitly
    (bootstrap). A default that creates is one careless caller away from
    letting any authenticated principal squat an arbitrary slug."""
    with pytest.raises(ProjectNotFound):
        banks.resolve_project_bank(session, principal, "never/seen")
