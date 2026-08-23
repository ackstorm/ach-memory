from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import audit
from memory.api.app import current_on_behalf_of, require_master
from memory.api.memory import (
    MAX_PAGE_SIZE,
    MemoryResponse,
    ScopedRequest,
    _resolve_bank,
    _strip_bank_id,
)
from memory.auth.principal import Principal
from memory.db import get_session
from memory.errors import RetiredSlugNotFound
from memory.hindsight.client import get_client
from memory.identifiers import is_unstorable, reject_control_characters
from memory.models import AuditEvent, RetiredSlug
from memory.slugs import normalize_slug

router = APIRouter(prefix="/v1/admin", tags=["admin"])

Scope = Literal["user", "project"]
MemoryType = Literal["world", "experience", "observation"]


class AuditEventResponse(BaseModel):
    id: str
    actor_key_id: str | None
    on_behalf_of: str | None
    action: str
    resource: str
    created_at: str


def _audit_response(event: AuditEvent) -> AuditEventResponse:
    """Built field by field, like projects._response. Never serialize the
    row: the table is a disclosure surface the moment it is readable, and a
    column added later (say, one that DID carry a bank_id) must not leak
    through this endpoint by default."""
    return AuditEventResponse(
        id=event.id,
        actor_key_id=event.actor_key_id,
        on_behalf_of=event.on_behalf_of,
        action=event.action,
        resource=event.resource,
        created_at=event.created_at.isoformat(),
    )


@router.get("/audit", response_model=list[AuditEventResponse])
def list_audit(
    principal: Annotated[Principal, Depends(require_master)],
    db: Session = Depends(get_session),
    action: str | None = None,
    actor_key_id: str | None = None,
    on_behalf_of: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditEventResponse]:
    """SPEC §6.1's mitigation, made readable. Tenant-filtered always, and the
    page size is bounded (le=MAX_PAGE_SIZE) so this can never become an
    unbounded dump of the tenant's whole history.

    Ordered by created_at DESC, id DESC -- not created_at alone. Several
    events from one request (e.g. a project creation that also transfers)
    can share a timestamp, so created_at by itself is not a total order: the
    same query could return a different row order on every call. The id
    tiebreak only fixes THAT -- it buys determinism, not recency. AuditEvent.id
    is `ids.new_audit_id()`, a random uuid4 hex uncorrelated with insertion
    order, so it does not recover which of two same-timestamp events actually
    happened first; it only makes repeated calls agree with each other.

    Not every master-key action lands here: GET /v1/users/{id}, GET
    /v1/groups[/{id}], and GET /v1/projects[/{slug}] write no event. They are
    identity-metadata reads with no memory content -- the same reasoning
    that already exempts a user reading their own memory -- and instrumenting
    them would mean a routine `GET /v1/projects` on every agent start drowns
    the log that matters. Every mutation, every delegated bank access, and
    every admin destructive operation IS recorded; see README.md.
    """
    # Filters, not lookups: a value Postgres cannot store matches nothing, so
    # an empty result IS the correct answer. Unguarded, psycopg raises
    # DataError at parameter adaptation -- not an IntegrityError, so no
    # `except` in this service catches it and it reaches api/app.py's
    # catch-all as a 500, reporting a caller mistake as a backend fault.
    if any(is_unstorable(v) for v in (action, actor_key_id, on_behalf_of)):
        return []
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == principal.tenant_id)
    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)
    if actor_key_id is not None:
        stmt = stmt.where(AuditEvent.actor_key_id == actor_key_id)
    if on_behalf_of is not None:
        stmt = stmt.where(AuditEvent.on_behalf_of == on_behalf_of)
    if since is not None:
        stmt = stmt.where(AuditEvent.created_at >= since)
    stmt = (
        stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.scalars(stmt).all()
    return [_audit_response(row) for row in rows]


def _admin_scope(
    scope: Scope, user_id: str | None, project_slug: str | None
) -> ScopedRequest:
    return ScopedRequest(scope=scope, user_id=user_id, project_slug=project_slug)


@router.post("/memory/{scope}/clear", response_model=MemoryResponse)
def clear_memories(
    scope: Scope,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
    user_id: str | None = None,
    project_slug: str | None = None,
    type: MemoryType | None = None,
) -> MemoryResponse:
    """SPEC §11.7 + §16.4: admin API + master key only, never advertised over
    MCP -- "an LLM that decides memory is 'stale' will use them." A user key
    that owns this very bank is still refused: `require_master` gates on the
    credential alone, before scope/ownership are ever resolved.

    `create=False` (via `_resolve_bank`): an admin must not be able to
    conjure a bank into existence by "clearing" one that never existed.
    """
    body = _admin_scope(scope, user_id, project_slug)
    bank_id, resolved_from, resolved_slug = _resolve_bank(
        body, db, principal, on_behalf_of, "admin.memory.clear", create=False
    )
    # Commit AFTER the upstream call, unlike every other route in this
    # service. Elsewhere the committed state is "a master key touched this
    # bank" or an enriched locator -- true whatever Hindsight does next. Here
    # the audited action IS the compliance claim that SPEC §12.3's only
    # complete erasure path completed, so a 502 must not leave a row saying
    # it did. `create=False` above means resolution created nothing, so
    # there is no local state that needs to survive the failure.
    result = get_client().clear_memories(bank_id, type=type)
    db.commit()
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=resolved_slug,
    )


