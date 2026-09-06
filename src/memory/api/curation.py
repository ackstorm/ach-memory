from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from memory import curation_service, read_context, read_service
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.memory import (
    MAX_PAGE_SIZE,
    MemoryResponse,
    ScopedRequest,
    UUID4Str,
    _check_content_size,
    _strip_bank_id,
    resolve_bank_and_commit,
)
from memory.auth.principal import Principal
from memory.db import get_session
from memory.hindsight.client import get_client
from memory.retained_records import get_by_source_memory_id
from memory.retention import resolve_bank_ref
from memory.sanitization import normalize_claim

router = APIRouter(prefix="/v1/memory", tags=["curation"])


class ListMemoriesRequest(ScopedRequest):
    q: str | None = None
    # Bound to Hindsight's own type enum, same reasoning as `state` below --
    # a bogus value forwarded the caller's typo upstream instead of a
    # boundary 422 (review finding 4, 2026-08-23). mcp/tools.py already
    # has this same Literal as FactType, but mcp/tools.py imports FROM this
    # module (ListMemoriesRequest), so importing it back here would be
    # circular; restated rather than sharing.
    type: Literal["world", "experience", "observation"] | None = None
    # Bound to Hindsight's own enum (measured against a live server: any
    # other value 400s with "Invalid state '...': expected 'valid' or
    # 'invalidated'.") so a bogus value is a typed 422 at the boundary
    # instead of a 502 blaming the backend for the caller's typo.
    state: Literal["valid", "invalidated"] | None = None
    document_id: str | None = None
    # Unset by default (not 0/100) so _present omits them and Hindsight's own
    # defaults apply, rather than the wrapper silently overriding them on
    # every request that doesn't ask for paging.
    # Bounded on both sides so an out-of-range value is a typed 422 at the
    # boundary, not a 502 blaming the backend for the caller's typo -- the
    # same reasoning as git_locator's bound and operation_id's validator on
    # ScopedRequest/RetainRequest. High side capped at MAX_PAGE_SIZE (see
    # memory/api/memory.py); low side is ge=1, not ge=0 -- a zero-size page
    # is meaningless and was forwarded upstream verbatim.
    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    offset: int | None = Field(default=None, ge=0)


class MemoryIdRequest(ScopedRequest):
    memory_id: str


class ForgetRequest(MemoryIdRequest):
    reason: str | None = None


class CorrectRequest(MemoryIdRequest):
    # min_length=1 plus the strip check below: Hindsight rejects blank text
    # BEFORE it looks the record up, so a blank correct on a valid memory came
    # back as 409 MEMORY_NOT_CURATABLE -- telling an agent the fact is a
    # derived observation when it simply sent nothing (review finding I5).
    content: str = Field(min_length=1)
    operation_id: UUID4Str

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


def _read_bank(
    body: ScopedRequest,
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
) -> tuple[str, str | None, str | None]:
    """Resolve list/get through the non-enriching read boundary.

    The legacy request still accepts ``git_locator`` for wire compatibility,
    but it is intentionally not forwarded: listing or fetching a memory must
    never bind repository metadata onto an existing project.
    """
    resolved = read_context.resolve_read_bank(
        db,
        principal,
        on_behalf_of,
        action,
        body.scope,
        user_id=body.user_id,
        project_slug=body.project_slug,
    )
    db.commit()
    return resolved.bank_id, resolved.resolved_from, resolved.current_slug


