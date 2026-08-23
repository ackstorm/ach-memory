"""The fifteen MCP tools of SPEC §11.

Each is a plain `def` on purpose: the SDK runs a synchronous tool in an AnyIO
worker thread, so this project's synchronous stack works here unchanged, while
an `async def` would run on the event loop and block it on every database call.

Every tool body is a single `_run(...)` call — build a request, run the
shared pipeline, call the client, strip the bank id — because SPEC §11.1
requires the authenticate/resolve/authorize/bank steps to be centralized. A
tool that grew its own version of any of them would be the bug.

`_run` is also the one place that turns an exception into what an MCP client
sees. Left alone, the SDK dispatcher wraps ANY exception a tool raises as
`f"Error executing tool {name}: {e}"` and ships the result over the wire —
so an unguarded backend error (a psycopg IntegrityError, a stray bank id in
a URL) is echoed to the caller verbatim. `_run` catches everything a tool
body can raise and converts it to `MCPToolError` before it gets anywhere
near that dispatcher: a `DomainError` keeps its SPEC §18 `code`/`details`
(the same disclosure REST's JSON envelope makes), a validation failure keeps
its safe, caller-authored message, and anything else becomes a fixed
"internal error" logged server-side only.
"""

import logging
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ValidationError

from memory import provenance
from memory.api.curation import CorrectRequest, ListMemoriesRequest
from memory.api.documents import ListDocumentsRequest
from memory.api.memory import (
    RetainRequest,
    ScopedRequest,
    _check_content_size,
    _resolve_bank,
    _strip_bank_id,
)
from memory.api.operations import ListOperationsRequest
from memory.errors import DomainError
from memory.hindsight.client import get_client
from memory.mcp.server import tool_session

logger = logging.getLogger("memory.mcp")

Scope = Literal["user", "project"]

# Populated by register(). The tests call through it, so an unregistered tool
# fails there exactly as it would over the wire.
REGISTRY: dict[str, Any] = {}


class ToolResult(BaseModel):
    """A BaseModel, not a dict: the SDK only emits structured output for one."""

    result: dict[str, Any]
    # Set only when the caller used a retired project slug (SPEC §8.6).
    project_slug: str | None = None
    resolved_from: str | None = None
    notice: str | None = None


