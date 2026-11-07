"""The logical bank a scope + principal resolves to (SPEC §7)."""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from memory import audit, projects
from memory.auth.principal import Principal
from memory.errors import InvalidRequest, MemoryNotFound


@dataclass(frozen=True)
class LogicalBankRef:
    scope: Literal["user", "project"]
    owner_id: str
    bank_id: str


def resolve_bank(
    db: Session, principal: Principal, scope: str, slug: str | None, *, create: bool
) -> tuple[LogicalBankRef, str | None]:
    """User scope addresses the caller, or whoever `on_behalf_of` names for
    an operator; project scope resolves `slug` through `projects.resolve`.
    Returns the bank ref and a notice (PROJECT_CREATED/PROJECT_RENAMED),
    None for user scope."""
    if scope == "user":
        owner_id = principal.on_behalf_of or principal.user_id
        return LogicalBankRef("user", owner_id, f"user_{owner_id}"), None

    if not slug:
        raise InvalidRequest("scope=project needs a project_slug")
    resolved = projects.resolve(db, principal, slug, create=create)
    ref = LogicalBankRef("project", resolved.project.internal_id, resolved.project.bank_id)
    return ref, resolved.notice


def lock_bank(db: Session, bank_id: str) -> None:
    """Serialize mutations against one logical bank for the rest of this transaction."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"memory:{bank_id}"})


def resolve_memory_bank(
    db: Session, principal: Principal, scope: str | None, slug: str | None, memory_id: str
) -> tuple[LogicalBankRef, str | None]:
    """`resolve_bank(create=False)` for a call that addresses one memory by id.
    `scope` may be omitted: it is read from the memory's retain journal row, and
    the bank the caller resolves to must own that row (I1) -- otherwise the
    memory does not exist for this caller."""
    if scope is not None:
        return resolve_bank(db, principal, scope, slug, create=False)
    row = audit.find_memory_retain(db, memory_id)
    if row is None:
        raise MemoryNotFound()
    ref, notice = resolve_bank(db, principal, row.details["scope"], slug, create=False)
    if ref.owner_id != row.details["owner"]:
        raise MemoryNotFound()
    return ref, notice
