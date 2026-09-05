"""Transactional custom mental-model lifecycle: quota, budget, Hindsight
orchestration and crash-recovery resume (SPEC §7).

REST (Task 3) and MCP (Task 4) both build one of the request models below
from their own wire-level request and call straight through -- this module
owns the one governed lifecycle, so neither surface reimplements quota,
budget or the create/resume contract.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory import model_registry
from memory.builtin_models import BuiltinModelDefinition
from memory.errors import (
    BuiltinModelImmutable,
    ContextBudgetExceeded,
    CurationNeedsOperator,
    IdempotencyConflict,
    MentalModelNotFound,
)
from memory.ids import new_model_key
from memory.models import MentalModelRegistration
from memory.retained_records import LogicalBankRef

REQUIRED_SOURCE_TAGS = frozenset({"schema:ach-retain-v1", "validity:indefinite"})
MIN_MAX_TOKENS = 256
USER_ALWAYS_IN_CONTEXT_BUDGET = 1024
PROJECT_ALWAYS_IN_CONTEXT_BUDGET = 2048


def _exact_required_tags(value: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if frozenset(value) != REQUIRED_SOURCE_TAGS:
        raise ValueError(
            "source_tags must select exactly schema:ach-retain-v1 and validity:indefinite"
        )
    return value


class CustomModelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    source_query: str
    source_tags: tuple[str, ...]
    tags_match: Literal["all"]
    max_tokens: int = Field(ge=MIN_MAX_TOKENS)
    trigger: dict[str, object]
    always_in_context: bool
    operation_id: str

    @field_validator("source_tags")
    @classmethod
    def _validate_source_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _exact_required_tags(value)  # type: ignore[return-value]


class CustomModelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    source_query: str | None = None
    max_tokens: int | None = Field(default=None, ge=MIN_MAX_TOKENS)
    trigger: dict[str, object] | None = None
    always_in_context: bool | None = None
    operation_id: str


class MentalModelView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_key: str
    name: str
    scope: Literal["user", "project"]
    origin: Literal["builtin", "user"]
    # Only meaningful for origin="builtin" (SPEC §7.2); always None for a
    # custom model.
    definition_version: int | None = None
    source_query: str
    source_tags: tuple[str, ...]
    tags_match: Literal["all"]
    max_tokens: int
    trigger: dict[str, object]
    always_in_context: bool
    delivery_state: Literal["ready", "withheld"]
    refresh_status: Literal["pending", "failed", "succeeded"] | None
    last_refreshed_at: datetime | None


class ModelListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: list[MentalModelView]
    unknown_upstream_count: int


def _upstream_name(model_key: str) -> str:
    return f"ach:{model_key}"


def _budget_for(scope: str) -> int:
    return (
        USER_ALWAYS_IN_CONTEXT_BUDGET if scope == "user" else PROJECT_ALWAYS_IN_CONTEXT_BUDGET
    )


def _check_budget(
    bank: LogicalBankRef,
    live_rows: list[MentalModelRegistration],
    *,
    added_tokens: int,
    excluding_model_key: str | None = None,
) -> None:
    total = added_tokens + sum(
        row.max_tokens
        for row in live_rows
        if row.always_in_context
        and row.lifecycle_state != "deleted"
        and row.model_key != excluding_model_key
    )
    limit = _budget_for(bank.scope)
    if total > limit:
        raise ContextBudgetExceeded(
            f"enabling always_in_context would exceed the {bank.scope} delivery budget",
            limit=limit,
            total=total,
        )


def _payload_hash(bank: LogicalBankRef, request: CustomModelCreateRequest) -> str:
    payload = {
        "schema": "ach-model-idempotency-v1",
        "scope": {
            "tenant_id": bank.tenant_id,
            "scope": bank.scope,
            "user_id": bank.user_id,
            "project_internal_id": bank.project_internal_id,
        },
        "name": request.name,
        "source_query": request.source_query,
        "source_tags": sorted(request.source_tags),
        "tags_match": request.tags_match,
        "max_tokens": request.max_tokens,
        "trigger": request.trigger,
        "always_in_context": request.always_in_context,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _touch(db: Session, row: MentalModelRegistration) -> None:
    row.updated_at = db.execute(select(func.now())).scalar_one()


def _to_view(row: MentalModelRegistration) -> MentalModelView:
    return MentalModelView(
        model_key=row.model_key,
        name=row.name,
        scope=row.scope,
        origin=row.origin,
        definition_version=row.definition_version,
        source_query=row.source_query,
        source_tags=tuple(row.source_tags),
        tags_match=row.tags_match,
        max_tokens=row.max_tokens,
        trigger=row.trigger,
        always_in_context=row.always_in_context,
        delivery_state=row.delivery_state,
        refresh_status=row.refresh_status,
        last_refreshed_at=row.last_refreshed_at,
    )


def _upstream_items(listed: dict) -> list[dict]:
    return listed.get("mental_models") or listed.get("items") or []


def _matches_recorded(upstream: dict, row: MentalModelRegistration) -> bool:
    if upstream.get("source_query") != row.source_query:
        return False
    if upstream.get("max_tokens") != row.max_tokens:
        return False
    if upstream.get("trigger") != row.trigger:
        return False
    tags = upstream.get("tags")
    return tags is None or frozenset(tags) == frozenset(row.source_tags)


def create_custom_model(
    db: Session, bank: LogicalBankRef, request: CustomModelCreateRequest, *, client
) -> MentalModelView:
    live = model_registry.locked_bank_models(db, bank)

    duplicate = next(
        (row for row in live if row.mutation_operation_id == request.operation_id), None
    )
    if duplicate is not None:
        digest = _payload_hash(bank, request)
        if duplicate.mutation_payload_hash != digest:
            raise IdempotencyConflict(
                "operation_id was already used with a different canonical payload",
                operation_id=request.operation_id,
            )
        return _to_view(duplicate)

    if request.always_in_context:
        _check_budget(bank, live, added_tokens=request.max_tokens)

    model_key = new_model_key()
    digest = _payload_hash(bank, request)
    model_registry.register_model(
        db,
        bank,
        origin="user",
        model_key=model_key,
        name=request.name,
        source_query=request.source_query,
        source_tags=list(request.source_tags),
        tags_match=request.tags_match,
        max_tokens=request.max_tokens,
        trigger=request.trigger,
        lifecycle_state="creating",
        mutation_operation_id=request.operation_id,
        mutation_payload_hash=digest,
        always_in_context=request.always_in_context,
        delivery_state="ready",
        existing=live,
    )
    db.commit()

    upstream = client.create_mental_model(
        bank.bank_id,
        name=_upstream_name(model_key),
        source_query=request.source_query,
        max_tokens=request.max_tokens,
        trigger=request.trigger,
        tags=list(request.source_tags),
    )
    activated = model_registry.activate_model(db, bank, model_key, upstream["id"])
    db.commit()
    return _to_view(activated)


def resume_model_mutation(
    db: Session, bank: LogicalBankRef, operation_id: str, *, client
) -> MentalModelView:
    """Crash recovery for a `create_custom_model` whose Hindsight response
    was lost after the `creating` row was already committed."""
    row = model_registry.find_by_operation(db, bank, operation_id)
    if row is None or row.lifecycle_state == "deleted":
        raise MentalModelNotFound("no pending mutation with that operation id")
    if row.lifecycle_state != "creating":
        return _to_view(row)

    listed = client.list_mental_models(bank.bank_id, detail="full")
    upstream_name = _upstream_name(row.model_key)
    candidates = [item for item in _upstream_items(listed) if item.get("name") == upstream_name]
    exact = [item for item in candidates if _matches_recorded(item, row)]

    if len(exact) == 1:
        activated = model_registry.activate_model(db, bank, row.model_key, exact[0]["id"])
        db.commit()
        return _to_view(activated)

    if not candidates:
        upstream = client.create_mental_model(
            bank.bank_id,
            name=upstream_name,
            source_query=row.source_query,
            max_tokens=row.max_tokens,
            trigger=row.trigger,
            tags=list(row.source_tags),
        )
        activated = model_registry.activate_model(db, bank, row.model_key, upstream["id"])
        db.commit()
        return _to_view(activated)

    raise CurationNeedsOperator(
        "multiple or mismatched upstream models share this model's internal name"
    )


def list_models(db: Session, bank: LogicalBankRef, *, client) -> ModelListResult:
    rows = [
        row
        for row in model_registry.list_registered_models(db, bank)
        if row.lifecycle_state != "deleted"
    ]
    known_upstream_ids = {row.upstream_model_id for row in rows if row.upstream_model_id}
    listed = client.list_mental_models(bank.bank_id, detail="full")
    unknown_count = sum(
        1 for item in _upstream_items(listed) if item.get("id") not in known_upstream_ids
    )
    return ModelListResult(models=[_to_view(row) for row in rows], unknown_upstream_count=unknown_count)


def get_model(db: Session, bank: LogicalBankRef, model_key: str) -> MentalModelView:
    row = model_registry.get_registered_model(db, bank, model_key)
    if row is None or row.lifecycle_state == "deleted":
        raise MentalModelNotFound("no registered model with that logical key")
    return _to_view(row)


def update_model(
    db: Session,
    bank: LogicalBankRef,
    model_key: str,
    request: CustomModelUpdateRequest,
    *,
    client,
) -> MentalModelView:
    live = model_registry.locked_bank_models(db, bank)
    row = next(
        (r for r in live if r.model_key == model_key and r.lifecycle_state != "deleted"), None
    )
    if row is None:
        raise MentalModelNotFound("no registered model with that logical key")
    if row.origin == "builtin":
        raise BuiltinModelImmutable(
            "a built-in model's definition cannot be changed through custom-model CRUD"
        )

    new_always = row.always_in_context if request.always_in_context is None else request.always_in_context
    new_tokens = row.max_tokens if request.max_tokens is None else request.max_tokens
    if new_always:
        _check_budget(bank, live, added_tokens=new_tokens, excluding_model_key=model_key)

    upstream_changes: dict[str, object] = {}
    if request.source_query is not None and request.source_query != row.source_query:
        upstream_changes["source_query"] = request.source_query
    if request.max_tokens is not None and request.max_tokens != row.max_tokens:
        upstream_changes["max_tokens"] = request.max_tokens
    if request.trigger is not None and request.trigger != row.trigger:
        upstream_changes["trigger"] = request.trigger
    if upstream_changes and row.upstream_model_id is not None:
        client.update_mental_model(bank.bank_id, row.upstream_model_id, **upstream_changes)

    if request.name is not None:
        row.name = request.name
    if request.source_query is not None:
        row.source_query = request.source_query
    if request.max_tokens is not None:
        row.max_tokens = request.max_tokens
    if request.trigger is not None:
        row.trigger = request.trigger
    if request.always_in_context is not None:
        row.always_in_context = request.always_in_context
    _touch(db, row)
    db.flush()
    db.commit()
    return _to_view(row)


def delete_model(db: Session, bank: LogicalBankRef, model_key: str, *, client) -> None:
    row = model_registry.get_registered_model(db, bank, model_key)
    if row is None:
        raise MentalModelNotFound("no registered model with that logical key")
    if row.origin == "builtin":
        raise BuiltinModelImmutable("a built-in model cannot be deleted through custom-model CRUD")
    if row.lifecycle_state == "deleted":
        return

    if row.upstream_model_id is not None:
        try:
            client.delete_mental_model(bank.bank_id, row.upstream_model_id)
        except MentalModelNotFound:
            pass  # an absent upstream source already satisfies deletion

    model_registry.mark_deleted(db, bank, model_key)
    db.commit()


def refresh_model(db: Session, bank: LogicalBankRef, model_key: str, *, client) -> MentalModelView:
    row = model_registry.get_registered_model(db, bank, model_key)
    if row is None or row.lifecycle_state == "deleted" or row.upstream_model_id is None:
        raise MentalModelNotFound("no registered model with that logical key")

    result = client.refresh_mental_model(bank.bank_id, row.upstream_model_id)
    operation_id = result.get("operation_id") or result.get("id")
    withheld = model_registry.withhold_model(db, bank, model_key, operation_id)
    db.commit()
    return _to_view(withheld)


def reconcile_builtin(
    db: Session, bank: LogicalBankRef, definition: BuiltinModelDefinition, *, client
) -> MentalModelView:
    """Ensure `bank` has `definition` registered at its current version (SPEC
    §7.4/§7.5): create it if missing, upgrade only the prompt/source
    definition when the compiled version increased (preserving the user's
    `always_in_context` choice), and leave an explicitly disabled built-in
    (`lifecycle_state="disabled"`) or an already-current one untouched.
    """
    existing = model_registry.get_registered_model(db, bank, definition.key)
    if existing is not None and existing.lifecycle_state == "disabled":
        return _to_view(existing)
    if existing is None:
        return _create_builtin(db, bank, definition, client=client)
    if existing.definition_version < definition.version:
        return _upgrade_builtin(db, bank, existing, definition, client=client)
    return _to_view(existing)


def _create_builtin(
    db: Session, bank: LogicalBankRef, definition: BuiltinModelDefinition, *, client
) -> MentalModelView:
    upstream_name = _upstream_name(definition.key)
    listed = client.list_mental_models(bank.bank_id, detail="full")
    if any(item.get("name") == upstream_name for item in _upstream_items(listed)):
        # An unrecognized upstream model already occupies this built-in's
        # internal locator (SPEC §7.3/§12.3): never silently adopt or mutate
        # it, and never create a second, colliding model under the same name.
        raise CurationNeedsOperator(
            "an unrecognized upstream model already uses this built-in's internal name"
        )

    live = model_registry.locked_bank_models(db, bank)
    model_registry.register_model(
        db,
        bank,
        origin="builtin",
        model_key=definition.key,
        name=definition.name,
        source_query=definition.source_query,
        source_tags=list(definition.source_tags),
        tags_match=definition.tags_match,
        max_tokens=definition.max_tokens,
        trigger=dict(definition.trigger),
        builtin_key=definition.key,
        definition_version=definition.version,
        lifecycle_state="creating",
        always_in_context=definition.always_in_context,
        delivery_state="ready",
        existing=live,
    )
    db.commit()

    upstream = client.create_mental_model(
        bank.bank_id,
        name=upstream_name,
        source_query=definition.source_query,
        max_tokens=definition.max_tokens,
        trigger=dict(definition.trigger),
        tags=list(definition.source_tags),
    )
    activated = model_registry.activate_model(db, bank, definition.key, upstream["id"])
    db.commit()
    return _to_view(activated)


def _upgrade_builtin(
    db: Session,
    bank: LogicalBankRef,
    existing: MentalModelRegistration,
    definition: BuiltinModelDefinition,
    *,
    client,
) -> MentalModelView:
    upstream_changes: dict[str, object] = {}
    if definition.source_query != existing.source_query:
        upstream_changes["source_query"] = definition.source_query
    if definition.max_tokens != existing.max_tokens:
        upstream_changes["max_tokens"] = definition.max_tokens
    if dict(definition.trigger) != existing.trigger:
        upstream_changes["trigger"] = dict(definition.trigger)
    if upstream_changes and existing.upstream_model_id is not None:
        client.update_mental_model(bank.bank_id, existing.upstream_model_id, **upstream_changes)

    existing.source_query = definition.source_query
    existing.max_tokens = definition.max_tokens
    existing.trigger = dict(definition.trigger)
    existing.definition_version = definition.version
    # always_in_context is deliberately untouched: an upgrade never re-enables
    # a user's disabled delivery choice (SPEC §7.4).
    _touch(db, existing)
    db.flush()

    if existing.upstream_model_id is not None:
        result = client.refresh_mental_model(bank.bank_id, existing.upstream_model_id)
        operation_id = result.get("operation_id") or result.get("id")
        withheld = model_registry.withhold_model(db, bank, definition.key, operation_id)
    else:
        withheld = existing
    db.commit()
    return _to_view(withheld)
