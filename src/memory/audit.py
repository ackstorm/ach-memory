"""The governance journal: one row per mutation (SPEC §3.4, invariant I4)."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.models import AuditEvent


def record(
    db: Session, *, principal: Principal, action: str, resource: str,
    operation_id: str | None = None, details: dict | None = None,
) -> AuditEvent:
    """Append an audit event. Caller commits. Returns the row."""
    event = AuditEvent(
        id=f"aud_{uuid4().hex}",
        actor_user_id=principal.user_id,
        on_behalf_of=principal.on_behalf_of,
        action=action,
        resource=resource,
        operation_id=operation_id,
        details=details,
    )
    db.add(event)
    return event


def find_retain(db: Session, operation_id: str) -> AuditEvent | None:
    """The latest `memory.retain` journal row for this operation, or None."""
    return db.scalars(
        select(AuditEvent)
        .where(AuditEvent.operation_id == operation_id, AuditEvent.action == "memory.retain")
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    ).first()
