"""Bounded, access-driven expiry of time-bounded retained claims.

No daemon and no scheduled activation exists (SPEC §6.4): an authorized
recall/reflect access is what performs at most one ordered, claimed batch of
overdue expiring records. A batch whose Hindsight outcome cannot be proven
withholds the physical bank exactly like any other outcome-safe curation
mutation (SPEC §5.8); a batch that proves everything it touched but still
leaves a backlog withholds only the triggering access, so the backlog
converges over successive accesses rather than blocking forever or running
unbounded work in one request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory.curation_service import _operation_id_for
from memory.currentness import withhold_bank
from memory.errors import BankCurrentnessUnavailable, MemoryNotFound
from memory.hindsight.client import HindsightClient, HindsightOutcomeUnknown
from memory.models import CurationOperation, RetainedRecord
from memory.retained_records import LogicalBankRef, _bank_filters

_BATCH_SIZE = 32


@dataclass(frozen=True)
class ExpiryResult:
    expired: int
    remaining: int


def _due_clause(bank: LogicalBankRef, now: datetime) -> tuple:
    return (
        *_bank_filters(RetainedRecord, bank),
        RetainedRecord.lifecycle == "active",
        RetainedRecord.valid_until.is_not(None),
        RetainedRecord.valid_until <= now,
    )


def expire_due_once(
    db: Session, bank: LogicalBankRef, *, client: HindsightClient, now: datetime
) -> ExpiryResult:
    """At most one ordered, claimed batch of overdue expiring records for
    this bank. Never loops -- an authorized access calls this at most once
    per request. `skip_locked` lets a concurrent access on the same bank
    claim a disjoint batch instead of blocking on this one."""
    rows = db.scalars(
        select(RetainedRecord)
        .where(*_due_clause(bank, now))
        .order_by(
            RetainedRecord.valid_until, RetainedRecord.recorded_at, RetainedRecord.document_id
        )
        .with_for_update(skip_locked=True)
        .limit(_BATCH_SIZE)
    ).all()

    expired = 0
    for record in rows:
        try:
            if record.source_memory_id is not None:
                client.curate(bank.bank_id, record.source_memory_id, state="invalidated")
        except MemoryNotFound:
            pass  # already gone -- expiry still completes (SPEC §5.8's removal rule)
        except HindsightOutcomeUnknown:
            operation_id = _operation_id_for(record.id, "expire", None)
            db.add(
                CurationOperation(
                    operation_id=operation_id,
                    retained_record_id=record.id,
                    tenant_id=bank.tenant_id,
                    scope=bank.scope,
                    user_id=bank.user_id,
                    project_internal_id=bank.project_internal_id,
                    action="expire",
                    desired_content=None,
                    state="unknown",
                )
            )
            withhold_bank(db, bank, operation_id)
            db.commit()
            raise BankCurrentnessUnavailable(
                "an expiry outcome could not be confirmed; the bank is withheld pending reconciliation"
            ) from None
        record.lifecycle = "expired"
        expired += 1

    db.flush()
    remaining = (
        db.scalar(select(func.count()).select_from(RetainedRecord).where(*_due_clause(bank, now)))
        or 0
    )
    db.commit()
    return ExpiryResult(expired=expired, remaining=remaining)


def ensure_no_expiry_backlog(
    db: Session, bank: LogicalBankRef, *, client: HindsightClient, now: datetime
) -> None:
    """Run one bounded expiry batch as part of an authorized recall/reflect
    access, then withhold THIS access if a backlog remains: access-driven
    expiry converges over successive accesses, never one unbounded pass."""
    result = expire_due_once(db, bank, client=client, now=now)
    if result.remaining > 0:
        raise BankCurrentnessUnavailable(
            "expiry_cleanup_pending: more overdue claims remain than one access processes"
        )
