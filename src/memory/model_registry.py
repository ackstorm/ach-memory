"""Transactional ACH mental-model registration and delivery state."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory.errors import MentalModelNotFound, MentalModelQuotaExceeded
from memory.models import MentalModelRegistration
from memory.retained_records import LogicalBankRef, _bank_filters, _lock_bank

MAX_CUSTOM_MODELS = 5


def _models_query(bank: LogicalBankRef):
    return select(MentalModelRegistration).where(*_bank_filters(MentalModelRegistration, bank))


def _model_query(bank: LogicalBankRef, model_key: str):
    return _models_query(bank).where(MentalModelRegistration.model_key == model_key)


def _db_now(db: Session):
    return db.execute(select(func.now())).scalar_one()


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
    always_in_context: bool = False,
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
        always_in_context=always_in_context,
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
    db: Session, bank: LogicalBankRef, model_key: str, upstream_model_id: str
) -> MentalModelRegistration:
    """Record the upstream id Hindsight assigned and leave `creating` for
    `active` -- the second half of create, run only after Hindsight's call
    actually succeeds."""
    row = _locked_model(db, bank, model_key)
    row.upstream_model_id = upstream_model_id
    row.lifecycle_state = "active"
    row.updated_at = _db_now(db)
    db.flush()
    return row


def mark_deleted(db: Session, bank: LogicalBankRef, model_key: str) -> MentalModelRegistration:
    row = _locked_model(db, bank, model_key)
    row.lifecycle_state = "deleted"
    row.updated_at = _db_now(db)
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
    row.updated_at = _db_now(db)
    db.flush()
    return row


def ready_model(
    db: Session, bank: LogicalBankRef, model_key: str, operation_id: str
) -> MentalModelRegistration:
    row = _locked_model(db, bank, model_key)
    if row.refresh_operation_id != operation_id:
        return row
    now = _db_now(db)
    row.delivery_state = "ready"
    row.refresh_status = "succeeded"
    row.repair_not_before = None
    row.last_refreshed_at = now
    row.updated_at = now
    db.flush()
    return row
