"""Outcome-safe correction, forgetting, restoration and hard delete.

Every mutation proves its Hindsight outcome before ACH's own lifecycle
changes. A response that proves nothing withholds the physical bank rather
than guess (SPEC §5.8): the caller sees `BankCurrentnessUnavailable` until an
authorized access reconciles it via `reconcile_bank_once`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory import model_registry
from memory.currentness import ready_bank, withhold_bank
from memory.errors import (
    BankCurrentnessUnavailable,
    CurationNeedsOperator,
    DocumentNotFound,
    MemoryNotCuratable,
    MemoryNotFound,
)
from memory.hindsight.client import HindsightClient, HindsightOutcomeUnknown
from memory.models import CurationOperation, MentalModelRegistration, RetainedRecord
from memory.retained_records import LogicalBankRef, _bank_filters, _lock_bank

# forget/expire/delete all remove a claim from current standing; a target
# already absent upstream proves that removed state (SPEC §5.8's terminal
# rules). correct/restore assert a PRESENT-state content, which an absent
# target can never prove -- that case is terminal for automatic repair.
_REMOVAL_ACTIONS = ("forget", "expire", "delete")


@dataclass(frozen=True)
class CurationResult:
    state: str  # "completed" | "needs_operator" | "unknown"
    record_id: str
    action: str


def _db_now(db: Session) -> datetime:
    return db.execute(select(func.now())).scalar_one()


def _bank_with_id(retained: RetainedRecord, bank_id: str) -> LogicalBankRef:
    return LogicalBankRef(
        tenant_id=retained.tenant_id,
        scope=retained.scope,
        user_id=retained.user_id,
        project_internal_id=retained.project_internal_id,
        bank_id=bank_id,
    )


def _operation_id_for(retained_id, action: str, desired_content: str | None) -> str:
    """Deterministic per (record, action, desired outcome): a retry of the
    exact same desired mutation always resolves to the same operation row,
    the same way `document_id_for` makes retain retries stable."""
    material = f"{retained_id}|{action}|{desired_content or ''}"
    return f"ach-curate-{sha256(material.encode()).hexdigest()[:32]}"


def _accept_operation(
    db: Session,
    bank: LogicalBankRef,
    retained: RetainedRecord,
    *,
    action: str,
    desired_content: str | None = None,
) -> CurationOperation:
    _lock_bank(db, bank)
    operation_id = _operation_id_for(retained.id, action, desired_content)
    existing = db.scalar(
        select(CurationOperation)
        .where(CurationOperation.operation_id == operation_id)
        .with_for_update()
    )
    if existing is not None:
        return existing
    row = CurationOperation(
        operation_id=operation_id,
        retained_record_id=retained.id,
        tenant_id=bank.tenant_id,
        scope=bank.scope,
        user_id=bank.user_id,
        project_internal_id=bank.project_internal_id,
        action=action,
        desired_content=desired_content,
        state="pending",
    )
    db.add(row)
    db.flush()
    return row


def _tags_for(retained: RetainedRecord) -> list[str]:
    validity = "expiring" if retained.valid_until is not None else "indefinite"
    return [
        f"type:{retained.memory_type}",
        f"basis:{retained.basis}",
        "schema:ach-retain-v1",
        f"validity:{validity}",
    ]


def _model_admits(model: MentalModelRegistration, source_tags: list[str]) -> bool:
    """True unless the model's own static tag filter provably excludes this
    source. Defaults to admitting whenever the match mode itself cannot be
    proven exclusionary -- SPEC's "if exclusion cannot be proven, withhold"."""
    model_tags = set(model.source_tags or [])
    if not model_tags:
        return True
    source = set(source_tags)
    if model.tags_match in ("all", "all_strict", "exact"):
        return model_tags <= source
    if model.tags_match in ("any", "any_strict"):
        return bool(model_tags & source)
    return True


