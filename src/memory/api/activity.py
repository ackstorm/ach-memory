"""Reading the activity trail. Master key only, tenant-filtered always.

Separate from api/admin.py because that module is the destructive plane
(whole-bank clear and delete) and this one is read-only telemetry. Keeping
them apart keeps the file that can erase a bank small enough to read in one
sitting.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory.api.app import require_master
from memory.api.memory import MAX_PAGE_SIZE
from memory.auth.principal import Principal
from memory.db import get_session
from memory.identifiers import is_unstorable
from memory.models import ActivityEvent

router = APIRouter(prefix="/v1/admin/activity", tags=["admin"])

HOURS = 24


class ActivityResponse(BaseModel):
    id: str
    created_at: str
    credential_id: str | None
    action: str
    surface: str
    scope: str
    user_id: str | None
    project_slug: str | None
    bank_fingerprint: str
    document_id: str | None
    content_bytes: int | None
    agent: str | None
    outcome: str
    error_code: str | None
    duration_ms: int


class FleetRow(BaseModel):
    scope: str
    user_id: str | None
    project_slug: str | None
    agent: str | None
    bank_fingerprint: str
    calls: int
    retains: int
    recalls: int
    errors: int
    bytes_written: int
    last_seen: str
    # Oldest first, one per hour, so the console renders it left to right.
    hours: list[int]


def _response(row: ActivityEvent) -> ActivityResponse:
    """Built field by field, never by serializing the row -- the same rule
    admin._audit_response states: a column added later must not leak through
    this endpoint by default."""
    return ActivityResponse(
        id=row.id,
        created_at=row.created_at.isoformat(),
        credential_id=row.credential_id,
        action=row.action,
        surface=row.surface,
        scope=row.scope,
        user_id=row.user_id,
        project_slug=row.project_slug,
        bank_fingerprint=row.bank_fingerprint,
        document_id=row.document_id,
        content_bytes=row.content_bytes,
        agent=row.agent,
        outcome=row.outcome,
        error_code=row.error_code,
        duration_ms=row.duration_ms,
    )


@router.get("", response_model=list[ActivityResponse])
def list_activity(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
    action: str | None = None,
    user_id: str | None = None,
    project_slug: str | None = None,
    scope: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ActivityResponse]:
    """Filters, not lookups: a value Postgres cannot store matches nothing,
    so an empty result IS the correct answer. Unguarded, psycopg raises
    DataError at parameter adaptation, which no `except` here catches and
    which surfaces as a 500 blaming us for a caller's typo -- the same guard
    admin.list_audit already carries."""
    if any(is_unstorable(v) for v in (action, user_id, project_slug, scope, outcome)):
        return []

    stmt = select(ActivityEvent).where(ActivityEvent.tenant_id == principal.tenant_id)
    for column, value in (
        (ActivityEvent.action, action),
        (ActivityEvent.user_id, user_id),
        (ActivityEvent.project_slug, project_slug),
        (ActivityEvent.scope, scope),
        (ActivityEvent.outcome, outcome),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if since is not None:
        stmt = stmt.where(ActivityEvent.created_at >= since)

    # created_at DESC, id DESC -- not created_at alone. Rows from one burst
    # share a timestamp, so created_at is not a total order and repeated
    # calls would disagree with each other. Same tiebreak, same limits, as
    # admin.list_audit.
    stmt = (
        stmt.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_response(row) for row in db.scalars(stmt).all()]


@router.get("/summary", response_model=list[FleetRow])
def summary(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
    hours: Annotated[int, Query(ge=1, le=168)] = HOURS,
) -> list[FleetRow]:
    """The console's Fleet screen in one request.

    Two queries, not one per row: the totals, and the hourly histogram, both
    grouped the same way and merged here. A per-row query would be N+1 on
    the one screen an operator refreshes constantly.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    group = (
        ActivityEvent.scope,
        ActivityEvent.user_id,
        ActivityEvent.project_slug,
        ActivityEvent.agent,
        ActivityEvent.bank_fingerprint,
    )
    where = (ActivityEvent.tenant_id == principal.tenant_id, ActivityEvent.created_at >= since)

    totals = db.execute(
        select(
            *group,
            func.count().label("calls"),
            func.count().filter(ActivityEvent.action == "memory.retain").label("retains"),
            func.count().filter(ActivityEvent.action == "memory.recall").label("recalls"),
            func.count().filter(ActivityEvent.outcome == "error").label("errors"),
            func.coalesce(func.sum(ActivityEvent.content_bytes), 0).label("bytes_written"),
            func.max(ActivityEvent.created_at).label("last_seen"),
        )
        .where(*where)
        .group_by(*group)
    ).all()

    # Explicit third argument, not the 2-arg form: date_trunc's hour boundary
    # is otherwise the CONNECTION's session TimeZone, which nothing in db.py
    # pins to UTC. `slots` below is built from datetime.now(UTC) -- if the
    # session zone ever drifts from UTC by a fractional-hour offset (e.g.
    # Asia/Kolkata, UTC+5:30), the SQL and Python hour boundaries disagree,
    # every lookup below misses, and this silently returns 24 zeros: the
    # operator console's primary "which agent went quiet" signal, dark with
    # no error anywhere. Looks redundant -- is not; do not simplify away.
    bucket = func.date_trunc("hour", ActivityEvent.created_at, "UTC").label("bucket")
    hourly: dict[tuple, dict] = {}
    for row in db.execute(
        select(*group, bucket, func.count().label("calls")).where(*where).group_by(*group, bucket)
    ).all():
        hourly.setdefault(row[:5], {})[row.bucket] = row.calls

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    slots = [now - timedelta(hours=h) for h in range(hours - 1, -1, -1)]

    return [
        FleetRow(
            scope=t.scope,
            user_id=t.user_id,
            project_slug=t.project_slug,
            agent=t.agent,
            bank_fingerprint=t.bank_fingerprint,
            calls=t.calls,
            retains=t.retains,
            recalls=t.recalls,
            errors=t.errors,
            bytes_written=t.bytes_written,
            last_seen=t.last_seen.isoformat(),
            hours=[hourly.get(t[:5], {}).get(slot, 0) for slot in slots],
        )
        for t in totals
    ]
