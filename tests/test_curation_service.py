from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from sqlalchemy import func, select

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
    IdempotencyConflict,
    MemoryNotFound,
)
from memory.hindsight.client import HindsightClient, HindsightOutcomeUnknown
from memory.models import CurationOperation, RetainedRecord, RetainedRecordRevision, User
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

    result = correct_record(
        session,
        retained,
        "Updated claim text.",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )

    assert result.state == "completed"
    assert retained.canonical_content == "Updated claim text."
    assert retained.lifecycle == "active"


def test_correction_preserves_prior_canonical_revision(session, bank, hindsight):
    retained = _retained(session, bank, canonical_content="old")
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    correct_record(
        session,
        retained,
        "new",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )

    revision = session.query(RetainedRecordRevision).one()
    op = session.query(CurationOperation).one()
    assert revision.retained_record_id == retained.id
    assert revision.canonical_content == "old"
    assert revision.curation_operation_id == op.operation_id
    assert retained.canonical_content == "new"


def test_exact_correction_retry_has_one_revision(session, bank, hindsight):
    retained = _retained(session, bank, canonical_content="old")
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    operation_id = str(uuid4())
    correct_record(
        session,
        retained,
        "new",
        operation_id=operation_id,
        client=hindsight,
        bank_id=bank.bank_id,
    )
    correct_record(
        session,
        retained,
        "new",
        operation_id=operation_id,
        client=hindsight,
        bank_id=bank.bank_id,
    )

    assert session.query(RetainedRecordRevision).count() == 1
    assert hindsight.curate.call_count == 2


def test_distinct_correction_operations_preserve_oscillating_history(
    session, bank, hindsight
):
    """Removing the caller operation id from `correct_record` makes the
    final A -> B correction reuse the first operation and lose the preceding
    A revision. Each user action needs its own id; only transport retries of
    that exact action deduplicate."""
    retained = _retained(session, bank, canonical_content="A")
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    for operation_id, content in (
        ("11111111-1111-4111-8111-111111111111", "B"),
        ("22222222-2222-4222-8222-222222222222", "A"),
        ("33333333-3333-4333-8333-333333333333", "B"),
    ):
        correct_record(
            session,
            retained,
            content,
            operation_id=operation_id,
            client=hindsight,
            bank_id=bank.bank_id,
        )

    revisions = list(
        session.scalars(
            select(RetainedRecordRevision).order_by(RetainedRecordRevision.revision)
        )
    )
    assert [revision.canonical_content for revision in revisions] == ["A", "B", "A"]
    assert [revision.curation_operation_id for revision in revisions] == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    assert retained.canonical_content == "B"


def test_correction_operation_id_rejects_a_different_payload(
    session, bank, hindsight
):
    retained = _retained(session, bank, canonical_content="A")
    operation_id = "11111111-1111-4111-8111-111111111111"
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    correct_record(
        session,
        retained,
        "B",
        operation_id=operation_id,
        client=hindsight,
        bank_id=bank.bank_id,
    )
    with pytest.raises(IdempotencyConflict):
        correct_record(
            session,
            retained,
            "C",
            operation_id=operation_id,
            client=hindsight,
            bank_id=bank.bank_id,
        )

    assert retained.canonical_content == "B"
    assert hindsight.curate.call_count == 1


def test_unknown_correction_outcome_leaves_prior_canonical_content_current(session, bank, hindsight):
    retained = _retained(session, bank, canonical_content="old")
    hindsight.curate.side_effect = HindsightOutcomeUnknown()

    with pytest.raises(BankCurrentnessUnavailable):
        correct_record(
            session,
            retained,
            "new",
            operation_id=str(uuid4()),
            client=hindsight,
            bank_id=bank.bank_id,
        )

    assert retained.canonical_content == "old"
    assert session.query(RetainedRecordRevision).count() == 1


def test_hard_delete_purges_revisions(session, bank, hindsight):
    retained = _retained(session, bank, canonical_content="old")
    hindsight.curate.return_value = {"id": retained.source_memory_id}
    correct_record(
        session,
        retained,
        "new",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )
    assert session.query(RetainedRecordRevision).count() == 1

    hindsight.delete_document.return_value = {"deleted": True}
    delete_record(session, retained, client=hindsight, bank_id=bank.bank_id)

    assert session.query(RetainedRecordRevision).count() == 0


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


