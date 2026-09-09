"""Transactional ACH mental-model registration and delivery state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.db import db_now
from memory.errors import IdempotencyConflict, MentalModelNotFound, MentalModelQuotaExceeded
from memory.models import MentalModelMutation, MentalModelRegistration
from memory.retained_records import LogicalBankRef, _bank_filters, _lock_bank

MAX_CUSTOM_MODELS = 5


def _models_query(bank: LogicalBankRef):
    return select(MentalModelRegistration).where(*_bank_filters(MentalModelRegistration, bank))


def _model_query(bank: LogicalBankRef, model_key: str):
    return _models_query(bank).where(MentalModelRegistration.model_key == model_key)


def locked_bank_models(db: Session, bank: LogicalBankRef) -> list[MentalModelRegistration]:
    """Lock the logical bank and return every one of its registrations.

    Exposed so a caller that must validate something ELSE (e.g. a delivery
    token budget) against the same locked row set can do so before calling
    `register_model`/`update` without acquiring the same locks twice.
    """
    _lock_bank(db, bank)
    return list(db.scalars(_models_query(bank).with_for_update()).all())


def get_registered_model(
    db: Session, bank: LogicalBankRef, model_key: str
) -> MentalModelRegistration | None:
    """Unlocked read for ordinary get/list -- never provisions or locks."""
    return db.scalar(_model_query(bank, model_key))


def find_by_operation(
    db: Session, bank: LogicalBankRef, operation_id: str
) -> MentalModelRegistration | None:
    """A registration by its recorded mutation operation id, any lifecycle
    state -- the crash-recovery lookup a lost create response resumes from."""
    return db.scalar(
        _models_query(bank).where(MentalModelRegistration.mutation_operation_id == operation_id)
    )


def register_model(
    db: Session,
    bank: LogicalBankRef,
    *,
    origin: str,
    model_key: str,
    name: str,
    source_query: str,
    source_tags: list[str],
    tags_match: str,
    max_tokens: int,
    trigger: dict[str, object],
    builtin_key: str | None = None,
    definition_version: int | None = None,
    upstream_model_id: str | None = None,
    lifecycle_state: str = "active",
    mutation_operation_id: str | None = None,
    mutation_payload_hash: str | None = None,
    delivery_state: str = "ready",
    refresh_operation_id: str | None = None,
    refresh_status: str | None = None,
    repair_not_before=None,
    existing: list[MentalModelRegistration] | None = None,
) -> MentalModelRegistration:
    """`existing`, when given, must already be this bank's locked row set
    (from `locked_bank_models`) -- lets a caller that locked for its own
    reason (e.g. a budget check) skip re-querying here. Left None, this
    function locks and queries for itself exactly as before."""
    if origin not in {"builtin", "user"}:
        raise ValueError("model origin must be builtin or user")
    if (origin == "builtin") != (builtin_key is not None and definition_version is not None):
        raise ValueError("only built-ins have a key and definition version")

    if existing is None:
        existing = locked_bank_models(db, bank)
    live = [row for row in existing if row.lifecycle_state != "deleted"]
    if origin == "builtin" and any(row.origin == "builtin" for row in live):
        raise MentalModelQuotaExceeded("a logical bank may register only one built-in model")
    if origin == "user" and sum(row.origin == "user" for row in live) >= MAX_CUSTOM_MODELS:
        raise MentalModelQuotaExceeded(
            f"a logical bank may register at most {MAX_CUSTOM_MODELS} custom models"
        )

    row = MentalModelRegistration(
        model_key=model_key,
        tenant_id=bank.tenant_id,
        scope=bank.scope,
        user_id=bank.user_id,
        project_internal_id=bank.project_internal_id,
        upstream_model_id=upstream_model_id,
        name=name,
        source_query=source_query,
        source_tags=source_tags,
        tags_match=tags_match,
        max_tokens=max_tokens,
        trigger=trigger,
        origin=origin,
        builtin_key=builtin_key,
        definition_version=definition_version,
        lifecycle_state=lifecycle_state,
        mutation_operation_id=mutation_operation_id,
        mutation_payload_hash=mutation_payload_hash,
        # No caller can set this any more (v0.4.8 builtin-only-standing-context
        # Task 4); the column itself is NOT NULL with no server default until
        # Task 5 drops it, so the ORM insert still needs a value here.
        always_in_context=False,
        delivery_state=delivery_state,
        refresh_operation_id=refresh_operation_id,
        refresh_status=refresh_status,
        repair_not_before=repair_not_before,
    )
    db.add(row)
    db.flush()
    return row


def list_registered_models(db: Session, bank: LogicalBankRef) -> list[MentalModelRegistration]:
    return list(
        db.scalars(
            _models_query(bank).order_by(
                MentalModelRegistration.origin,
                MentalModelRegistration.model_key,
            )
        ).all()
    )


def _locked_model(db: Session, bank: LogicalBankRef, model_key: str) -> MentalModelRegistration:
    row = db.scalar(_model_query(bank, model_key).with_for_update())
    if row is None:
        raise MentalModelNotFound("no registered model with that logical key")
    return row


def activate_model(
    db: Session,
    bank: LogicalBankRef,
    model_key: str,
    upstream_model_id: str,
    refresh_operation_id: str | None = None,
) -> MentalModelRegistration:
    """Record the upstream id Hindsight assigned and leave `creating` for
    `active` -- the second half of create, run only after Hindsight's call
    actually succeeds."""
    row = _locked_model(db, bank, model_key)
    row.upstream_model_id = upstream_model_id
    row.lifecycle_state = "active"
    if refresh_operation_id is not None:
        row.delivery_state = "withheld"
        row.refresh_operation_id = refresh_operation_id
        row.refresh_status = "pending"
    row.updated_at = db_now(db)
    db.flush()
    return row


def mark_deleted(db: Session, bank: LogicalBankRef, model_key: str) -> MentalModelRegistration:
    row = _locked_model(db, bank, model_key)
    row.lifecycle_state = "deleted"
    row.updated_at = db_now(db)
    db.flush()
    return row


def withhold_model(
    db: Session, bank: LogicalBankRef, model_key: str, operation_id: str
) -> MentalModelRegistration:
    row = _locked_model(db, bank, model_key)
    row.delivery_state = "withheld"
    row.refresh_operation_id = operation_id
    row.refresh_status = "pending"
    row.repair_not_before = None
    row.updated_at = db_now(db)
    db.flush()
    return row


def require_model_refresh(
    db: Session,
    bank: LogicalBankRef,
    model_key: str,
    *,
    repair_not_before: datetime,
) -> MentalModelRegistration:
    """Withhold now, with no invented operation id -- a model this bank knows
    needs refreshing, but for which no upstream refresh has been submitted
    (or attempted-and-failed) yet. `repair_not_before` governs when a repair
    access is first eligible to pick this row up: the caller passes `now`
    for a freshly-required model (repairable immediately) or a backed-off
    time for one whose submission just failed."""
    row = _locked_model(db, bank, model_key)
    row.delivery_state = "withheld"
    row.refresh_operation_id = None
    row.refresh_status = "required"
    row.repair_not_before = repair_not_before
    row.updated_at = db_now(db)
    db.flush()
    return row


def record_model_refresh_operation(
    db: Session, bank: LogicalBankRef, model_key: str, operation_id: str
) -> MentalModelRegistration:
    """Replace a required/failed state with the exact upstream operation
    identity a refresh submission actually returned. Never invented locally
    -- `operation_id` must be Hindsight's own id, so later observation
    compares against the exact recorded value (SPEC §6.4)."""
    row = _locked_model(db, bank, model_key)
    row.refresh_operation_id = operation_id
    row.refresh_status = "pending"
    row.repair_not_before = None
    row.updated_at = db_now(db)
    db.flush()
    return row


def ready_model(
    db: Session, bank: LogicalBankRef, model_key: str, operation_id: str
) -> MentalModelRegistration:
    row = _locked_model(db, bank, model_key)
    if row.refresh_operation_id != operation_id:
        return row
    now = db_now(db)
    row.delivery_state = "ready"
    row.refresh_status = "succeeded"
    row.repair_not_before = None
    row.last_refreshed_at = now
    row.updated_at = now
    db.flush()
    return row


def mark_refresh_failed(
    db: Session,
    bank: LogicalBankRef,
    model_key: str,
    operation_id: str,
    *,
    repair_not_before,
) -> MentalModelRegistration:
    """A terminal non-success outcome for the exact recorded operation.
    Ignored (no-op) if `operation_id` no longer matches, same guard as
    `ready_model` -- a stale observer must never overwrite a newer state."""
    row = _locked_model(db, bank, model_key)
    if row.refresh_operation_id != operation_id:
        return row
    row.refresh_status = "failed"
    row.repair_not_before = repair_not_before
    row.updated_at = db_now(db)
    db.flush()
    return row


def _mutation_query(bank: LogicalBankRef, operation_id: str):
    return select(MentalModelMutation).where(
        *_bank_filters(MentalModelMutation, bank),
        MentalModelMutation.operation_id == operation_id,
    )


def accept_model_mutation(
    db: Session,
    bank: LogicalBankRef,
    *,
    model_key: str,
    operation_id: str,
    action: str,
    payload_hash: str,
) -> tuple[MentalModelMutation, bool]:
    """Idempotency ledger entry for update/refresh/delete (create keeps its
    own mechanism -- see `MentalModelMutation`'s docstring). Returns
    `(row, created)`: `created=False` with the existing row for an exact
    retry (same operation id, same canonical payload); raises
    `IdempotencyConflict` for a reused operation id whose payload, model or
    action differs."""
    _lock_bank(db, bank)
    existing = db.scalar(_mutation_query(bank, operation_id).with_for_update())
    if existing is not None:
        if (
            existing.payload_hash != payload_hash
            or existing.model_key != model_key
            or existing.action != action
        ):
            raise IdempotencyConflict(
                "operation_id was already used with a different canonical payload",
                operation_id=operation_id,
            )
        return existing, False

    row = MentalModelMutation(
        tenant_id=bank.tenant_id,
        scope=bank.scope,
        user_id=bank.user_id,
        project_internal_id=bank.project_internal_id,
        model_key=model_key,
        operation_id=operation_id,
        action=action,
        payload_hash=payload_hash,
        state="pending",
    )
    db.add(row)
    db.flush()
    return row, True


def complete_model_mutation(
    db: Session, mutation: MentalModelMutation, *, upstream_operation_id: str | None = None
) -> MentalModelMutation:
    mutation.state = "completed"
    mutation.upstream_operation_id = upstream_operation_id
    mutation.completed_at = db_now(db)
    db.flush()
    return mutation


def oldest_failed_model(
    db: Session, bank: LogicalBankRef, *, now
) -> MentalModelRegistration | None:
    """The one eligible registration a single repair access may act on:
    withheld and past its own backoff, oldest first -- either a previously
    failed refresh submission, or one newly marked `required` (no operation
    id was ever submitted for it, e.g. `require_model_refresh` recorded a
    safe state but the refresh request itself was never attempted or lost)."""
    _lock_bank(db, bank)
    return db.scalar(
        _models_query(bank)
        .where(
            MentalModelRegistration.lifecycle_state != "deleted",
            MentalModelRegistration.delivery_state == "withheld",
            MentalModelRegistration.refresh_status.in_(("failed", "required")),
            MentalModelRegistration.repair_not_before <= now,
        )
        .order_by(MentalModelRegistration.updated_at.asc())
        .with_for_update()
        .limit(1)
    )