class MCPToolError(Exception):
    """The only exception shape `_run` lets escape a tool body.

    An MCP client ultimately only sees `str(exc)` — the SDK's dispatcher
    wraps a raised exception as `f"Error executing tool {name}: {e}"` and
    drops every other attribute — so `code`/`details` are encoded into the
    text itself, the only channel that survives that wrapping. `.code`,
    `.message` and `.details` also stay as real attributes for anything that
    can see the exception object directly (tests included).
    """

    def __init__(
        self, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        text = f"{code}: {message}"
        if self.details:
            text = f"{text} {self.details}"
        super().__init__(text)


def _validation_message(exc: ValidationError) -> str:
    """A pydantic error's own messages are safe to surface verbatim: they
    describe the shape of the caller's OWN input, never server state."""
    return "; ".join(e["msg"] for e in exc.errors())


def _run(
    ctx: Context,
    body_factory,
    action: str,
    call,
    *,
    create: bool,
    is_write: bool = False,
) -> ToolResult:
    """The shared pipeline. `body_factory` takes no arguments and returns the
    validated `ScopedRequest` (or subclass) for this call — built inside the
    try below so a bad field is caught by the same boundary as everything
    else. `call` receives (bank_id, db, principal, project_slug) — the LAST
    one is `_resolve_bank`'s resolved, current slug (None for scope=user),
    never the caller's raw argument: it is the only value SPEC §13.4 allows
    into extraction metadata, since a project rename or an attacker-chosen
    scope=user slug must not ride along untouched.

    `on_behalf_of` is hardcoded to None here because it is moot: `tool_session`
    refuses `principal.is_master` outright (invariant 22 — the master key
    never resides in an ordinary agent runtime, and MCP is exactly that), so
    every principal `_run` ever sees is a user key, for which `on_behalf_of`
    is always None on REST too. There is no MCP equivalent of REST's
    `current_on_behalf_of` header dependency to wire, and none is needed
    unless a future task adds master-key delegation over MCP on purpose.

    `is_write` forwards to `_resolve_bank`'s rate-limit gate (SPEC §20) — a
    caller cannot dodge the REST limit by switching to the MCP twin, since
    both funnel through the same `_resolve_bank` and the same per-credential
    `memory.ratelimit.Limiter`.
    """
    try:
        body = body_factory()
        with tool_session(ctx) as tc:
            bank_id, resolved_from, slug = _resolve_bank(
                body, tc.db, tc.principal, None, action,
                create=create, is_write=is_write,
            )
            # Commit before the upstream call: resolution may have created the
            # project that owns this bank_id, and rolling that back after the
            # bank is materialized upstream orphans it unreachably.
            tc.db.commit()
            result = call(bank_id, tc.db, tc.principal, slug)
    except DomainError as exc:
        # Same disclosure REST's JSON envelope makes (code + message +
        # details) — SPEC §18 already decided a `ProjectAccessDenied`'s
        # project_slug/owner_type, for instance, is meant to reach the
        # caller; nothing here adds a new leak, it just stops MCP from
        # throwing that decision away.
        raise MCPToolError(exc.code, exc.message, exc.details) from None
    except ValidationError as exc:
        raise MCPToolError("INVALID_REQUEST", _validation_message(exc)) from None
    except Exception as exc:
        # Anything else is unexpected and may carry backend internals (SQL,
        # a connection string, a bank id) in its text — logged here, for our
        # eyes only, and never echoed to the caller.
        logger.error("unhandled MCP tool error", exc_info=exc)
        raise MCPToolError("INTERNAL_ERROR", "internal error") from None
    return ToolResult(
        result=_strip_bank_id(result, bank_id),
        project_slug=slug,
        resolved_from=resolved_from,
        notice="PROJECT_RENAMED" if resolved_from else None,
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Store something worth remembering. Returns immediately with an "
            "operation you can follow with get_operation; use sync_retain when "
            "you need to read it back straight away."
        ),
    )
    def retain(
        scope: Scope,
        content: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        document_id: str | None = None,
        update_mode: str = "replace",
        metadata: dict[str, str] | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        return _retain(
            ctx, scope, content, project_slug, git_locator, document_id,
            update_mode, metadata, operation_id, is_async=True,
        )

    @mcp.tool(
        description="Store something and wait until it is searchable.",
    )
    def sync_retain(
        scope: Scope,
        content: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        document_id: str | None = None,
        update_mode: str = "replace",
        metadata: dict[str, str] | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        # No idempotentHint: two calls with no document_id write two separate
        # memories, same as retain -- this only blocks longer while Hindsight
        # makes the write searchable before returning. A true hint here would
        # invite an LLM client to retry blindly on a timeout and duplicate the
        # write.
        return _retain(
            ctx, scope, content, project_slug, git_locator, document_id,
            update_mode, metadata, operation_id, is_async=False,
        )

    @mcp.tool(
        description="Search memory and return the matching facts.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def recall(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        # is_write=True: with create=True (the default), an unmetered loop of
        # recall(scope="project", project_slug=<random>) mints one Project row
        # per call -- each permanently squatting a tenant-unique slug
        # (invariant 8: unique across live AND retired names, never
        # recoverable). Measured live at 80 projects in 5.1s against one key.
        # recall spends embedding tokens on the same server-level credential
        # `reflect` is gated for, plus this persistent side effect `reflect`
        # doesn't have -- so the same gate applies here, even though the read
        # itself is free.
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.recall",
            lambda bank, db, p, slug: get_client().recall(bank, query),
            create=True,
            is_write=True,
        )

    @mcp.tool(
        description=(
            "Ask memory a question and get a synthesized answer rather than "
            "a list of facts. Costs more than recall."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def reflect(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.reflect",
            lambda bank, db, p, slug: get_client().reflect(bank, query),
            create=True,
            is_write=True,
        )

    @mcp.tool(
        description="List stored memories, most recent first.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def list_memories(
        scope: Scope,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        q: str | None = None,
        type: str | None = None,
        state: str | None = None,
        document_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            # Reuses ListMemoriesRequest itself (the REST model) rather than a
            # bare ScopedRequest -- the same fix _retain already got for
            # RetainRequest. A bare ScopedRequest here dropped `state`'s
            # Literal["valid","invalidated"] bound and both `Field(ge=0)`
            # bounds, so a bogus state or a negative limit reached Hindsight
            # as a 502 blaming the backend instead of a typed rejection at
            # the boundary.
            lambda: ListMemoriesRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator,
                q=q, type=type, state=state, document_id=document_id,
                limit=limit, offset=offset,
            ),
            "memory.list",
            lambda bank, db, p, slug: get_client().list_memories(
                bank, q=q, type=type, state=state, document_id=document_id,
                limit=limit, offset=offset,
            ),
            create=False,
        )

    @mcp.tool(
        description="Fetch one memory by id.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_memory(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.get",
            lambda bank, db, p, slug: get_client().get_memory(bank, memory_id),
            create=False,
        )

    @mcp.tool(
        description=(
            "Retire a memory that is wrong or obsolete. It is invalidated, not "
            "deleted: it leaves the active set but the record survives, and "
            "restore brings it back."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def forget(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        reason: str | None = None,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.forget",
            lambda bank, db, p, slug: get_client().curate(
                bank, memory_id, state="invalidated", reason=reason
            ),
            create=False,
            is_write=True,
        )

    @mcp.tool(description="Replace the text of an existing memory.")
    def correct(
        scope: Scope,
        memory_id: str,
        content: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        def body_factory() -> CorrectRequest:
            # Reuses CorrectRequest itself rather than a bare ScopedRequest --
            # the same fix _retain already got for RetainRequest. A bare
            # ScopedRequest here dropped `content`'s min_length=1/_not_blank
            # bound, so a blank correct on a valid memory reached Hindsight and
            # came back as 409 MEMORY_NOT_CURATABLE -- telling the caller the
            # memory is a derived observation when it simply sent nothing
            # (review finding I5, reopened as F1). Also runs
            # _check_content_size here, which only retain's paths ran before.
            body = CorrectRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator,
                memory_id=memory_id, content=content,
            )
            _check_content_size(body.content)
            return body

        return _run(
            ctx,
            body_factory,
            "memory.correct",
            lambda bank, db, p, slug: get_client().curate(
                bank, memory_id, text=content
            ),
            create=False,
            is_write=True,
        )

    @mcp.tool(
        description="Bring back a memory that forget retired.",
        annotations=ToolAnnotations(idempotentHint=True),
    )
    def restore(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.restore",
            lambda bank, db, p, slug: get_client().curate(
                bank, memory_id, state="valid"
            ),
            create=False,
            is_write=True,
        )

    @mcp.tool(
        description=(
            "List the documents memories were derived from. A document id is "
            "yours to choose — a PR, a file, a session — and is shared by "
            "everyone authorized for this memory."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def list_documents(
        scope: Scope,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        q: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ToolResult:
        return _list_documents(
            ctx, scope, project_slug, git_locator, q, limit, offset
        )

    @mcp.tool(
        description="Fetch one document by its id.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_document(
        scope: Scope,
        document_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.documents.get",
            lambda bank, db, p, slug: get_client().get_document(bank, document_id),
            create=False,
        )

    @mcp.tool(
        description=(
            "Delete a document AND every memory derived from it. This is "
            "irreversible — unlike forget, nothing restores it. The document "
            "is shared, so this affects everyone using this memory."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def delete_document(
        scope: Scope,
        document_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.documents.delete",
            lambda bank, db, p, slug: get_client().delete_document(
                bank, document_id
            ),
            create=False,
            is_write=True,
        )

    @mcp.tool(
        description="Check whether an async retain has finished.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_operation(
        scope: Scope,
        operation_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.operations.get",
            lambda bank, db, p, slug: get_client().get_operation(
                bank, operation_id
            ),
            create=False,
        )

    @mcp.tool(
        description="List recent async operations.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def list_operations(
        scope: Scope,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        status: str | None = None,
        type: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ToolResult:
        return _list_operations(
            ctx, scope, project_slug, git_locator, status, type, limit, offset
        )

    @mcp.tool(
        description="Cancel a pending async operation.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def cancel_operation(
        scope: Scope,
        operation_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            ),
            "memory.operations.cancel",
            lambda bank, db, p, slug: get_client().cancel_operation(
                bank, operation_id
            ),
            create=False,
            is_write=True,
        )

    REGISTRY.update(
        retain=retain,
        sync_retain=sync_retain,
        recall=recall,
        reflect=reflect,
        list_memories=list_memories,
        get_memory=get_memory,
        forget=forget,
        correct=correct,
        restore=restore,
        list_documents=list_documents,
        get_document=get_document,
        delete_document=delete_document,
        get_operation=get_operation,
        list_operations=list_operations,
        cancel_operation=cancel_operation,
    )


def _retain(ctx, scope, content, project_slug, git_locator, document_id,
            update_mode, metadata, operation_id, *, is_async: bool) -> ToolResult:
    def body_factory() -> RetainRequest:
        # Reuses RetainRequest itself rather than re-deriving its rules: this
        # is what keeps update_mode/operation_id/the append-needs-a-document_id
        # rule identical to REST's, instead of a second, silently-drifting
        # copy the way a bare ScopedRequest let them drift before.
        body = RetainRequest(
            scope=scope,
            project_slug=project_slug,
            git_locator=git_locator,
            content=content,
            document_id=document_id,
            update_mode=update_mode,
            metadata=metadata,
            operation_id=operation_id,
        )
        _check_content_size(body.content)
        return body

    def call(bank_id, db, principal, slug):
        # provenance.build must run before retain: a reserved metadata key
        # must raise before anything is written upstream (SPEC §13.4). It is
        # called first in this body, not last.
        #
        # `slug` is `_resolve_bank`'s RESOLVED project slug, not the raw
        # `project_slug` argument above: None for scope=user (the argument is
        # meaningless there and must never be stamped into extraction
        # metadata), and the project's current, live slug for scope=project
        # even when the caller named a retired one.
        extraction = provenance.build(metadata, project_slug=slug)
        client = get_client()
        return client.retain(
            bank_id, content, document_id=document_id,
            metadata=extraction or None, context=provenance.context_line(extraction),
            update_mode=update_mode, is_async=is_async, operation_id=operation_id,
        )

    return _run(ctx, body_factory, "memory.retain", call, create=True, is_write=True)


def _list_documents(ctx, scope, project_slug, git_locator, q, limit, offset) -> ToolResult:
    def body_factory() -> ListDocumentsRequest:
        # Reuses ListDocumentsRequest itself for its Field(ge=0) bound on
        # limit/offset -- the same fix _retain already got for RetainRequest.
        # A bare ScopedRequest here let a negative value reach Hindsight as a
        # 502 blaming the backend instead of a typed rejection at the
        # boundary. Unset fields are OMITTED, not passed as None: the model's
        # own concrete defaults (100/0) are for validation only here, so a
        # caller who leaves limit/offset unset keeps today's behavior of
        # sending nothing and letting Hindsight's own default apply -- this
        # is a validation fix, not a wire-behavior change.
        kwargs: dict[str, Any] = {
            "scope": scope, "project_slug": project_slug,
            "git_locator": git_locator, "q": q,
        }
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset
        return ListDocumentsRequest(**kwargs)

    def call(bank_id, db, principal, slug):
        return get_client().list_documents(bank_id, q=q, limit=limit, offset=offset)

    return _run(ctx, body_factory, "memory.documents.list", call, create=False)


def _list_operations(
    ctx, scope, project_slug, git_locator, status, type, limit, offset
) -> ToolResult:
    def body_factory() -> ListOperationsRequest:
        # Same reasoning as _list_documents above, against ListOperationsRequest.
        kwargs: dict[str, Any] = {
            "scope": scope, "project_slug": project_slug,
            "git_locator": git_locator, "status": status, "type": type,
        }
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset
        return ListOperationsRequest(**kwargs)

    def call(bank_id, db, principal, slug):
        return get_client().list_operations(
            bank_id, status=status, type=type, limit=limit, offset=offset
        )

    return _run(ctx, body_factory, "memory.operations.list", call, create=False)
