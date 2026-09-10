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
import logging
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory import model_registry
from memory.builtin_models import BuiltinModelDefinition
from memory.currentness import bank_is_withheld
from memory.errors import (
    BuiltinModelImmutable,
    ContextBudgetExceeded,
    CurationNeedsOperator,
    DomainError,
    HindsightError,
    IdempotencyConflict,
    MentalModelNotFound,
)
from memory.ids import new_model_key
from memory.models import MentalModelRegistration
from memory.retained_records import LogicalBankRef
from memory.tags import normalize_caller_tags

logger = logging.getLogger("memory.mental_model_service")

REQUIRED_SOURCE_TAGS = frozenset({"schema:ach-retain-v1", "validity:indefinite"})
MIN_MAX_TOKENS = 256
REPAIR_BACKOFF_SECONDS = 60
# Room for a built-in at its full budget plus a couple of custom models, on
# each side, inside the 5120-token delivery ceiling in `delivery.py`. Raised
# with the built-ins themselves: 512 tokens of user context was already too
# small for a bank with real content, and the whole section was being dropped
# for overrunning it.
USER_ALWAYS_IN_CONTEXT_BUDGET = 2560
PROJECT_ALWAYS_IN_CONTEXT_BUDGET = 2560

# Defaults returned by Hindsight 0.9.2 when a create request omits the
# trigger. ACH stores that manual policy as ``{}``; normalizing both sides
# lets crash recovery recognize the model Hindsight actually created.
_TRIGGER_DEFAULTS: dict[str, object] = {
    "mode": "full",
    "refresh_after_consolidation": False,
    "refresh_cron": None,
    "min_refresh_interval_seconds": None,
    "fact_types": None,
    "exclude_mental_models": False,
    "exclude_mental_model_ids": None,
    "tags_match": None,
    "tag_groups": None,
    "include_chunks": None,
    "recall_max_tokens": None,
    "recall_chunks_max_tokens": None,
    "response_schema": None,
    "keep_trace": False,
}
_MANUAL_TRIGGER_UPDATE: dict[str, object] = {
    "mode": "full",
    "refresh_after_consolidation": False,
    "refresh_cron": None,
}


#: Trigger keys that decide which memories a model is built from. Upstream
#: reads them in preference to the model's own tags -- `tags_match` replaces
#: the match mode, `tag_groups` discards the tag list outright -- so a caller
#: that set either one chose the source selection while the row went on
#: recording `all_strict` over `effective_source_tags`. Everything else in a
#: trigger governs WHEN a refresh happens and is forwarded verbatim by
#: design; these two govern WHAT is summarised, which is server-owned for the
#: same reason `tags.RESERVED_PREFIXES` is.
_SERVER_OWNED_TRIGGER_KEYS = ("tags_match", "tag_groups")