def test_restore_expired_claim_keeps_upstream_invalidated(session, bank, hindsight):
    """The preceding proven forget already established invalidation upstream
    -- restoring an already-expired claim must not re-send `state="valid"`
    and risk reactivating it there, even though ACH's own ledger ends up
    right either way (`_apply_lifecycle` would re-derive "expired")."""
    from datetime import timedelta

    db_now = session.execute(select(func.now())).scalar_one()
    retained = _retained(
        session, bank, lifecycle="forgotten", valid_until=db_now - timedelta(seconds=1)
    )

    result = restore_record(session, retained, client=hindsight, bank_id=bank.bank_id)

    assert result.state == "completed"
    assert retained.lifecycle == "expired"
    assert hindsight.curate.call_count == 0


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
        correct_record(
            session,
            retained,
            "new text",
            operation_id=str(uuid4()),
            client=hindsight,
            bank_id=bank.bank_id,
        )

    assert bank_is_withheld(session, bank) is True
    assert retained.lifecycle == "active"


def test_indefinite_correction_withholds_an_admitting_model(session, bank, hindsight):
    from memory import model_registry

    retained = _retained(session, bank)
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-1",
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}
    hindsight.refresh_mental_model.return_value = {"operation_id": "refresh-42"}

    correct_record(
        session,
        retained,
        "revised",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.delivery_state == "withheld"
    assert model.refresh_status == "pending"
    assert model.refresh_operation_id == "refresh-42"
    hindsight.refresh_mental_model.assert_called_once_with(bank.bank_id, "mm-upstream-1")


def test_expiring_correction_never_touches_models(session, bank, hindsight):
    from datetime import UTC, datetime, timedelta

    from memory import model_registry

    retained = _retained(session, bank, valid_until=datetime.now(UTC) + timedelta(days=1))
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-1",
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    correct_record(
        session,
        retained,
        "revised",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.delivery_state == "ready"
    hindsight.refresh_mental_model.assert_not_called()


def test_forget_of_indefinite_claim_requires_and_submits_model_refresh(session, bank, hindsight):
    from memory import model_registry

    retained = _retained(session, bank)
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-1",
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}
    hindsight.refresh_mental_model.return_value = {"operation_id": "refresh-forget"}

    forget_record(session, retained, client=hindsight, bank_id=bank.bank_id)

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.refresh_operation_id == "refresh-forget"


def test_restore_of_indefinite_claim_requires_and_submits_model_refresh(session, bank, hindsight):
    from memory import model_registry

    retained = _retained(session, bank)
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-1",
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}
    hindsight.refresh_mental_model.return_value = {"operation_id": "refresh-restore"}

    restore_record(session, retained, client=hindsight, bank_id=bank.bank_id)

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.refresh_operation_id == "refresh-restore"


def test_hard_delete_of_indefinite_claim_requires_and_submits_model_refresh(session, bank, hindsight):
    """The bug this closes: hard delete used to purge the retained row
    without ever computing which models it might have fed, so no refresh
    was ever required or submitted for them."""
    from memory import model_registry

    retained = _retained(session, bank)
    retained_id = retained.id
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-1",
    )
    hindsight.delete_document.return_value = {"deleted": True}
    hindsight.refresh_mental_model.return_value = {"operation_id": "refresh-42"}

    delete_record(session, retained, client=hindsight, bank_id=bank.bank_id)

    assert session.get(RetainedRecord, retained_id) is None
    model = model_registry.list_registered_models(session, bank)[0]
    assert model.delivery_state == "withheld"
    assert model.refresh_operation_id == "refresh-42"
    hindsight.refresh_mental_model.assert_called_once_with(bank.bank_id, "mm-upstream-1")


def test_model_refresh_submission_continues_after_a_concurrently_removed_model(session, bank, hindsight):
    """`record_model_refresh_operation`'s row lookup can fail with a
    DomainError too (its own `_locked_model` call), not just the upstream
    `client.refresh_mental_model` call -- both must be guarded so one
    model's failure never aborts the rest of the batch."""
    from memory import model_registry
    from memory.curation_service import _submit_refresh_for_affected_models
    from memory.models import MentalModelRegistration

    survivor = model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(), name="survivor",
        source_query="q", source_tags=["schema:ach-retain-v1"], tags_match="all",
        max_tokens=100, trigger={}, upstream_model_id="mm-upstream-survivor",
    )
    removed = model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(), name="removed",
        source_query="q", source_tags=["schema:ach-retain-v1"], tags_match="all",
        max_tokens=100, trigger={}, upstream_model_id="mm-upstream-removed",
    )
    session.commit()
    hindsight.refresh_mental_model.return_value = {"operation_id": "refresh-op"}
    session.query(MentalModelRegistration).filter_by(id=removed.id).delete()
    session.commit()

    _submit_refresh_for_affected_models(session, bank, [survivor, removed], client=hindsight)

    session.refresh(survivor)
    assert survivor.refresh_operation_id == "refresh-op"