def _withhold_affected_models(db: Session, bank: LogicalBankRef, retained: RetainedRecord) -> None:
    """After a proven INDEFINITE-source lifecycle change, withhold every
    registered model whose static tags might have drawn on this source.
    Expiring records never feed a registered model (SPEC §5.6/§6.4), so this
    is a no-op for one."""
    if retained.valid_until is not None:
        return
    source_tags = _tags_for(retained)
    for model in model_registry.list_registered_models(db, bank):
        if model.lifecycle_state == "deleted":
            continue
        if _model_admits(model, source_tags):
            model_registry.withhold_model(db, bank, model.model_key, str(uuid4()))


def _issue(
    client: HindsightClient, bank_id: str, op: CurationOperation, retained: RetainedRecord,
    *, reason: str | None = None,
) -> None:
    """The one upstream call `op.action` desires, against the stable
    source-memory/document identity -- never re-derived from caller input.

    `reason` is not part of `CurationOperation`'s stored identity (SPEC does
    not require it for outcome-safety); it is best-effort forwarded on the
    triggering call only, and omitted on a later reconciliation retry.
    """
    if op.action == "correct":
        client.curate(bank_id, retained.source_memory_id, text=op.desired_content)
    elif op.action in ("forget", "expire"):
        client.curate(bank_id, retained.source_memory_id, state="invalidated", reason=reason)
    elif op.action == "restore":
        client.curate(bank_id, retained.source_memory_id, state="valid")
    elif op.action == "delete":
        client.delete_document(bank_id, retained.document_id)
    else:  # pragma: no cover -- defensive; action is server-assigned
        raise ValueError(f"unknown curation action {op.action!r}")


def _apply_lifecycle(retained: RetainedRecord, action: str, *, now: datetime) -> None:
    if action == "correct":
        return  # content itself already carries the correction
    if action == "forget":
        retained.lifecycle = "forgotten"
    elif action == "expire":
        retained.lifecycle = "expired"
    elif action == "restore":
        # SPEC §5.8: restore never resurrects an already-expired claim.
        retained.lifecycle = (
            "expired" if retained.valid_until is not None and retained.valid_until <= now else "active"
        )


def _mutate(
    db: Session,
    retained: RetainedRecord,
    *,
    action: str,
    client: HindsightClient,
    bank_id: str,
    desired_content: str | None = None,
    reason: str | None = None,
) -> CurationResult:
    if retained.source_memory_id is None and action != "delete":
        raise MemoryNotFound("no confirmed source memory to curate yet")

    bank = _bank_with_id(retained, bank_id)
    op = _accept_operation(db, bank, retained, action=action, desired_content=desired_content)
    db.commit()
    record_id = str(retained.id)

    try:
        _issue(client, bank_id, op, retained, reason=reason)
    except (MemoryNotFound, DocumentNotFound):
        if action not in _REMOVAL_ACTIONS:
            op.state = "needs_operator"
            withhold_bank(db, bank, op.operation_id)
            db.commit()
            raise CurationNeedsOperator(
                "the target memory is absent; this action cannot be proven safe to repeat automatically"
            ) from None
        # Absent already satisfies every removal action (SPEC §5.8).
    except HindsightOutcomeUnknown:
        op.state = "unknown"
        withhold_bank(db, bank, op.operation_id)
        db.commit()
        raise BankCurrentnessUnavailable(
            "this action's outcome could not be confirmed; the bank is withheld pending reconciliation"
        ) from None

    if action == "correct":
        retained.canonical_content = desired_content
    if action == "delete":
        db.query(CurationOperation).filter_by(retained_record_id=retained.id).delete()
        db.delete(retained)
    else:
        _apply_lifecycle(retained, action, now=_db_now(db))
        op.state = "completed"
        op.completed_at = _db_now(db)
        _withhold_affected_models(db, bank, retained)
    ready_bank(db, bank, op.operation_id)
    db.commit()
    return CurationResult(state="completed", record_id=record_id, action=action)


def correct_record(
    db: Session, retained: RetainedRecord, content: str, *, client: HindsightClient, bank_id: str
) -> CurationResult:
    return _mutate(db, retained, action="correct", client=client, bank_id=bank_id, desired_content=content)


