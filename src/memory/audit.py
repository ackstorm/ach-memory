"""The governance journal: one row per mutation (SPEC §3.4, invariant I4), and the read log."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.errors import InvalidRequest
from memory.models import AuditEvent, ProjectSlug, ReadEvent
from memory.sanitization import contains_secret
from memory.slugs import normalize_slug


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


def find_memory_retain(db: Session, memory_id: str) -> AuditEvent | None:
    """The `memory.retain` journal row that minted this memory id, or None."""
    return db.scalars(
        select(AuditEvent)
        .where(AuditEvent.resource == memory_id, AuditEvent.action == "memory.retain")
        .limit(1)
    ).first()


def log_read(
    db: Session, *, principal: Principal, tool: str, scope: str | None, project_slug: str | None,
    results: int, latency_ms: int, query: str | None = None, family: str | None = None,
) -> None:
    """One read (SPEC §7 `read_events`). A query shaped like a secret is not stored."""
    project_id = None
    if project_slug:
        try:
            mapping = db.get(ProjectSlug, normalize_slug(project_slug))
        except InvalidRequest:
            mapping = None
        project_id = mapping.project_internal_id if mapping else None
    db.add(ReadEvent(
        id=f"rd_{uuid4().hex}", actor_user_id=principal.on_behalf_of or principal.user_id,
        project_internal_id=project_id, tool=tool, scope=scope,
        query="[withheld: looks like a secret]" if query and contains_secret(query) else query,
        family=family, results=results, latency_ms=latency_ms,
    ))
