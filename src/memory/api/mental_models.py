"""SPEC §14: mental models are REST API-only, never an MCP tool.

A mental model is persisted, synthesized project knowledge Hindsight builds
from a source query and feeds back into reflection -- it can become
high-priority shared context for every user and agent on a project. Same
reasoning as directives.py: writing or refreshing one is governance for the
whole project, not a private note, so it stays off the LLM-facing MCP surface
(§14.2, §14.4) even though the underlying knowledge is exactly the kind of
thing an agent would otherwise want to curate for itself. Authorization is
the same §7 bank rule as everywhere else in this service -- this file adds no
new permission model, only a narrower surface.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
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

router = APIRouter(prefix="/v1/mental-models", tags=["mental-models"])


class MentalModelTrigger(BaseModel):
    # Pass-through by design (SPEC §14.5) EXCEPT `mode`, which upstream types
    # as Literal["full","delta"]: an unknown value was a 422 upstream and a
    # 502 here. Everything else stays unvalidated on purpose -- Hindsight's
    # own defaults already mean "no automatic refresh".
    model_config = ConfigDict(extra="allow")

    mode: Literal["full", "delta"] | None = None


class CreateMentalModelRequest(ScopedRequest):
    name: str
    source_query: str
    # Mirrors hindsight-api 0.9.1's own Field(ge=256, le=8192). Upstream is
    # FastAPI, so its rejection is a 422 that _request cannot distinguish from
    # a backend fault -- it became a 502 (review finding I6).
    max_tokens: int | None = Field(default=None, ge=256, le=8192)
    # Passed through verbatim, never defaulted or shape-validated (SPEC
    # §14.5): Hindsight's own defaults (`refresh_after_consolidation=false`,
    # `refresh_cron=null`) already mean "no automatic refresh" when this is
    # omitted -- the cheapest and safest behavior. A caller who sets
    # `refresh_after_consolidation: true` is choosing to spend a full
    # `reflect` per refresh (§19.4: unattributable spend), and that choice is
    # never made for them here.
    trigger: MentalModelTrigger | None = None
    # No "tags", no "id": tags is Hindsight's in-bank visibility scope this
    # service does not model (same as directives); id is not exposed because
    # nothing in the brief calls for caller-assigned mental-model ids.


class UpdateMentalModelRequest(ScopedRequest):
    name: str | None = None
    source_query: str | None = None
    max_tokens: int | None = Field(default=None, ge=256, le=8192)
    trigger: MentalModelTrigger | None = None


def _bank(
    body: ScopedRequest,
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
    *,
    is_write: bool = False,
) -> tuple[str, str | None, str | None]:
    """Authorize first, always -- same shape as directives.py.

    create=False: a mental-model route is maintenance over an existing bank
    (SPEC §11.3), never first-touch project creation.
    """
    bank_id, resolved_from, project_slug = _resolve_bank(
        body, db, principal, on_behalf_of, action, create=False, is_write=is_write
    )
    db.commit()
    return bank_id, resolved_from, project_slug


@router.post("", response_model=MemoryResponse, status_code=201)
def create_mental_model(
    body: CreateMentalModelRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    """Create persisted, synthesized project knowledge from a source query.

    Not an MCP tool (SPEC §14.2/§14.4): a mental model feeds reflection for
    everyone on the project, so managing it is governance an agent cannot
    grant itself.
    """
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "mental_models.create", is_write=True
    )
    result = get_client().create_mental_model(
        bank_id,
        name=body.name,
        source_query=body.source_query,
        max_tokens=body.max_tokens,
        trigger=body.trigger.model_dump(exclude_none=True) if body.trigger else None,
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.get("", response_model=MemoryResponse)
def list_mental_models(
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
    detail: str | None = None,
    limit: Annotated[int | None, Query(ge=0)] = None,
    offset: Annotated[int | None, Query(ge=0)] = None,
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "mental_models.list"
    )
    result = get_client().list_mental_models(
        bank_id, detail=detail, limit=limit, offset=offset
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.get("/{mental_model_id}", response_model=MemoryResponse)
def get_mental_model(
    mental_model_id: str,
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "mental_models.get"
    )
    result = get_client().get_mental_model(bank_id, mental_model_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.patch("/{mental_model_id}", response_model=MemoryResponse)
def update_mental_model(
    mental_model_id: str,
    body: UpdateMentalModelRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        body, db, principal, on_behalf_of, "mental_models.update", is_write=True
    )
    result = get_client().update_mental_model(
        bank_id,
        mental_model_id,
        name=body.name,
        source_query=body.source_query,
        max_tokens=body.max_tokens,
        trigger=body.trigger.model_dump(exclude_none=True) if body.trigger else None,
    )
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.delete("/{mental_model_id}", response_model=MemoryResponse)
def delete_mental_model(
    mental_model_id: str,
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "mental_models.delete", is_write=True
    )
    result = get_client().delete_mental_model(bank_id, mental_model_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/{mental_model_id}/refresh", response_model=MemoryResponse)
def refresh_mental_model(
    mental_model_id: str,
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    """Force an immediate refresh.

    Costs a full `reflect` upstream every time (SPEC §14.5, §19.4), same as
    the data-plane `reflect` route -- `is_write=True` here for the same
    reason it is there: the spend, not a Hindsight write, is what needs rate
    limiting. `dry-run-refresh` exists upstream and is deliberately never
    wired on any surface (SPEC §11.7): it costs the same as this route while
    inviting the caller to treat it as free.
    """
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "mental_models.refresh", is_write=True
    )
    result = get_client().refresh_mental_model(bank_id, mental_model_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )


@router.post("/{mental_model_id}/clear", response_model=MemoryResponse)
def clear_mental_model(
    mental_model_id: str,
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    bank_id, resolved_from, project_slug = _bank(
        scoped, db, principal, on_behalf_of, "mental_models.clear", is_write=True
    )
    result = get_client().clear_mental_model(bank_id, mental_model_id)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