def forget_record(
    db: Session, retained: RetainedRecord, *, reason: str | None = None, client: HindsightClient, bank_id: str
) -> CurationResult:
    # `reason` rides along on the upstream call but is not part of the
    # idempotency identity: retrying the same forget with a different reason
    # string is still the same desired outcome.
    return _mutate(db, retained, action="forget", client=client, bank_id=bank_id, reason=reason)


def restore_record(
    db: Session, retained: RetainedRecord, *, client: HindsightClient, bank_id: str
) -> CurationResult:
    return _mutate(db, retained, action="restore", client=client, bank_id=bank_id)


def delete_record(
    db: Session, retained: RetainedRecord, *, client: HindsightClient, bank_id: str
) -> CurationResult:
    return _mutate(db, retained, action="delete", client=client, bank_id=bank_id)


def reconcile_bank_once(db: Session, bank: LogicalBankRef, *, client: HindsightClient) -> CurationResult:
    """At most one inspection or mutation against this bank's oldest
    unknown-outcome operation, applying SPEC §5.8's terminal rules. Never
    loops -- an authorized access calls this at most once per request."""
    _lock_bank(db, bank)
    op = db.scalar(
        select(CurationOperation)
        .where(*_bank_filters(CurationOperation, bank), CurationOperation.state == "unknown")
        .order_by(CurationOperation.created_at)
        .with_for_update()
    )
    if op is None:
        # Nothing pending to reconcile. If the bank is still withheld, it is
        # blocked by an operation this pass did not choose to touch (e.g.
        # already resolved through its own direct call) -- `ready_bank` only
        # ever releases a barrier for the exact operation_id that set it, so
        # there is nothing safe to clear here.
        db.commit()
        return CurationResult(state="completed", record_id="", action="none")

    retained = db.get(RetainedRecord, op.retained_record_id)
    record_id = str(op.retained_record_id)
    if retained is None:
        # The claim itself is already gone (e.g. a prior delete completed
        # this exact operation concurrently) -- nothing left to reconcile.
        db.delete(op)
        ready_bank(db, bank, "")
        db.commit()
        return CurationResult(state="completed", record_id=record_id, action=op.action)

    try:
        if op.action == "delete":
            client.get_document(bank.bank_id, retained.document_id)
        else:
            client.get_memory(bank.bank_id, retained.source_memory_id)
        present = True
    except (MemoryNotFound, DocumentNotFound):
        present = False

    if not present:
        if op.action in _REMOVAL_ACTIONS:
            if op.action == "delete":
                db.query(CurationOperation).filter_by(retained_record_id=retained.id).delete()
                db.delete(retained)
            else:
                _apply_lifecycle(retained, op.action, now=_db_now(db))
            ready_bank(db, bank, op.operation_id)
            db.commit()
            return CurationResult(state="completed", record_id=record_id, action=op.action)
        op.state = "needs_operator"
        db.commit()
        return CurationResult(state="needs_operator", record_id=record_id, action=op.action)

    # Present: the same desired mutation is always safely repeatable here --
    # it is idempotent in effect (re-invalidate, re-validate, re-delete, or
    # re-write the SAME recorded desired_content).
    try:
        _issue(client, bank.bank_id, op, retained)
    except HindsightOutcomeUnknown:
        db.commit()
        return CurationResult(state="unknown", record_id=record_id, action=op.action)
    except MemoryNotCuratable:
        op.state = "needs_operator"
        db.commit()
        return CurationResult(state="needs_operator", record_id=record_id, action=op.action)

    if op.action == "correct":
        retained.canonical_content = op.desired_content
    if op.action == "delete":
        db.query(CurationOperation).filter_by(retained_record_id=retained.id).delete()
        db.delete(retained)
    else:
        _apply_lifecycle(retained, op.action, now=_db_now(db))
        op.state = "completed"
        op.completed_at = _db_now(db)
        _withhold_affected_models(db, bank, retained)
    ready_bank(db, bank, op.operation_id)
    db.commit()
    return CurationResult(state="completed", record_id=record_id, action=op.action)
