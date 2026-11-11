from memory.backend.fake import FakeBackend
from memory.bank_ref import LogicalBankRef
from memory.bootstrap import provision_before_retain
from memory.builtin_models import BUILTIN_MODELS


def test_bootstrap_provisions_the_bank_and_its_scoped_builtins(db):
    backend = FakeBackend()
    ref = LogicalBankRef("user", "usr_juan", "user_usr_juan")
    backend.provision_mental_models(ref.bank_id, BUILTIN_MODELS[:1])

    provision_before_retain(db, ref, backend=backend)

    assert ref.bank_id in backend._data
    # A user bank carries no built-ins: provisioning one deletes any it had before.
    assert backend.mental_models[ref.bank_id] == {}


def test_bootstrap_is_idempotent(db):
    backend = FakeBackend()
    ref = LogicalBankRef("project", "prj_1", "project_acme")

    provision_before_retain(db, ref, backend=backend)
    provision_before_retain(db, ref, backend=backend)

    assert set(backend.mental_models[ref.bank_id]) == {m.key for m in BUILTIN_MODELS}
