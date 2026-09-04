import pytest
from sqlalchemy.exc import IntegrityError

from memory import ids
from memory.currentness import bank_is_withheld, ready_bank, withhold_bank
from memory.models import BankCurrentness, User, WorkingSession
from memory.retained_records import LogicalBankRef


@pytest.fixture
def currentness_bank(session, tenant):
    user = User(id="usr_currentness", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return LogicalBankRef(tenant, "user", user.id, None, user.bank_id)


def test_bank_requires_exact_blocking_operation_to_become_ready(session, currentness_bank):
    assert bank_is_withheld(session, currentness_bank) is False

    withheld = withhold_bank(session, currentness_bank, "curation-new")
    stale = ready_bank(session, currentness_bank, "curation-old")

    assert withheld.state == "withheld"
    assert stale.state == "withheld"
    assert stale.blocking_operation_id == "curation-new"
    assert bank_is_withheld(session, currentness_bank) is True

    ready = ready_bank(session, currentness_bank, "curation-new")
    assert ready.state == "ready"
    assert ready.blocking_operation_id is None
    assert bank_is_withheld(session, currentness_bank) is False


def test_new_withhold_supersedes_an_older_operation(session, currentness_bank):
    withhold_bank(session, currentness_bank, "curation-old")
    withhold_bank(session, currentness_bank, "curation-new")

    row = ready_bank(session, currentness_bank, "curation-old")

    assert row.state == "withheld"
    assert row.blocking_operation_id == "curation-new"


def test_scoped_rows_require_exactly_one_matching_identity(session, tenant):
    session.add(
        BankCurrentness(
            tenant_id=tenant,
            scope="user",
            user_id=None,
            project_internal_id=None,
            state="ready",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_working_sessions_have_nullable_completion_fence_columns():
    assert WorkingSession.completed_checkpoint_seq.nullable is True
    assert WorkingSession.completed_at.nullable is True
