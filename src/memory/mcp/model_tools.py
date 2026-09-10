"""Governed mental-model MCP tools (SPEC §7, §10.1): the same closed
request contracts and service `api/mental_models.py` uses, so REST and MCP
share one governed lifecycle rather than growing a second one.

`create_mental_model`/`update_mental_model`/`refresh_mental_model`/
`delete_mental_model` change durable, shared model configuration for every
authorized consumer of the bank -- their descriptions say so and their
annotations are never `readOnlyHint=True`, so a host's confirmation policy
sees them as the write they are. `list_mental_models`/`get_mental_model` are
genuinely read-only: neither provisions or reconciles a definition (SPEC
§7.5's bootstrap does that, separately).
"""

import json
import logging
import uuid
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError

from memory import activity, mental_model_service, metrics
from memory.api.memory import ScopedRequest, _check_content_size
from memory.api.mental_models import (
    CreateMentalModelRequest,
    MutationScopedRequest,
    UpdateMentalModelRequest,
    resolve_logical_bank,
)
from memory.errors import DomainError, ProjectNotFound
from memory.hindsight.client import get_client
from memory.mcp.server import tool_session
from memory.mcp.tools import (
    ABSENT_PROJECT_STILL_RAISES,
    REGISTRY,
    MCPToolError,
    ToolResult,
    _internal_error,
    _invalid_request,
)
from memory.mental_model_service import CustomModelCreateRequest, CustomModelUpdateRequest

logger = logging.getLogger("memory.mcp")

Scope = Literal["user", "project"]
MaxTokens = Annotated[int, Field(ge=256, le=8192)]
OptionalMaxTokens = Annotated[int | None, Field(default=None, ge=256, le=8192)]