@router.post("/list", response_model=MemoryResponse)
def list_memories(
    body: ListMemoriesRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    # q is a caller-authored search query, same embedding-spend risk class as
    # recall's query; optional, so guarded like the UPDATE routes.
    if body.q is not None:
        _check_content_size(body.q)
    bank_id, resolved_from, project_slug = _read_bank(
        body, db, principal, on_behalf_of, "memory.list"
    )
    read_service.ensure_current_read_allowed(db, resolve_bank_ref(db, principal, body))
    result = get_client().list_memories(
        bank_id,
        q=body.q,
        type=body.type,
        state=body.state,
        document_id=body.document_id,
        limit=body.limit,
        offset=body.offset,
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/get", response_model=MemoryResponse)
def get_memory(
    body: MemoryIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _read_bank(
        body, db, principal, on_behalf_of, "memory.get"
    )
    read_service.ensure_current_read_allowed(db, resolve_bank_ref(db, principal, body))
    result = get_client().get_memory(bank_id, body.memory_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


def _tracked_record(db: Session, principal: Principal, body: MemoryIdRequest):
    """Resolve the stable `RetainedRecord` for this bank+memory_id, if ACH
    ever retained it, and gate on the currentness barrier either way (SPEC
    §5.8: an unresolved prior mutation withholds the whole bank, not just
    reads of it). Returns None to signal a fall back to a direct, untracked
    Hindsight call -- a memory Hindsight derived on its own, or one that
    predates typed retain, has no ACH ledger row to make outcome-safe."""
    bank_ref = resolve_bank_ref(db, principal, body)
    read_service.ensure_current_read_allowed(db, bank_ref)
    return get_by_source_memory_id(db, bank_ref, body.memory_id)


@router.post("/forget", response_model=MemoryResponse)
def forget(
    body: ForgetRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    """Soft retirement, not deletion (SPEC §12.1).

    An agent that invalidates a fact must not be able to destroy the evidence,
    and a wrong invalidation is recoverable with /restore.
    """
    # reason is caller free text forwarded verbatim to Hindsight; optional, so
    # guarded like the UPDATE routes' `if x is not None`.
    if body.reason is not None:
        _check_content_size(body.reason)
    bank_id, resolved_from, project_slug = resolve_bank_and_commit(
        body, db, principal, on_behalf_of, "memory.forget", is_write=True
    )
    retained = _tracked_record(db, principal, body)
    if retained is not None:
        curation_service.forget_record(
            db, retained, reason=body.reason, client=get_client(), bank_id=bank_id
        )
        result: dict = {"id": body.memory_id, "state": "invalidated"}
    else:
        result = get_client().curate(
            bank_id, body.memory_id, state="invalidated", reason=body.reason
        )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/restore", response_model=MemoryResponse)
def restore(
    body: MemoryIdRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = resolve_bank_and_commit(
        body, db, principal, on_behalf_of, "memory.restore", is_write=True
    )
    retained = _tracked_record(db, principal, body)
    if retained is not None:
        curation_service.restore_record(db, retained, client=get_client(), bank_id=bank_id)
        result: dict = {"id": body.memory_id, "state": "valid"}
    else:
        result = get_client().curate(bank_id, body.memory_id, state="valid")
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/correct", response_model=MemoryResponse)
def correct(
    body: CorrectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    # Authorize first, always (this module's own rule -- see `_bank`'s
    # docstring): the bank is resolved BEFORE content is canonicalized, so an
    # unauthorized caller cannot use the sanitizer's secret/size rejection as
    # a free oracle. `correct_record` canonicalizes internally for the
    # tracked path; the untracked legacy fallback below canonicalizes here,
    # since it calls Hindsight directly with no `curation_service` in between.
    # This replaces `_check_content_size` as correct's security contract --
    # that check remains right for document/query transports, but a canonical
    # claim's boundary is `normalize_claim`'s 4096-byte/secret-rejection rule
    # (SPEC's canonical-claim invariant), not the much larger transport cap.
    bank_id, resolved_from, project_slug = resolve_bank_and_commit(
        body, db, principal, on_behalf_of, "memory.correct", is_write=True
    )
    retained = _tracked_record(db, principal, body)
    if retained is not None:
        curation_service.correct_record(
            db,
            retained,
            body.content,
            operation_id=body.operation_id,
            client=get_client(),
            bank_id=bank_id,
        )
        # Echoes the canonical text `correct_record` actually stored, never
        # the caller's raw input (SPEC: a public response includes text only
        # in its canonical form).
        result: dict = {"id": body.memory_id, "text": retained.canonical_content}
    else:
        canonical_content = normalize_claim(body.content)
        result = get_client().curate(bank_id, body.memory_id, text=canonical_content)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
