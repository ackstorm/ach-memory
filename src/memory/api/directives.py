"""SPEC §14: directives are REST API-only, never an MCP tool.

A directive is a standing rule attached to a bank -- "Always use uv for Python
dependency management." For project scope that rule is shared by every user
and every agent on the project: one directive changes behavior service-wide,
not just for whoever wrote it. That is the whole justification for keeping
this off the LLM-facing MCP surface (§14.1) -- an agent must not be able to
steer *other people's* agents by writing a rule for itself. This is a surface
restriction, not a new permission model: the same §7 bank authorization
(owner, group member, or a master key for any bank in its tenant) still
governs who may call these routes.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from memory.api.app import current_on_behalf_of, current_principal
from memory.api.memory import (
    MemoryResponse,
    ScopedRequest,
    _resolve_bank,
    _strip_bank_id,
    scoped_query_params,
)
from memory.auth.principal import Principal
from memory.db import get_session
from memory.hindsight.client import get_client

router = APIRouter(prefix="/v1/directives", tags=["directives"])


class CreateDirectiveRequest(ScopedRequest):
    name: str
    content: str
    priority: int | None = None
    is_active: bool | None = None
    # No "tags" field: Hindsight's directive tags are an in-bank execution
    # scope this service does not model (SPEC §14) -- never exposed, never
    # sent upstream.


class UpdateDirectiveRequest(ScopedRequest):
    name: str | None = None
    content: str | None = None
    priority: int | None = None
    is_active: bool | None = None


def _bank(
    body: ScopedRequest,
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
    *,
    is_write: bool = False,
) -> tuple[str, str | None, str | None]:
    """Authorize first, always -- same shape as curation.py/documents.py.

    create=False: a directive route is maintenance over an existing bank
    (SPEC §11.3), never first-touch project creation.
    """
    bank_id, resolved_from, project_slug = _resolve_bank(
        body, db, principal, on_behalf_of, action, create=False, is_write=is_write
    )
    db.commit()
    return bank_id, resolved_from, project_slug


@router.post("", response_model=MemoryResponse, status_code=201)
def create_directive(
    body: CreateDirectiveRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    """Create a standing rule on a bank.

    Not an MCP tool (SPEC §14.1): for project scope, this rule steers every
    other user's and agent's future prompts on that project -- an agent
    cannot be allowed to write governance for other agents it shares a
    project with.

    Calls `ensure_bank` first, unlike every other route in this file: a
    directive route is normally maintenance over an existing bank (`_bank`'s
    `create=False`), but the underlying Hindsight bank can still be
    brand-new -- nothing has necessarily ever `retain`'d into it. Without
    this, POST on such a bank 500s upstream (folded into 502 HINDSIGHT_ERROR
    by `HindsightClient._request`), measured live. Letting Hindsight
    auto-create the bank instead (as `create_mental_model` does) would not
    avoid that 500 -- only `create_directive` needs the row to pre-exist
    (SPEC §19.5) -- so the PUT upsert stays only on this route.
    """
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "directives.create", is_write=True
    )
    client = get_client()
    client.ensure_bank(bank_id)
    result = client.create_directive(
        bank_id,
        name=body.name,
        content=body.content,
        priority=body.priority,
        is_active=body.is_active,
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.get("", response_model=MemoryResponse)
def list_directives(
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
    active_only: bool | None = None,
    limit: Annotated[int | None, Query(ge=0)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "directives.list"
    )
    result = get_client().list_directives(
        bank_id, active_only=active_only, limit=limit, offset=offset
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.get("/{directive_id}", response_model=MemoryResponse)
def get_directive(
    directive_id: str,
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "directives.get"
    )
    result = get_client().get_directive(bank_id, directive_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.patch("/{directive_id}", response_model=MemoryResponse)
def update_directive(
    directive_id: str,
    body: UpdateDirectiveRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "directives.update", is_write=True
    )
    result = get_client().update_directive(
        bank_id,
        directive_id,
        name=body.name,
        content=body.content,
        priority=body.priority,
        is_active=body.is_active,
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.delete("/{directive_id}", response_model=MemoryResponse)
def delete_directive(
    directive_id: str,
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "directives.delete", is_write=True
    )
    result = get_client().delete_directive(bank_id, directive_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