def _model_run(
    ctx: Context,
    body_factory,
    action: str,
    call,
    *,
    is_write: bool,
    empty_result: dict[str, Any] | object = ABSENT_PROJECT_STILL_RAISES,
) -> ToolResult:
    """Same authorize/resolve/authorize-then-call shape as `memory_tools._run`,
    reshaped for a `LogicalBankRef` instead of a bare `bank_id`.

    `empty_result`, when given, is what list_mental_models/get_mental_model
    return instead of raising PROJECT_NOT_FOUND (decision 3) -- the four
    mutation tools that share this pipeline never pass it, so they are
    unaffected."""
    activity.new_call()
    try:
        with tool_session(ctx) as tc:
            body = body_factory()
            try:
                bank = resolve_logical_bank(
                    body, tc.db, tc.principal, None, action, is_write=is_write
                )
            except ProjectNotFound:
                if empty_result is ABSENT_PROJECT_STILL_RAISES:
                    raise
                return ToolResult(result=dict(empty_result))
            tc.db.commit()
            result = call(bank, tc.db, body)
            return ToolResult(result=result)
    except DomainError as exc:
        metrics.ERRORS.labels(code=exc.code).inc()
        activity.set_error(exc.code)
        raise MCPToolError(exc.code, exc.message, exc.details) from None
    except ValidationError as exc:
        metrics.ERRORS.labels(code="INVALID_REQUEST").inc()
        activity.set_error("INVALID_REQUEST")
        raise _invalid_request(exc) from None
    except MCPToolError as exc:
        activity.set_error(getattr(exc, "code", "INTERNAL_ERROR"))
        raise
    except Exception as exc:
        logger.error("unhandled MCP tool error", exc_info=exc)
        metrics.ERRORS.labels(code="INTERNAL_ERROR").inc()
        activity.set_error("INTERNAL_ERROR")
        raise _internal_error() from None
    finally:
        activity.finish("mcp")


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Create a new custom governed mental model. This changes durable "
            "shared model configuration for every authorized consumer of this "
            "bank and requires host confirmation -- it is not a private note. "
            "`source_tags` narrows what the model summarizes to memories "
            "carrying those tags, same convention as recall (e.g. "
            "`repo:group/app`); omit it to summarize everything this bank "
            "holds. Tags are ANDed, and a memory must carry every one of "
            "them."
        ),
        annotations=ToolAnnotations(destructiveHint=False),
    )
    def create_mental_model(
        scope: Scope,
        name: str,
        source_query: str,
        max_tokens: MaxTokens,
        trigger: dict[str, object],
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        operation_id: str | None = None,
        source_tags: list[str] | None = None,
    ) -> ToolResult:
        def body_factory() -> CreateMentalModelRequest:
            _check_content_size(source_query)
            body = CreateMentalModelRequest(
                scope=scope,
                project_slug=project_slug,
                git_locator=git_locator,
                name=name,
                source_query=source_query,
                source_tags=tuple(source_tags or ()),
                max_tokens=max_tokens,
                trigger=trigger,
                operation_id=operation_id or str(uuid.uuid4()),
            )
            _check_content_size(json.dumps(body.trigger.model_dump(exclude_none=True)))
            return body

        def call(bank, db, body: CreateMentalModelRequest):
            request = CustomModelCreateRequest(
                name=body.name,
                source_query=body.source_query,
                source_tags=body.source_tags,
                max_tokens=body.max_tokens,
                trigger=body.trigger.model_dump(exclude_none=True),
                operation_id=body.operation_id,
            )
            view = mental_model_service.create_custom_model(
                db, bank, request, client=get_client()
            )
            return view.model_dump(mode="json")

        return _model_run(ctx, body_factory, "mental_models.create", call, is_write=True)

    @mcp.tool(
        description=(
            "List the mental models registered on this bank -- at most one "
            "built-in plus five custom models -- and how many additional "
            "unrecognized upstream models exist without adopting or exposing "
            "their content."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def list_mental_models(
        scope: Scope,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        def body_factory() -> ScopedRequest:
            return ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator)

        def call(bank, db, body):
            result = mental_model_service.list_models(db, bank, client=get_client())
            return result.model_dump(mode="json")

        return _model_run(
            ctx, body_factory, "mental_models.list", call, is_write=False,
            empty_result={"models": [], "unknown_upstream_count": 0},
        )

    @mcp.tool(
        description="Fetch one registered mental model's governance metadata by its logical key.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_mental_model(
        scope: Scope,
        model_key: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        def body_factory() -> ScopedRequest:
            return ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator)

        def call(bank, db, body):
            view = mental_model_service.get_model(db, bank, model_key)
            if view.delivery_state == "withheld":
                view = mental_model_service.observe_model_refresh(
                    db, bank, model_key, client=get_client()
                )
            return view.model_dump(mode="json")

        return _model_run(
            ctx, body_factory, "mental_models.get", call, is_write=False,
            empty_result={},
        )

    @mcp.tool(
        description=(
            "Change a custom mental model's display name, prompt, trigger, "
            "or budget. This changes durable shared model configuration for "
            "every authorized consumer of this bank and requires host "
            "confirmation. A built-in model's definition cannot be changed "
            "this way."
        ),
        annotations=ToolAnnotations(destructiveHint=False),
    )
    def update_mental_model(
        scope: Scope,
        model_key: str,
        ctx: Context,
        name: str | None = None,
        source_query: str | None = None,
        max_tokens: OptionalMaxTokens = None,
        trigger: dict[str, object] | None = None,
        project_slug: str | None = None,
        git_locator: str | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        def body_factory() -> UpdateMentalModelRequest:
            if source_query is not None:
                _check_content_size(source_query)
            body = UpdateMentalModelRequest(
                scope=scope,
                project_slug=project_slug,
                git_locator=git_locator,
                name=name,
                source_query=source_query,
                max_tokens=max_tokens,
                trigger=trigger,
                operation_id=operation_id or str(uuid.uuid4()),
            )
            if body.trigger is not None:
                _check_content_size(json.dumps(body.trigger.model_dump(exclude_none=True)))
            return body

        def call(bank, db, body: UpdateMentalModelRequest):
            request = CustomModelUpdateRequest(
                name=body.name,
                source_query=body.source_query,
                max_tokens=body.max_tokens,
                trigger=body.trigger.model_dump(exclude_none=True) if body.trigger else None,
                operation_id=body.operation_id,
            )
            view = mental_model_service.update_model(
                db, bank, model_key, request, client=get_client()
            )
            return view.model_dump(mode="json")

        return _model_run(ctx, body_factory, "mental_models.update", call, is_write=True)

    @mcp.tool(
        description=(
            "Request an immediate refresh of a custom mental model's "
            "synthesis. Costs a full reflect upstream and requires host "
            "confirmation. Output is withheld until the refresh operation "
            "succeeds."
        ),
        annotations=ToolAnnotations(destructiveHint=False),
    )
    def refresh_mental_model(
        scope: Scope,
        model_key: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        def body_factory() -> MutationScopedRequest:
            return MutationScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator,
                operation_id=operation_id or str(uuid.uuid4()),
            )

        def call(bank, db, body: MutationScopedRequest):
            view = mental_model_service.refresh_model(
                db, bank, model_key, operation_id=body.operation_id, client=get_client()
            )
            return view.model_dump(mode="json")

        return _model_run(ctx, body_factory, "mental_models.refresh", call, is_write=True)

    @mcp.tool(
        description=(
            "Delete a custom mental model. This changes durable shared model "
            "configuration for every authorized consumer of this bank and "
            "requires host confirmation. A built-in model cannot be deleted "
            "this way. Idempotent: deleting an already-deleted model is a "
            "no-op success."
        ),
        annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True),
    )
    def delete_mental_model(
        scope: Scope,
        model_key: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        def body_factory() -> MutationScopedRequest:
            return MutationScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator,
                operation_id=operation_id or str(uuid.uuid4()),
            )

        def call(bank, db, body: MutationScopedRequest):
            mental_model_service.delete_model(
                db, bank, model_key, operation_id=body.operation_id, client=get_client()
            )
            return {"deleted": True, "model_key": model_key}

        return _model_run(ctx, body_factory, "mental_models.delete", call, is_write=True)

    REGISTRY.update(
        create_mental_model=create_mental_model,
        list_mental_models=list_mental_models,
        get_mental_model=get_mental_model,
        update_mental_model=update_mental_model,
        refresh_mental_model=refresh_mental_model,
        delete_mental_model=delete_mental_model,
    )
