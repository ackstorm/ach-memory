"""ACH's own record of each memory (SPEC §7 `memories`): title, family, pin, and the state and
size that the index and the pin cap read without an engine call. Every mutation passes through
ACH, so the curation functions keep it in step with the engine."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory.bank_ref import LogicalBankRef
from memory.config import get_settings
from memory.errors import PinCapExceeded
from memory.models import Memory


def add(db: Session, ref: LogicalBankRef, memory_id: str, *, title: str, family: str | None,
        pinned: bool, memory_type: str, chars: int, created_at: datetime | None = None) -> Memory:
    row = Memory(memory_id=memory_id, scope=ref.scope, owner_id=ref.owner_id, title=title,
                 family=family, pinned=pinned, memory_type=memory_type, state="valid", chars=chars)
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.flush()
    return row


def get(db: Session, ref: LogicalBankRef, memory_id: str) -> Memory | None:
    """The row, only if it belongs to `ref`'s bank (I1)."""
    row = db.get(Memory, memory_id)
    return row if row is not None and row.owner_id == ref.owner_id else None


def by_ids(db: Session, ids: list[str]) -> dict[str, Memory]:
    if not ids:
        return {}
    return {row.memory_id: row for row in db.scalars(select(Memory).where(Memory.memory_id.in_(ids)))}


def valid(db: Session, owner_id: str) -> list[Memory]:
    """Valid memories of one bank, newest first."""
    return list(db.scalars(
        select(Memory).where(Memory.owner_id == owner_id, Memory.state == "valid")
        .order_by(Memory.created_at.desc(), Memory.memory_id)
    ))


def count_valid(db: Session, owner_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(Memory)
        .where(Memory.owner_id == owner_id, Memory.state == "valid")
    ) or 0


def pinned(db: Session, owner_id: str) -> list[Memory]:
    """Valid pinned memories of one bank, newest first."""
    return [row for row in valid(db, owner_id) if row.pinned]


def pin_cap(scope: str) -> int:
    settings = get_settings()
    return settings.pin_cap_project_chars if scope == "project" else settings.pin_cap_user_chars


def check_pin_cap(db: Session, ref: LogicalBankRef, chars: int, *, memory_id: str | None = None) -> None:
    """Refuse a pin of `chars` characters (replacing `memory_id`'s own, if pinned) that would put
    the bank's pinned text over its cap. Caller holds the bank lock."""
    rows = [row for row in pinned(db, ref.owner_id) if row.memory_id != memory_id]
    used, cap = sum(row.chars for row in rows), pin_cap(ref.scope)
    if used + chars <= cap:
        return
    listed = "; ".join(f'{row.memory_id} "{row.title}" ({row.chars} chars)' for row in rows) or "nothing"
    raise PinCapExceeded(
        f"pinned text in this {ref.scope} bank would reach {used + chars} chars, over its cap of "
        f"{cap}. Unpin or shorten one of these, or save without pin: {listed}"
    )
