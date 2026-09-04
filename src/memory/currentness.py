"""Logical-bank current-read barriers fenced by exact operation identity."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory.models import BankCurrentness
from memory.retained_records import LogicalBankRef, _bank_filters, _lock_bank


def _query(bank: LogicalBankRef):
    return select(BankCurrentness).where(*_bank_filters(BankCurrentness, bank))


def _db_now(db: Session):
    return db.execute(select(func.now())).scalar_one()


def _locked(db: Session, bank: LogicalBankRef) -> BankCurrentness | None:
    return db.scalar(_query(bank).with_for_update())


def withhold_bank(db: Session, bank: LogicalBankRef, operation_id: str) -> BankCurrentness:
    _lock_bank(db, bank)
    row = _locked(db, bank)
    if row is None:
        row = BankCurrentness(
            tenant_id=bank.tenant_id,
            scope=bank.scope,
            user_id=bank.user_id,
            project_internal_id=bank.project_internal_id,
            state="withheld",
            blocking_operation_id=operation_id,
        )
        db.add(row)
    else:
        row.state = "withheld"
        row.blocking_operation_id = operation_id
        row.repair_not_before = None
        row.updated_at = _db_now(db)
    db.flush()
    return row


def ready_bank(db: Session, bank: LogicalBankRef, operation_id: str) -> BankCurrentness:
    _lock_bank(db, bank)
    row = _locked(db, bank)
    if row is None:
        row = BankCurrentness(
            tenant_id=bank.tenant_id,
            scope=bank.scope,
            user_id=bank.user_id,
            project_internal_id=bank.project_internal_id,
            state="ready",
            blocking_operation_id=None,
        )
        db.add(row)
    elif row.blocking_operation_id == operation_id:
        row.state = "ready"
        row.blocking_operation_id = None
        row.repair_not_before = None
        row.updated_at = _db_now(db)
    db.flush()
    return row


def bank_is_withheld(db: Session, bank: LogicalBankRef) -> bool:
    row = db.scalar(_query(bank))
    return row is not None and row.state == "withheld"