def _validated_trigger(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    mode = value.get("mode")
    if mode is not None and mode not in ("full", "delta"):
        raise ValueError("trigger mode must be 'full' or 'delta'; use {} for manual refresh")
    for key in _SERVER_OWNED_TRIGGER_KEYS:
        if value.get(key) is not None:
            raise ValueError(f"trigger.{key} is server-owned; narrow with source_tags instead")
    return value


def _canonical_trigger(value: dict[str, object]) -> dict[str, object]:
    return {**_TRIGGER_DEFAULTS, **value}


def effective_source_tags(caller_tags: tuple[str, ...]) -> list[str]:
    """The filter a custom model actually gets: the server's required pair,
    AND-ed with whatever the caller narrowed by.

    The pair is not caller input and never was a caller decision.
    `validity:indefinite` keeps a claim with an expiry out of a summary that
    would outlive it (SPEC §5.6/§6.4); `schema:ach-retain-v1` keeps the source
    to ACH's own typed retain. Composing them here rather than making the
    caller repeat them is what removes the mode question entirely: with the
    pair always ANDed in, the only sound match is `all_strict`, so there is
    nothing left to choose. Both prefixes live in `tags.RESERVED_PREFIXES`, so
    `normalize_caller_tags` already refuses a caller that tries to name either
    one, in any case.
    """
    return sorted(REQUIRED_SOURCE_TAGS | set(caller_tags))


class CustomModelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    source_query: str
    #: The caller's OWN narrowing tags (e.g. `repo:group/app`), never the
    #: required pair -- `effective_source_tags` adds that. Empty means the
    #: whole indefinite ACH corpus, which is what a built-in reads.
    source_tags: tuple[str, ...] = ()
    max_tokens: int = Field(ge=MIN_MAX_TOKENS)
    trigger: dict[str, object]
    operation_id: str

    @field_validator("source_tags", mode="before")
    @classmethod
    def _normalize_source_tags(cls, value: object) -> tuple[str, ...]:
        # The same one gate every other surface runs caller tags through
        # (`read_models.RecallRequest.tags_filter`), so a tag written by
        # retain and a tag a model selects on are byte-identical, and a
        # server-owned namespace is refused here exactly as it is there.
        return normalize_caller_tags(value)

    @field_validator("trigger")
    @classmethod
    def _validate_trigger(cls, value: dict[str, object]) -> dict[str, object]:
        return _validated_trigger(value)  # type: ignore[return-value]


class CustomModelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    source_query: str | None = None
    max_tokens: int | None = Field(default=None, ge=MIN_MAX_TOKENS)
    trigger: dict[str, object] | None = None
    operation_id: str

    @field_validator("trigger")
    @classmethod
    def _validate_trigger(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        return _validated_trigger(value)


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
    #: The EFFECTIVE filter -- the server's required pair plus whatever the
    #: caller narrowed by. Always matched strictly and as an AND, so there
    #: is no mode to report alongside it.
    source_tags: tuple[str, ...]
    max_tokens: int
    trigger: dict[str, object]
    delivery_state: Literal["ready", "withheld"]
    refresh_status: Literal["required", "pending", "failed", "succeeded"] | None
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
    definition: BuiltinModelDefinition,
    excluding_model_key: str | None = None,
) -> None:
    # Standing delivery is origin="builtin" and lifecycle_state="active"
    # (context_service.load's own predicate) -- nothing else can ever
    # contribute to this scope's spend any more.
    total = added_tokens + sum(
        row.max_tokens
        for row in live_rows
        if row.origin == "builtin"
        and row.lifecycle_state == "active"
        and row.model_key != excluding_model_key
    )
    limit = _budget_for(bank.scope)
    if total > limit:
        raise ContextBudgetExceeded(
            f"built-in {definition.key!r} would exceed the {bank.scope} delivery budget",
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
        "max_tokens": request.max_tokens,
        "trigger": request.trigger,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _touch(db: Session, row: MentalModelRegistration) -> None:
    row.updated_at = db.execute(select(func.now())).scalar_one()


def _update_payload_hash(
    bank: LogicalBankRef, model_key: str, request: CustomModelUpdateRequest
) -> str:
    payload = {
        "schema": "ach-model-update-idempotency-v1",
        "scope": {
            "tenant_id": bank.tenant_id,
            "scope": bank.scope,
            "user_id": bank.user_id,
            "project_internal_id": bank.project_internal_id,
        },
        "model_key": model_key,
        "name": request.name,
        "source_query": request.source_query,
        "max_tokens": request.max_tokens,
        "trigger": request.trigger,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bare_mutation_payload_hash(bank: LogicalBankRef, model_key: str, action: str) -> str:
    """Refresh and delete carry no caller-authored content -- the payload
    identity is just which model, in which bank, under which action, so a
    reused operation id can never legitimately mean two different things
    for either of these (`IdempotencyConflict` still exists as a boundary,
    but only a caller genuinely reusing an id for a DIFFERENT model or
    action can ever trip it)."""
    payload = {
        "schema": "ach-model-mutation-idempotency-v1",
        "scope": {
            "tenant_id": bank.tenant_id,
            "scope": bank.scope,
            "user_id": bank.user_id,
            "project_internal_id": bank.project_internal_id,
        },
        "model_key": model_key,
        "action": action,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _to_view(row: MentalModelRegistration) -> MentalModelView:
    return MentalModelView(
        model_key=row.model_key,
        name=row.name,
        scope=row.scope,
        origin=row.origin,
        definition_version=row.definition_version,
        source_query=row.source_query,
        source_tags=tuple(row.source_tags),
        max_tokens=row.max_tokens,
        trigger=row.trigger,
        delivery_state=row.delivery_state,
        refresh_status=row.refresh_status,
        last_refreshed_at=row.last_refreshed_at,
    )


def _upstream_items(listed: dict) -> list[dict]:
    return listed.get("mental_models") or listed.get("items") or []


def _created_identity(response: dict) -> tuple[str, str]:
    """Consume Hindsight 0.9.2's CreateMentalModelResponse exactly."""
    model_id = response.get("mental_model_id")
    operation_id = response.get("operation_id")
    if not isinstance(model_id, str) or not model_id:
        raise HindsightError("memory backend did not identify the created mental model")
    if not isinstance(operation_id, str) or not operation_id:
        raise HindsightError("memory backend did not identify the model refresh operation")
    return model_id, operation_id


def _matches_recorded(upstream: dict, row: MentalModelRegistration) -> bool:
    if upstream.get("source_query") != row.source_query:
        return False
    if upstream.get("max_tokens") != row.max_tokens:
        return False
    upstream_trigger = upstream.get("trigger")
    if not isinstance(upstream_trigger, dict):
        return False
    if _canonical_trigger(upstream_trigger) != _canonical_trigger(row.trigger):
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
        if duplicate.lifecycle_state == "creating":
            # A lost create response: the row was durably committed but
            # never activated. Resume rather than echo the stale `creating`
            # state back to a caller retrying the exact same request.
            return resume_model_mutation(db, bank, request.operation_id, client=client)
        return _to_view(duplicate)

    model_key = new_model_key()
    digest = _payload_hash(bank, request)
    # Composed once and used for both halves: the row and the upstream model
    # must select on the same set, or `_matches_recorded` stops recognizing
    # our own model during crash recovery.
    source_tags = effective_source_tags(request.source_tags)
    model_registry.register_model(
        db,
        bank,
        origin="user",
        model_key=model_key,
        name=request.name,
        source_query=request.source_query,
        source_tags=source_tags,
        # Hindsight's own default for a tagged model, stated rather than
        # inferred (`memory_engine._resolve_tags_match`): every tag in the set
        # must be present, and an untagged memory is excluded.
        tags_match="all_strict",
        max_tokens=request.max_tokens,
        trigger=request.trigger,
        lifecycle_state="creating",
        mutation_operation_id=request.operation_id,
        mutation_payload_hash=digest,
        delivery_state="ready",
        existing=live,
    )
    db.commit()

    upstream = client.create_mental_model(
        bank.bank_id,
        name=_upstream_name(model_key),
        source_query=request.source_query,
        max_tokens=request.max_tokens,
        # Hindsight has no literal ``manual`` mode. An empty ACH trigger is
        # the explicit manual policy and must be omitted on create.
        trigger=request.trigger or None,
        tags=source_tags,
    )
    upstream_id, refresh_operation_id = _created_identity(upstream)
    activated = model_registry.activate_model(
        db, bank, model_key, upstream_id, refresh_operation_id
    )
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
    return _to_view(_resume_row(db, bank, row, client=client))


def _resume_row(
    db: Session, bank: LogicalBankRef, row: MentalModelRegistration, *, client
) -> MentalModelRegistration:
    """Finish a `creating` registration whose upstream half is unknown.

    Origin-agnostic on purpose. A built-in and a custom model get stuck the
    same way and for the same good reason -- the row is committed before the
    Hindsight create, so a lost response can never orphan an upstream model
    nobody recorded -- so both are recovered the same way: adopt our own
    model if it is already up there, create it if the name is free, and
    refuse to guess when something else answers to that name.
    """
    listed = client.list_mental_models(bank.bank_id, detail="full")
    upstream_name = _upstream_name(row.model_key)
    candidates = [item for item in _upstream_items(listed) if item.get("name") == upstream_name]
    exact = [item for item in candidates if _matches_recorded(item, row)]

    if len(exact) == 1:
        activated = model_registry.activate_model(db, bank, row.model_key, exact[0]["id"])
        db.commit()
        return activated

    if not candidates:
        upstream = client.create_mental_model(
            bank.bank_id,
            name=upstream_name,
            source_query=row.source_query,
            max_tokens=row.max_tokens,
            trigger=row.trigger or None,
            tags=list(row.source_tags),
        )
        upstream_id, refresh_operation_id = _created_identity(upstream)
        activated = model_registry.activate_model(
            db, bank, row.model_key, upstream_id, refresh_operation_id
        )
        db.commit()
        return activated

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

    digest = _update_payload_hash(bank, model_key, request)
    mutation, created = model_registry.accept_model_mutation(
        db, bank, model_key=model_key, operation_id=request.operation_id,
        action="update", payload_hash=digest,
    )
    if not created and mutation.state == "completed":
        db.commit()
        return _to_view(row)

    upstream_changes: dict[str, object] = {}
    if request.source_query is not None and request.source_query != row.source_query:
        upstream_changes["source_query"] = request.source_query
    if request.max_tokens is not None and request.max_tokens != row.max_tokens:
        upstream_changes["max_tokens"] = request.max_tokens
    if request.trigger is not None and request.trigger != row.trigger:
        # Hindsight PATCHes trigger fields rather than replacing the object.
        # Sending {} would leave an existing schedule active while ACH
        # recorded it as manual, so manual must explicitly clear both
        # automatic refresh mechanisms.
        upstream_changes["trigger"] = (
            request.trigger or dict(_MANUAL_TRIGGER_UPDATE)
        )
    # A display-name-only update changes nothing the synthesis reads from,
    # so it never touches delivery currentness (SPEC §6.4). Any of the three
    # source-affecting fields does: the model
    # is withheld as refresh-required BEFORE the upstream definition update
    # is even sent, so a crash between here and the eventual refresh leaves
    # a repairable withheld model, never a falsely-current one.
    requires_refresh = bool(upstream_changes) and row.upstream_model_id is not None
    if requires_refresh:
        now = db.execute(select(func.now())).scalar_one()
        model_registry.require_model_refresh(db, bank, model_key, repair_not_before=now)
        db.commit()

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
    _touch(db, row)
    db.flush()

    if requires_refresh:
        result = client.refresh_mental_model(bank.bank_id, row.upstream_model_id)
        refresh_operation_id = result.get("operation_id") or result.get("id")
        model_registry.record_model_refresh_operation(db, bank, model_key, refresh_operation_id)

    model_registry.complete_model_mutation(db, mutation)
    db.commit()
    return _to_view(row)


def delete_model(db: Session, bank: LogicalBankRef, model_key: str, *, operation_id: str, client) -> None:
    row = model_registry.get_registered_model(db, bank, model_key)
    if row is None:
        raise MentalModelNotFound("no registered model with that logical key")
    if row.origin == "builtin":
        raise BuiltinModelImmutable("a built-in model cannot be deleted through custom-model CRUD")

    # The ledger is consulted BEFORE the already-deleted short-circuit below,
    # not after: a caller reusing this operation_id for a genuinely
    # different model/action must still get IdempotencyConflict even when
    # the model happens to already be gone, rather than that mismatch being
    # silently swallowed by the no-op return.
    digest = _bare_mutation_payload_hash(bank, model_key, "delete")
    mutation, created = model_registry.accept_model_mutation(
        db, bank, model_key=model_key, operation_id=operation_id,
        action="delete", payload_hash=digest,
    )
    if row.lifecycle_state == "deleted":
        model_registry.complete_model_mutation(db, mutation)
        db.commit()
        return
    if not created and mutation.state == "completed":
        db.commit()
        return

    if row.upstream_model_id is not None:
        try:
            client.delete_mental_model(bank.bank_id, row.upstream_model_id)
        except MentalModelNotFound:
            pass  # an absent upstream source already satisfies deletion

    model_registry.mark_deleted(db, bank, model_key)
    model_registry.complete_model_mutation(db, mutation)
    db.commit()


def refresh_model(db: Session, bank: LogicalBankRef, model_key: str, *, operation_id: str, client) -> MentalModelView:
    row = model_registry.get_registered_model(db, bank, model_key)
    if row is None or row.lifecycle_state == "deleted" or row.upstream_model_id is None:
        raise MentalModelNotFound("no registered model with that logical key")

    digest = _bare_mutation_payload_hash(bank, model_key, "refresh")
    mutation, created = model_registry.accept_model_mutation(
        db, bank, model_key=model_key, operation_id=operation_id,
        action="refresh", payload_hash=digest,
    )
    if not created and mutation.state == "completed":
        db.commit()
        return _to_view(row)

    result = client.refresh_mental_model(bank.bank_id, row.upstream_model_id)
    refresh_operation_id = result.get("operation_id") or result.get("id")
    withheld = model_registry.withhold_model(db, bank, model_key, refresh_operation_id)
    model_registry.complete_model_mutation(db, mutation, upstream_operation_id=refresh_operation_id)
    db.commit()
    return _to_view(withheld)


def reconcile_builtin(
    db: Session, bank: LogicalBankRef, definition: BuiltinModelDefinition, *, client
) -> MentalModelView:
    """Ensure `bank` has `definition` registered at its current version (SPEC
    §7.4/§7.5): create it if missing, upgrade only the prompt/source
    definition when the compiled version increased, and leave an explicitly
    disabled built-in (`lifecycle_state="disabled"`) or an already-current
    one untouched. An upgrade never re-enables an operator-disabled
    built-in -- `lifecycle_state` is the only thing that ever carried that.

    Also repairs a built-in left in `creating` by a create whose Hindsight
    call failed after the row was committed, which is otherwise permanent.
    """
    existing = model_registry.get_registered_model(db, bank, definition.key)
    if existing is None:
        return _create_builtin(db, bank, definition, client=client)
    if existing.lifecycle_state == "disabled":
        return _to_view(existing)
    if existing.lifecycle_state == "creating":
        # A create whose Hindsight half was lost commits the row and never
        # activates it, and nothing else repaired that: the definition
        # version already matches, so every later reconcile returned the
        # stuck row unchanged. Standing delivery requires "active", so the
        # bank served empty standing context for ever -- and since built-ins
        # are the only standing context, that meant none at all.
        #
        # Repaired here rather than at delivery because load_context is
        # read-only and deadline-bound; this runs on the paths that already
        # talk to Hindsight (bootstrap, and creating a project or user).
        # Falls through rather than returning, so a definition bump still
        # applies in the same pass.
        existing = _resume_row(db, bank, existing, client=client)
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
    _check_budget(bank, live, added_tokens=definition.max_tokens, definition=definition)
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
    upstream_id, refresh_operation_id = _created_identity(upstream)
    activated = model_registry.activate_model(
        db, bank, definition.key, upstream_id, refresh_operation_id
    )
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
    live = model_registry.locked_bank_models(db, bank)
    _check_budget(
        bank, live, added_tokens=definition.max_tokens,
        definition=definition, excluding_model_key=definition.key,
    )

    upstream_changes: dict[str, object] = {}
    if definition.source_query != existing.source_query:
        upstream_changes["source_query"] = definition.source_query
    if definition.max_tokens != existing.max_tokens:
        upstream_changes["max_tokens"] = definition.max_tokens
    if dict(definition.trigger) != existing.trigger:
        upstream_changes["trigger"] = dict(definition.trigger)
    # `tags` upstream, `source_tags` on the row. This IS an upstream property
    # -- `_create_builtin` sends it as `tags` -- so a definition that changes
    # which sources feed a built-in has to move both, and has to refresh:
    # without it the row kept the old tag set, claimed the new
    # `definition_version`, left `upstream_changes` empty, and therefore
    # skipped the update AND the refresh. Nothing ever converged.
    if list(definition.source_tags) != list(existing.source_tags or []):
        upstream_changes["tags"] = list(definition.source_tags)
    if upstream_changes and existing.upstream_model_id is not None:
        client.update_mental_model(bank.bank_id, existing.upstream_model_id, **upstream_changes)

    existing.source_query = definition.source_query
    existing.max_tokens = definition.max_tokens
    existing.trigger = dict(definition.trigger)
    existing.source_tags = list(definition.source_tags)
    # Never sent upstream (Hindsight's mental-model tags_match lives in its
    # trigger, which this codebase never sets): a definition_version bump for
    # a mode-only change like all_strict is local bookkeeping, so it's safe
    # to update unconditionally rather than diffing into upstream_changes.
    existing.tags_match = definition.tags_match
    existing.definition_version = definition.version
    # lifecycle_state is deliberately untouched: an upgrade never re-enables
    # an operator-disabled built-in (SPEC §7.4).
    _touch(db, existing)
    db.flush()

    # Refresh only what actually changed upstream. A version bump that moves
    # nothing Hindsight can see -- `tags_match`, which lives in ACH's own row
    # and not in the model we created -- has nothing to re-synthesize, and
    # withholding for it would take standing context (built-ins, and nothing
    # else) off the air on every bank at once, on the first call after a
    # deploy, for a change that alters no output.
    if upstream_changes and existing.upstream_model_id is not None:
        result = client.refresh_mental_model(bank.bank_id, existing.upstream_model_id)
        operation_id = result.get("operation_id") or result.get("id")
        upgraded = model_registry.withhold_model(db, bank, definition.key, operation_id)
    else:
        upgraded = existing
    db.commit()
    return _to_view(upgraded)


def _repair_not_before(now: datetime) -> datetime:
    return now + timedelta(seconds=REPAIR_BACKOFF_SECONDS)


def observe_model_refresh(
    db: Session, bank: LogicalBankRef, model_key: str, *, client
) -> MentalModelView:
    """Exact refresh-completion check (SPEC §6.4): reads only the row's own
    recorded `refresh_operation_id`. Pending and failed operations leave the
    model withheld; only a matching successful terminal status sets ready.
    A mismatched operation id is ignored and logged, never trusted as proof
    of currentness.
    """
    row = model_registry.get_registered_model(db, bank, model_key)
    if row is None or row.lifecycle_state == "deleted":
        raise MentalModelNotFound("no registered model with that logical key")
    if row.delivery_state != "withheld" or row.refresh_operation_id is None:
        return _to_view(row)

    operation = client.get_operation(bank.bank_id, row.refresh_operation_id)
    # Hindsight 0.9.2's get_operation response carries the id under
    # "operation_id", never "id" -- measured live against a disposable
    # deployment (this comparison using "id" never matched anything real,
    # so a model could never observe its way from withheld to ready).
    if operation.get("operation_id") != row.refresh_operation_id:
        logger.warning(
            "model_key=%s ignored a refresh operation observation whose id did not "
            "match the recorded operation",
            model_key,
        )
        return _to_view(row)

    status = operation.get("status")
    if status == "completed":
        ready = model_registry.ready_model(db, bank, model_key, row.refresh_operation_id)
        db.commit()
        return _to_view(ready)
    if status == "failed":
        now = db.execute(select(func.now())).scalar_one()
        failed = model_registry.mark_refresh_failed(
            db, bank, model_key, row.refresh_operation_id, repair_not_before=_repair_not_before(now)
        )
        db.commit()
        return _to_view(failed)
    # Any other status (Hindsight 0.9.2 measured live: "pending", then
    # "processing") is non-terminal -- a terminal-status whitelist, the same
    # shape retention.py's _TERMINAL_STATUSES uses, so an unrecognized future
    # status defaults to "still in progress" rather than "must have failed".
    return _to_view(row)


def repair_one_model(
    db: Session, bank: LogicalBankRef, *, now: datetime, client
) -> MentalModelView | None:
    """At most one repair action per access (SPEC §6.4): while the physical
    bank barrier is active, defer entirely to bank-level reconciliation.
    Otherwise submit exactly one refresh for the oldest eligible failed
    model past its own backoff, and return -- never loop, never wait."""
    if bank_is_withheld(db, bank):
        return None

    row = model_registry.oldest_failed_model(db, bank, now=now)
    if row is None:
        return None

    try:
        result = client.refresh_mental_model(bank.bank_id, row.upstream_model_id)
        operation_id = result.get("operation_id") or result.get("id")
        repaired = model_registry.record_model_refresh_operation(db, bank, row.model_key, operation_id)
    except DomainError:
        repaired = model_registry.mark_refresh_failed(
            db, bank, row.model_key, row.refresh_operation_id,
            repair_not_before=_repair_not_before(now),
        )
    db.commit()
    return _to_view(repaired)