def test_model_refresh_submission_failure_leaves_it_required_without_blocking_others(
    session, bank, hindsight
):
    from memory import model_registry
    from memory.errors import HindsightError

    retained = _retained(session, bank)
    failing = model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="failing", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-failing",
    )
    ok = model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="ok", source_query="q", source_tags=["schema:ach-retain-v1"],
        tags_match="all", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-ok",
    )
    def _refresh_side_effect(bank_id, upstream_model_id):
        if upstream_model_id == "mm-upstream-failing":
            raise HindsightError("backend unreachable")
        return {"operation_id": "refresh-ok"}

    hindsight.curate.return_value = {"id": retained.source_memory_id}
    hindsight.refresh_mental_model.side_effect = _refresh_side_effect

    correct_record(
        session,
        retained,
        "revised",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )

    session.refresh(failing)
    session.refresh(ok)
    assert failing.delivery_state == "withheld"
    assert failing.refresh_status == "required"
    assert failing.refresh_operation_id is None
    assert ok.refresh_operation_id == "refresh-ok"


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


def test_a_model_narrowed_by_a_caller_tag_is_affected_by_a_correction(
    session, bank, hindsight
):
    """A caller tag narrows a model, so a source carrying that tag feeds it.

    The four derived tags are rebuilt from columns, but a caller's own tag is
    only recoverable from `retained_records.caller_tags`. Before that column
    existed the comparison ran against a tag set the caller's label was
    missing from, so `_model_admits` concluded the model was unaffected and
    the summary kept the corrected text for ever -- silently, because a
    model that is never affected is never withheld and never errors.
    """
    from memory import model_registry

    retained = _retained(session, bank, caller_tags=["repo:group/app"])
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q",
        source_tags=["repo:group/app", "schema:ach-retain-v1"],
        tags_match="all_strict", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-1",
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}
    hindsight.refresh_mental_model.return_value = {"operation_id": "refresh-7"}

    correct_record(
        session,
        retained,
        "revised",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.delivery_state == "withheld"
    assert model.refresh_status == "pending"
    hindsight.refresh_mental_model.assert_called_once_with(bank.bank_id, "mm-upstream-1")


def test_a_record_with_unrecorded_caller_tags_admits_every_model(
    session, bank, hindsight
):
    """`caller_tags IS NULL` means "not recorded", never "there were none".

    A row written before the column existed cannot prove that a narrowed
    model excludes it, and SPEC's rule for that is to withhold rather than
    to presume currentness -- so such a row must admit the model even though
    the reconstructed tags do not contain its filter.
    """
    from memory import model_registry

    retained = _retained(session, bank, caller_tags=None)
    model_registry.register_model(
        session, bank, origin="user", model_key=ids.new_model_key(),
        name="n", source_query="q",
        source_tags=["repo:never-recorded", "schema:ach-retain-v1"],
        tags_match="all_strict", max_tokens=100, trigger={},
        upstream_model_id="mm-upstream-2",
    )
    hindsight.curate.return_value = {"id": retained.source_memory_id}
    hindsight.refresh_mental_model.return_value = {"operation_id": "refresh-8"}

    correct_record(
        session,
        retained,
        "revised",
        operation_id=str(uuid4()),
        client=hindsight,
        bank_id=bank.bank_id,
    )

    model = model_registry.list_registered_models(session, bank)[0]
    assert model.delivery_state == "withheld"
    assert model.refresh_status == "pending"


def test_forget_persists_its_reason_on_the_operation(session, bank, hindsight):
    """QA F-14: the reason given to forget used to ride along to Hindsight and
    vanish. It is now on the row `memory_history` reads back."""
    retained = _retained(session, bank)
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    forget_record(session, retained, reason="stale", client=hindsight, bank_id=bank.bank_id)

    assert session.query(CurationOperation).one().reason == "stale"


def test_a_forget_retry_keeps_the_first_reason(session, bank, hindsight):
    """The wording of a retry is not part of the operation's identity: same
    record, same action, same row -- and the reason recorded is the one
    that was given when the outcome was first desired."""
    retained = _retained(session, bank)
    hindsight.curate.return_value = {"id": retained.source_memory_id}

    forget_record(session, retained, reason="stale", client=hindsight, bank_id=bank.bank_id)
    forget_record(session, retained, reason="other", client=hindsight, bank_id=bank.bank_id)

    assert session.query(CurationOperation).one().reason == "stale"
