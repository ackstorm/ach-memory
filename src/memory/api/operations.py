from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.orm import Session

from memory.api.app import current_on_behalf_of, current_principal
from memory.api.memory import (
    MAX_PAGE_SIZE,
    MemoryResponse,
    ScopedRequest,
    _resolve_bank,
    _strip_bank_id,
)
from memory.auth.principal import Principal
from memory.db import get_session
from memory.hindsight.client import get_client

router = APIRouter(prefix="/v1/memory/operations", tags=["operations"])


class ListOperationsRequest(ScopedRequest):
    status: str | None = None
    type: str | None = None
    # Bounded on both sides so an out-of-range value is a typed 422 at the
    # boundary, not a 502 blaming the backend for the caller's typo.
    # Defaulted to concrete values (20/0), NOT left unset like
    # ListMemoriesRequest in memory/api/curation.py -- this route always
    # sends both params to Hindsight rather than omitting them for
    # Hindsight's own defaults to apply. Same shape as ListDocumentsRequest
    # in memory/api/documents.py. High side capped at MAX_PAGE_SIZE (see
    # memory/api/memory.py); low side is ge=1, not ge=0 -- a zero-size page
    # is meaningless and was forwarded upstream verbatim.
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)


class OperationIdRequest(ScopedRequest):
    # UUID-shaped upstream, but deliberately NOT validated here: authorization
    # must run before an id is inspected at all, so a caller with no rights to
    # the bank gets 403 regardless of whether its id is well-formed (SPEC
    # §20.1). HindsightClient.get_operation/cancel_operation validate it
    # locally, after the bank check, and map a malformed id to
    # OperationNotFound.
    operation_id: str


def _bank(
    body: ScopedRequest,
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
    *,
    is_write: bool = False,
) -> tuple[str, str | None, str | None]:
    """Authorize first, always.

    create=False: these are lookups over operations that already exist, not
    first-touch creation (SPEC §11.3) -- an operation route on an unknown slug
    must 404, not squat the slug for whoever asked first.

    `is_write` forwards to `_resolve_bank`'s rate-limit gate (SPEC §20) --
    only `cancel` passes it.
    """
    bank_id, resolved_from, project_slug = _resolve_bank(
        body, db, principal, on_behalf_of, action, create=False, is_write=is_write
    )
    db.commit()
    return bank_id, resolved_from, project_slug


@router.post("/list", response_model=MemoryResponse)
def list_operations(
    body: ListOperationsRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "memory.operations.list"
    )
    result = get_client().list_operations(
        bank_id,
        status=body.status,
        type=body.type,
        limit=body.limit,
        offset=body.offset,
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/get", response_model=MemoryResponse)
def get_operation(
    body: OperationIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "memory.operations.get"
    )
    result = get_client().get_operation(bank_id, body.operation_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/cancel", response_model=MemoryResponse)
def cancel_operation(
    body: OperationIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    """Cancels a pending operation.

    Maps to DELETE .../operations/{id}. Hindsight also offers
    .../operations/{id}/delete (remove a terminal operation) and .../retry;
    v1 exposes neither (SPEC §11.5).
    """
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "memory.operations.cancel", is_write=True
    )
    result = get_client().cancel_operation(bank_id, body.operation_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
