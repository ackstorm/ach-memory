"""SPEC §7: governed mental-model lifecycle, addressed by ACH `model_key`.

Public paths keep `/v1/mental-models/{model_key}` but every route resolves
through `mental_model_service` -- never a raw upstream mental-model id, and
never `bank_id`. `mental_model_service.MentalModelView`/`ModelListResult`
already close their own field set (`extra="forbid"`), so responses are
returned flat, with no `MemoryResponse`/`resolved_from` envelope: unlike the
old REST-only passthrough this file used to be, there is no arbitrary
upstream JSON left to redact or forward slugs for.
"""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import mental_model_service
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.memory import (
    ScopedRequest,
    UUID4Str,
    _check_content_size,
    _resolve_bank,
    scoped_query_params,
)
from memory.auth.principal import Principal
from memory.db import get_session
from memory.hindsight.client import get_client
from memory.mental_model_service import (
    REQUIRED_SOURCE_TAGS,
    CustomModelCreateRequest,
    CustomModelUpdateRequest,
    MentalModelView,
    ModelListResult,
)
from memory.models import Project
from memory.retained_records import LogicalBankRef

router = APIRouter(prefix="/v1/mental-models", tags=["mental-models"])


class MentalModelTrigger(BaseModel):
    # Same validated-subset pass-through the pre-governance REST surface
    # used: `mode` is bound to Hindsight's own enum so an unknown value is a
    # typed 422 here instead of a 502 blaming the backend; every other field
    # is forwarded verbatim.
    model_config = ConfigDict(extra="allow")

    mode: Literal["full", "delta"] | None = None


class CreateMentalModelRequest(ScopedRequest):
    name: str = Field(max_length=256)
    source_query: str
    source_tags: tuple[str, ...]
    tags_match: Literal["all"]
    max_tokens: int = Field(ge=256, le=8192)
    always_in_context: bool
    trigger: MentalModelTrigger
    operation_id: UUID4Str

    @field_validator("source_tags")
    @classmethod
    def _validate_source_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if frozenset(value) != REQUIRED_SOURCE_TAGS:
            raise ValueError(
                "source_tags must select exactly schema:ach-retain-v1 and validity:indefinite"
            )
        return value


class UpdateMentalModelRequest(ScopedRequest):
    name: str | None = Field(default=None, max_length=256)
    source_query: str | None = None
    max_tokens: int | None = Field(default=None, ge=256, le=8192)
    trigger: MentalModelTrigger | None = None
    always_in_context: bool | None = None
    operation_id: UUID4Str


class MutationScopedRequest(ScopedRequest):
    """`ScopedRequest` plus the operation id every mutation-by-verb (DELETE,
    the refresh POST) carries as a query param -- the JSON-body mutations
    (create, update) get theirs from `CreateMentalModelRequest`/
    `UpdateMentalModelRequest` above instead."""

    operation_id: UUID4Str


def mutation_query_params(
    scope: Literal["user", "project"],
    operation_id: str,
    user_id: Annotated[str | None, Query(pattern=r"^[^\x00-\x1f\x7f]*$")] = None,
    project_slug: str | None = None,
    git_locator: Annotated[
        str | None, Query(max_length=512, pattern=r"^[^\x00-\x1f\x7f]*$")
    ] = None,
) -> MutationScopedRequest:
    return MutationScopedRequest(
        scope=scope,
        operation_id=operation_id,
        user_id=user_id,
        project_slug=project_slug,
        git_locator=git_locator,
    )


def resolve_logical_bank(
    body: ScopedRequest,
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
    *,
    is_write: bool,
) -> LogicalBankRef:
    """`_resolve_bank`'s authorization/audit/rate-limit path, re-shaped into
    the `LogicalBankRef` the governed model service addresses instead of a
    bare `bank_id`.

    A mental-model route is maintenance over an existing bank (SPEC §7),
    never first-touch project creation -- every route here resolves with
    `create=False`, matching the pre-governance surface's own rule.
    """
    bank_id, _resolved_from, _project_slug = _resolve_bank(
        body, db, principal, on_behalf_of, action, create=False, is_write=is_write
    )
    if body.scope == "user":
        target_id = body.user_id if (principal.is_master and body.user_id) else principal.user_id
        return LogicalBankRef(principal.tenant_id, "user", target_id, None, bank_id)

    project_internal_id = db.scalar(
        select(Project.internal_id).where(
            Project.tenant_id == principal.tenant_id, Project.bank_id == bank_id
        )
    )
    return LogicalBankRef(principal.tenant_id, "project", None, project_internal_id, bank_id)


