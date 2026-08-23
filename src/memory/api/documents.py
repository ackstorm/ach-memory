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

router = APIRouter(prefix="/v1/memory/documents", tags=["documents"])


class ListDocumentsRequest(ScopedRequest):
    q: str | None = None
    # Bounded on both sides so an out-of-range value is a typed 422 at the
    # boundary, not a 502 blaming the backend for the caller's typo.
    # Defaulted to concrete values (100/0), NOT left unset like
    # ListMemoriesRequest in memory/api/curation.py -- this route always
    # sends both params to Hindsight rather than omitting them for
    # Hindsight's own defaults to apply. High side capped at MAX_PAGE_SIZE
    # (see memory/api/memory.py); low side is ge=1, not ge=0 -- a zero-size
    # page is meaningless and was forwarded upstream verbatim.
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)


class DocumentIdRequest(ScopedRequest):
    # Caller-managed and arbitrary inside the bank (SPEC §11.4), e.g.
    # "github:acme/api:pr:382" -- unlike memory_id/operation_id this is NEVER
    # validated as a UUID; it must reach Hindsight verbatim.
    document_id: str


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

    create=False: these are lookups/maintenance over an existing bank, not
    first-touch creation (SPEC §11.3) -- a document route on an unknown slug
    must 404, not squat the slug for whoever asked first.

    `is_write` forwards to `_resolve_bank`'s rate-limit gate (SPEC §20) --
    only `delete` passes it.
    """
    bank_id, resolved_from, project_slug = _resolve_bank(
        body, db, principal, on_behalf_of, action, create=False, is_write=is_write
    )
    db.commit()
    return bank_id, resolved_from, project_slug


@router.post("/list", response_model=MemoryResponse)
def list_documents(
    body: ListDocumentsRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "memory.documents.list"
    )
    result = get_client().list_documents(
        bank_id, q=body.q, limit=body.limit, offset=body.offset
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/get", response_model=MemoryResponse)
def get_document(
    body: DocumentIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "memory.documents.get"
    )
    result = get_client().get_document(bank_id, body.document_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/delete", response_model=MemoryResponse)
def delete_document(
    body: DocumentIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    """Destructive and irreversible (SPEC §12.2).

    Removes the document and every memory derived from it. Deliberately
    available to any caller authorized for the bank: a document belongs to the
    shared bank namespace, not to whoever created it, and the blast radius is
    one document inside one already-authorized bank.
    """
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "memory.documents.delete", is_write=True
    )
    result = get_client().delete_document(bank_id, body.document_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
