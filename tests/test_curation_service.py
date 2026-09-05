from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from memory import ids
from memory.curation_service import (
    correct_record,
    delete_record,
    forget_record,
    reconcile_bank_once,
    restore_record,
)
from memory.currentness import bank_is_withheld
from memory.errors import (
    BankCurrentnessUnavailable,
    CurationNeedsOperator,
    DocumentNotFound,
    MemoryNotFound,
)
from memory.hindsight.client import HindsightClient, HindsightOutcomeUnknown
from memory.models import CurationOperation, RetainedRecord, User
from memory.retained_records import LogicalBankRef


@pytest.fixture
def bank(session, tenant) -> LogicalBankRef:
    user = User(id="usr_curation", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return LogicalBankRef(tenant, "user", user.id, None, user.bank_id)


def _retained(session, bank: LogicalBankRef, **overrides) -> RetainedRecord:
    fields = {
        "tenant_id": bank.tenant_id,
        "scope": bank.scope,
        "user_id": bank.user_id,
        "project_internal_id": bank.project_internal_id,
        "operation_id": str(uuid4()),
        "payload_hash": "h" * 64,
        "document_id": f"ach-retain-{uuid4().hex}",
        "source_memory_id": str(uuid4()),
        "canonical_content": "Project slugs remain stable through aliases.",
        "memory_type": "constraint",
        "basis": "human_explicit",
        "trigger": "user_requested",
        "sanitized_evidence": [{"kind": "user_quote", "raw": "quote", "source_ref": None}],
        "valid_until": None,
        "lifecycle": "active",
        "upstream_state": "completed",
        "calling_agent": None,
        "created_by_credential": "key_curation",
    }
    fields.update(overrides)
    row = RetainedRecord(**fields)
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def hindsight():
    return create_autospec(HindsightClient, instance=True)


def test_forget_proven_success_marks_forgotten(session, bank, hindsight):
    retained = _retained(session, bank)
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    result = forget_record(session, retained, reason="obsolete", client=hindsight, bank_id=bank.bank_id)

    assert result.state == "completed"
    assert retained.lifecycle == "forgotten"
    hindsight.curate.assert_called_once_with(
        bank.bank_id, retained.source_memory_id, state="invalidated", reason="obsolete"
    )
    assert bank_is_withheld(session, bank) is False


def test_lost_forget_response_withholds_bank(session, bank, hindsight):
    retained = _retained(session, bank)
    hindsight.curate.side_effect = HindsightOutcomeUnknown()

    with pytest.raises(BankCurrentnessUnavailable):
        forget_record(session, retained, reason="obsolete", client=hindsight, bank_id=bank.bank_id)

    assert retained.lifecycle == "active"  # unproven -- prior lifecycle untouched
    assert bank_is_withheld(session, bank) is True
    op = session.get(CurationOperation, session.query(CurationOperation).one().operation_id)
    assert op.state == "unknown"


def test_correct_proven_success_updates_content_and_keeps_lifecycle(session, bank, hindsight):
    retained = _retained(session, bank)
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    result = correct_record(session, retained, "Updated claim text.", client=hindsight, bank_id=bank.bank_id)

    assert result.state == "completed"
    assert retained.canonical_content == "Updated claim text."
    assert retained.lifecycle == "active"


def test_restore_never_resurrects_an_already_expired_claim(session, bank, hindsight):
    from datetime import UTC, datetime, timedelta

    retained = _retained(
        session, bank, lifecycle="forgotten",
        valid_until=datetime.now(UTC) - timedelta(days=1),
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    result = restore_record(session, retained, client=hindsight, bank_id=bank.bank_id)

    assert result.state == "completed"
    assert retained.lifecycle == "expired"


def test_delete_proven_success_purges_the_record(session, bank, hindsight):
    retained = _retained(session, bank)
    retained_id = retained.id
    hindsight.delete_document.return_value = {"deleted": True}

    result = delete_record(session, retained, client=hindsight, bank_id=bank.bank_id)

    assert result.state == "completed"
    assert session.get(RetainedRecord, retained_id) is None
    assert session.query(CurationOperation).count() == 0


@pytest.mark.parametrize(
    "action,call",
    [
        ("forget", lambda db, r, h, bid: forget_record(db, r, client=h, bank_id=bid)),
        ("delete", lambda db, r, h, bid: delete_record(db, r, client=h, bank_id=bid)),
    ],
)
def test_absent_target_satisfies_removal_actions_directly(session, bank, hindsight, action, call):
    retained = _retained(session, bank)
    hindsight.curate.side_effect = MemoryNotFound()
    hindsight.delete_document.side_effect = DocumentNotFound()

    result = call(session, retained, hindsight, bank.bank_id)

    assert result.state == "completed"


def test_absent_target_needs_operator_for_correct(session, bank, hindsight):
    retained = _retained(session, bank)
    hindsight.curate.side_effect = MemoryNotFound()

    with pytest.raises(CurationNeedsOperator):
        correct_record(session, retained, "new text", client=hindsight, bank_id=bank.bank_id)

    assert bank_is_withheld(session, bank) is True
    assert retained.lifecycle == "active"


def test_indefinite_correction_withholds_an_admitting_model(session, bank, hindsight):
    from memory import model_registry

    retained = _retained(session, bank)
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    correct_record(session, retained, "revised", client=hindsight, bank_id=bank.bank_id)

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.delivery_state == "withheld"
    assert model.refresh_operation_id is not None


def test_expiring_correction_never_touches_models(session, bank, hindsight):
    from datetime import UTC, datetime, timedelta

    from memory import model_registry

    retained = _retained(session, bank, valid_until=datetime.now(UTC) + timedelta(days=1))
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    correct_record(session, retained, "revised", client=hindsight, bank_id=bank.bank_id)

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.delivery_state == "ready"


# -- reconcile_bank_once ------------------------------------------------


def _unknown_operation(session, bank, retained, action: str, **overrides) -> CurationOperation:
    from memory.curation_service import _operation_id_for

    fields = {
        "operation_id": _operation_id_for(retained.id, action, overrides.get("desired_content")),
        "retained_record_id": retained.id,
        "tenant_id": bank.tenant_id,
        "scope": bank.scope,
        "user_id": bank.user_id,
        "project_internal_id": bank.project_internal_id,
        "action": action,
        "desired_content": overrides.get("desired_content"),
        "state": "unknown",
    }
    row = CurationOperation(**fields)
    session.add(row)
    session.flush()
    from memory.currentness import withhold_bank

    withhold_bank(session, bank, row.operation_id)
    session.commit()
    return row


@pytest.mark.parametrize("action", ["forget", "expire", "delete"])
def test_reconcile_absent_target_satisfies_removal_action(session, bank, hindsight, action):
    retained = _retained(session, bank)
    _unknown_operation(session, bank, retained, action)
    hindsight.get_memory.side_effect = MemoryNotFound()
    hindsight.get_document.side_effect = DocumentNotFound()

    result = reconcile_bank_once(session, bank, client=hindsight)

    assert result.state == "completed"
    assert bank_is_withheld(session, bank) is False


@pytest.mark.parametrize("action", ["correct", "restore"])
def test_reconcile_absent_target_needs_operator_for_present_state(session, bank, hindsight, action):
    retained = _retained(session, bank)
    _unknown_operation(session, bank, retained, action, desired_content="x" if action == "correct" else None)
    hindsight.get_memory.side_effect = MemoryNotFound()

    result = reconcile_bank_once(session, bank, client=hindsight)

    assert result.state == "needs_operator"
    assert bank_is_withheld(session, bank) is True


def test_reconcile_present_target_safely_repeats_the_mutation(session, bank, hindsight):
    retained = _retained(session, bank)
    _unknown_operation(session, bank, retained, "forget")
    hindsight.get_memory.return_value = {"id": retained.source_memory_id}
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    result = reconcile_bank_once(session, bank, client=hindsight)

    assert result.state == "completed"
    assert retained.lifecycle == "forgotten"
    assert bank_is_withheld(session, bank) is False


def test_reconcile_with_nothing_pending_leaves_an_unrelated_barrier_alone(session, bank, hindsight):
    """`ready_bank` only ever releases the exact operation_id that set the
    barrier -- reconciliation must not blindly clear one it did not resolve."""
    from memory.currentness import withhold_bank

    withhold_bank(session, bank, "stale-op-not-tracked-here")
    session.commit()

    result = reconcile_bank_once(session, bank, client=hindsight)

    assert result.state == "completed"
    assert bank_is_withheld(session, bank) is True


def test_reconcile_never_loops_past_one_operation(session, bank, hindsight):
    """At most one inspection/mutation per call: a second pending unknown
    operation is left untouched."""
    first = _retained(session, bank)
    second = _retained(session, bank)
    _unknown_operation(session, bank, first, "forget")
    _unknown_operation(session, bank, second, "restore", desired_content=None)
    hindsight.get_memory.return_value = {"id": first.source_memory_id}
    hindsight.curate.return_value = {"id": first.source_memory_id}

    reconcile_bank_once(session, bank, client=hindsight)

    remaining = session.query(CurationOperation).filter_by(state="unknown").count()
    assert remaining == 1
