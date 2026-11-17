"""The governance journal: one row per mutation (SPEC §3.4, invariant I4), and the read log."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.config import get_settings
from memory.errors import InvalidRequest
from memory.models import AuditEvent, ProjectSlug, ReadEvent
from memory.sanitization import contains_secret
from memory.slugs import normalize_slug

#: Read queries keep their text this long; length, relevance and latency stay.
QUERY_TEXT_DAYS = 30


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
    relevance: float | None = None, session_id: str | None = None,
) -> None:
    """One read (SPEC §7 `read_events`). A query shaped like a secret is not stored, nor any
    query of a `withhold_query_users` subject; its length always is. Each session start
    clears query text older than QUERY_TEXT_DAYS."""
    project_id = None
    if project_slug:
        try:
            mapping = db.get(ProjectSlug, normalize_slug(project_slug))
        except InvalidRequest:
            mapping = None
        project_id = mapping.project_internal_id if mapping else None
    requested = project_slug[:128] if project_slug and not project_id else None
    if not query or principal.subject in get_settings().withhold_query_users:
        stored = None
    else:
        stored = "[withheld: looks like a secret]" if contains_secret(query) else query
    db.add(ReadEvent(
        id=f"rd_{uuid4().hex}", actor_user_id=principal.on_behalf_of or principal.user_id,
        project_internal_id=project_id, project_slug=requested, session_id=session_id and session_id[:128],
        tool=tool, scope=scope,
        query=stored, query_chars=len(query) if query else None,
        family=family, results=results, relevance=relevance, latency_ms=latency_ms,
    ))
    if tool == "load_context":
        db.execute(update(ReadEvent)
                   .where(ReadEvent.query.is_not(None),
                          ReadEvent.created_at < datetime.now(UTC) - timedelta(days=QUERY_TEXT_DAYS))
                   .values(query=None))
