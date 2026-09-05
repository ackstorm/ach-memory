from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from memory import ids
from memory.currentness import bank_is_withheld
from memory.errors import BankCurrentnessUnavailable, MemoryNotFound
from memory.expiry import ensure_no_expiry_backlog, expire_due_once
from memory.hindsight.client import HindsightClient, HindsightOutcomeUnknown
from memory.models import RetainedRecord, User
from memory.retained_records import LogicalBankRef


@pytest.fixture
def bank(session, tenant) -> LogicalBankRef:
    user = User(id="usr_expiry", tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    return LogicalBankRef(tenant, "user", user.id, None, user.bank_id)


@pytest.fixture
def hindsight():
    return create_autospec(HindsightClient, instance=True)


def _due_record(session, bank: LogicalBankRef, *, valid_until: datetime, **overrides) -> RetainedRecord:
    fields = {
        "tenant_id": bank.tenant_id,
        "scope": bank.scope,
        "user_id": bank.user_id,
        "project_internal_id": bank.project_internal_id,
        "operation_id": str(uuid4()),
        "payload_hash": "h" * 64,
        "document_id": f"ach-retain-{uuid4().hex}",
        "source_memory_id": str(uuid4()),
        "canonical_content": "An expiring claim.",
        "memory_type": "fact",
        "basis": "human_explicit",
        "trigger": "user_requested",
        "sanitized_evidence": [{"kind": "user_quote", "raw": "quote", "source_ref": None}],
        "valid_until": valid_until,
        "lifecycle": "active",
        "upstream_state": "completed",
        "calling_agent": None,
        "created_by_credential": "key_expiry",
    }
    fields.update(overrides)
    row = RetainedRecord(**fields)
    session.add(row)
    session.flush()
    return row


def test_expiry_is_exact_at_valid_until(session, bank, hindsight):
    now = datetime.now(UTC)
    record = _due_record(session, bank, valid_until=now)
    hindsight.curate.return_value = {"id": record.source_memory_id}

    result = expire_due_once(session, bank, client=hindsight, now=now)

    assert result.expired == 1
    assert result.remaining == 0
    assert record.lifecycle == "expired"


def test_not_yet_due_records_are_left_alone(session, bank, hindsight):
    now = datetime.now(UTC)
    record = _due_record(session, bank, valid_until=now + timedelta(seconds=1))

    result = expire_due_once(session, bank, client=hindsight, now=now)

    assert result.expired == 0
    assert record.lifecycle == "active"
    hindsight.curate.assert_not_called()


def test_expiry_claims_only_the_first_32_in_stable_order(session, bank, hindsight):
    now = datetime.now(UTC)
    records = [
        _due_record(session, bank, valid_until=now - timedelta(seconds=40 - n), source_memory_id=str(uuid4()))
        for n in range(40)
    ]
    hindsight.curate.return_value = {"id": "ok"}

    result = expire_due_once(session, bank, client=hindsight, now=now)

    assert result.expired == 32
    assert result.remaining == 8
    called_ids = [call.args[1] for call in hindsight.curate.call_args_list]
    assert called_ids == [r.source_memory_id for r in records[:32]]
    assert all(r.lifecycle == "expired" for r in records[:32])
    assert all(r.lifecycle == "active" for r in records[32:])


def test_absent_upstream_memory_still_completes_expiry(session, bank, hindsight):
    now = datetime.now(UTC)
    record = _due_record(session, bank, valid_until=now)
    hindsight.curate.side_effect = MemoryNotFound()

    result = expire_due_once(session, bank, client=hindsight, now=now)

    assert result.expired == 1
    assert record.lifecycle == "expired"


def test_unknown_expiry_outcome_withholds_the_bank(session, bank, hindsight):
    now = datetime.now(UTC)
    record = _due_record(session, bank, valid_until=now)
    hindsight.curate.side_effect = HindsightOutcomeUnknown()

    with pytest.raises(BankCurrentnessUnavailable):
        expire_due_once(session, bank, client=hindsight, now=now)

    assert record.lifecycle == "active"  # unproven -- left untouched
    assert bank_is_withheld(session, bank) is True


def test_ensure_no_expiry_backlog_withholds_the_read_when_a_backlog_remains(session, bank, hindsight):
    now = datetime.now(UTC)
    for n in range(33):
        _due_record(session, bank, valid_until=now - timedelta(seconds=33 - n), source_memory_id=str(uuid4()))
    hindsight.curate.return_value = {"id": "ok"}

    with pytest.raises(BankCurrentnessUnavailable):
        ensure_no_expiry_backlog(session, bank, client=hindsight, now=now)


def test_ensure_no_expiry_backlog_is_silent_when_everything_is_caught_up(session, bank, hindsight):
    now = datetime.now(UTC)
    record = _due_record(session, bank, valid_until=now)
    hindsight.curate.return_value = {"id": record.source_memory_id}

    ensure_no_expiry_backlog(session, bank, client=hindsight, now=now)  # must not raise

    assert record.lifecycle == "expired"
