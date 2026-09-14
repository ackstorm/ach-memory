from memory.backend.fake import FakeBackend
from memory.bank_ref import LogicalBankRef
from memory.bootstrap import provision_before_retain
from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT


def test_bootstrap_provisions_the_bank_and_its_scoped_builtins(db):
    backend = FakeBackend()
    ref = LogicalBankRef("user", "usr_juan", "user_usr_juan")

    provision_before_retain(db, ref, backend=backend)

    assert ref.bank_id in backend._data
    assert set(backend.mental_models[ref.bank_id]) == {USER_CONTEXT.key}


def test_bootstrap_is_idempotent(db):
    backend = FakeBackend()
    ref = LogicalBankRef("project", "prj_1", "project_acme")

    provision_before_retain(db, ref, backend=backend)
    provision_before_retain(db, ref, backend=backend)

    assert set(backend.mental_models[ref.bank_id]) == {PROJECT_CONTEXT.key}