@router.delete("/memory/{scope}", response_model=MemoryResponse)
def delete_bank(
    scope: Scope,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
    user_id: str | None = None,
    project_slug: str | None = None,
) -> MemoryResponse:
    """SPEC §11.7 + §12.3: irreversible, whole-bank. For scope=user this is
    the only complete right-to-erasure path for a departing user.

    Deliberately does not touch the `users` row: `User.bank_id` is NOT NULL,
    so there is no schema-safe way to clear it, and deleting the row would
    cascade into that user's API keys / group memberships / project
    ownership -- consequences far outside "erase this bank's memory content."
    A bank_id whose Hindsight bank has been torn down is no different from
    one freshly allocated and never materialized (SPEC §17): the next retain
    against it just auto-creates an empty bank under the same id (measured
    live: Hindsight creates a bank on first retain, no upsert needed). Same
    `create=False` reasoning as clear above.
    """
    body = _admin_scope(scope, user_id, project_slug)
    bank_id, resolved_from, resolved_slug = _resolve_bank(
        body, db, principal, on_behalf_of, "admin.memory.delete", create=False
    )
    # Commit AFTER the upstream call, unlike every other route in this
    # service. Elsewhere the committed state is "a master key touched this
    # bank" or an enriched locator -- true whatever Hindsight does next. Here
    # the audited action IS the compliance claim that SPEC §12.3's only
    # complete erasure path completed, so a 502 must not leave a row saying
    # it did. `create=False` above means resolution created nothing, so
    # there is no local state that needs to survive the failure.
    result = get_client().delete_bank(bank_id)
    db.commit()
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=resolved_slug,
    )


@router.post("/slugs/{retired_slug}/release", status_code=204)
def release_slug(
    retired_slug: str,
    principal: Annotated[Principal, Depends(require_master)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> Response:
    """SPEC §8.6: an explicit admin action, never automatic -- a
    self-expiring tombstone would silently let a caller who never saw the
    rename create a second, empty project under the old name, exactly what
    the tombstone exists to prevent.

    Deletes only the tombstone row. The project it forwarded FROM keeps its
    CURRENT slug untouched; this never reverts, renames, or deletes anything
    on the project itself. Tenant-scoped via the composite primary key, same
    as every other tombstone lookup in this codebase.
    """
    reject_control_characters(retired_slug, RetiredSlugNotFound)
    # normalize_slug like every other slug lookup in this service. Without it
    # `POST /v1/admin/slugs/Payments-API/release` 404s against a tombstone
    # stored as `payments-api` -- on the one route whose whole purpose is an
    # operator typing a name by hand.
    retired_slug = normalize_slug(retired_slug)
    tombstone = db.get(RetiredSlug, (principal.tenant_id, retired_slug))
    if tombstone is None:
        raise RetiredSlugNotFound("no such retired slug", retired_slug=retired_slug)
    db.delete(tombstone)
    audit.record(db, principal, "slug.release", retired_slug, on_behalf_of=on_behalf_of)
    db.commit()
    return Response(status_code=204)