@router.post("", response_model=MentalModelView, status_code=201)
def create_mental_model(
    body: CreateMentalModelRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MentalModelView:
    _check_content_size(body.source_query)
    # MentalModelTrigger is extra="allow" (pass-through), so an oversize
    # caller-authored key is otherwise the last uncapped blob on this route.
    _check_content_size(json.dumps(body.trigger.model_dump(exclude_none=True)))
    bank = resolve_logical_bank(
        body, db, principal, on_behalf_of, "mental_models.create", is_write=True
    )
    db.commit()
    request = CustomModelCreateRequest(
        name=body.name,
        source_query=body.source_query,
        source_tags=body.source_tags,
        tags_match=body.tags_match,
        max_tokens=body.max_tokens,
        trigger=body.trigger.model_dump(exclude_none=True),
        always_in_context=body.always_in_context,
        operation_id=body.operation_id,
    )
    return mental_model_service.create_custom_model(db, bank, request, client=get_client())


@router.get("", response_model=ModelListResult)
def list_mental_models(
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> ModelListResult:
    bank = resolve_logical_bank(
        scoped, db, principal, on_behalf_of, "mental_models.list", is_write=False
    )
    db.commit()
    return mental_model_service.list_models(db, bank, client=get_client())


@router.get("/{model_key}", response_model=MentalModelView)
def get_mental_model(
    model_key: str,
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MentalModelView:
    bank = resolve_logical_bank(
        scoped, db, principal, on_behalf_of, "mental_models.get", is_write=False
    )
    db.commit()
    view = mental_model_service.get_model(db, bank, model_key)
    if view.delivery_state == "withheld":
        # Exact refresh-completion check (SPEC §6.4), never a new mutation:
        # this only observes the already-recorded operation, so it stays
        # inside "ordinary get never provisions or reconciles a definition".
        view = mental_model_service.observe_model_refresh(db, bank, model_key, client=get_client())
    return view


@router.patch("/{model_key}", response_model=MentalModelView)
def update_mental_model(
    model_key: str,
    body: UpdateMentalModelRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MentalModelView:
    if body.source_query is not None:
        _check_content_size(body.source_query)
    if body.trigger is not None:
        _check_content_size(json.dumps(body.trigger.model_dump(exclude_none=True)))
    bank = resolve_logical_bank(
        body, db, principal, on_behalf_of, "mental_models.update", is_write=True
    )
    db.commit()
    request = CustomModelUpdateRequest(
        name=body.name,
        source_query=body.source_query,
        max_tokens=body.max_tokens,
        trigger=body.trigger.model_dump(exclude_none=True) if body.trigger else None,
        always_in_context=body.always_in_context,
        operation_id=body.operation_id,
    )
    return mental_model_service.update_model(db, bank, model_key, request, client=get_client())


@router.delete("/{model_key}", status_code=204)
def delete_mental_model(
    model_key: str,
    scoped: Annotated[MutationScopedRequest, Depends(mutation_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> Response:
    bank = resolve_logical_bank(
        scoped, db, principal, on_behalf_of, "mental_models.delete", is_write=True
    )
    db.commit()
    mental_model_service.delete_model(
        db, bank, model_key, operation_id=scoped.operation_id, client=get_client()
    )
    return Response(status_code=204)


@router.post("/{model_key}/refresh", response_model=MentalModelView)
def refresh_mental_model(
    model_key: str,
    scoped: Annotated[MutationScopedRequest, Depends(mutation_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MentalModelView:
    bank = resolve_logical_bank(
        scoped, db, principal, on_behalf_of, "mental_models.refresh", is_write=True
    )
    db.commit()
    return mental_model_service.refresh_model(
        db, bank, model_key, operation_id=scoped.operation_id, client=get_client()
    )
