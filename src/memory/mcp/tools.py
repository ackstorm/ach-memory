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
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from memory import activity, metrics, provenance
from memory.api.curation import CorrectRequest, ListMemoriesRequest
from memory.api.documents import ListDocumentsRequest
from memory.api.memory import (
    MAX_PAGE_SIZE,
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

# The tool SIGNATURE is what the SDK turns into the advertised JSON Schema, so
# a bound living only on the pydantic model is invisible to the model calling
# the tool. REST's OpenAPI publishes these enums for the identical operations;
# MCP published bare `str`. SPEC §11.4 blesses update_mode="append" for
# interactive coding sessions and nothing in the advertised schema said
# `append` existed at all.
UpdateMode = Literal["replace", "append"]
MemoryState = Literal["valid", "invalidated"]
FactType = Literal["world", "experience", "observation"]
PageLimit = Annotated[int | None, Field(ge=1, le=MAX_PAGE_SIZE)]
PageOffset = Annotated[int | None, Field(ge=0)]

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
    describe the shape of the caller's OWN input, never server state.

    `loc` is included for the same reason: it names the caller's own fields.
    Without it a multi-field failure read "Input should be 'user' or
    'project'; Input should be greater than or equal to 0" with no indication
    of WHICH arguments were wrong, so INVALID_REQUEST was unactionable.
    """
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in exc.errors()
    )


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
    validated `ScopedRequest` (or subclass) for this call — built inside
    `with tool_session(ctx)`, after authentication, so a caller who presented
    no credential never reaches pydantic's bounds or `_check_content_size`
    (both would otherwise hand an unauthenticated party a free oracle for the
    request shape and the configured content-size limit). It is also inside
    the same try, so the ValidationError/DomainError mapping below is
    unchanged. `call` receives (bank_id, db, principal, project_slug) — the LAST
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
    activity.new_call()
    try:
        with tool_session(ctx) as tc:
            # body_factory runs INSIDE the session, not before it: tool_session
            # is the only thing that reads the Authorization header, so
            # building the model first meant every pydantic bound and
            # _check_content_size executed for an unauthenticated caller.
            # REST resolves current_principal before any handler body runs;
            # this is the same ordering. It stays inside the same try, so the
            # ValidationError/DomainError mapping below is unchanged.
            body = body_factory()
            bank_id, resolved_from, slug = _resolve_bank(
                body, tc.db, tc.principal, None, action,
                create=create, is_write=is_write,
            )
            # Commit before the upstream call: resolution may have created the
            # project that owns this bank_id, and rolling that back after the
            # bank is materialized upstream orphans it unreachably.
            tc.db.commit()
            result = call(bank_id, tc.db, tc.principal, slug)
            # Built inside the try so a failure cannot escape to the SDK
            # dispatcher, which would wrap it verbatim -- including pydantic's
            # `input_value=` repr of the upstream payload.
            #
            # Its OWN except, though, not the outer INVALID_REQUEST one:
            # ToolResult.result is typed dict[str, Any], so an upstream 200
            # whose body is a JSON array or scalar fails here -- and by this
            # point the bank is resolved, the row is committed and the upstream
            # call has happened. SPEC §18 defines INVALID_REQUEST as input that
            # "failed validation before anything was resolved or written", so
            # reporting this as INVALID_REQUEST would blame the caller for an
            # upstream response shape. INTERNAL_ERROR is §18's catch-all "for
            # an exception no DomainError subclass claims", which is what this
            # is.
            try:
                return ToolResult(
                    result=_strip_bank_id(result, bank_id),
                    project_slug=slug,
                    resolved_from=resolved_from,
                    notice="PROJECT_RENAMED" if resolved_from else None,
                )
            except ValidationError as exc:
                logger.error("upstream response was not a JSON object", exc_info=exc)
                raise MCPToolError("INTERNAL_ERROR", "internal error") from None
    except DomainError as exc:
        # Same disclosure REST's JSON envelope makes (code + message +
        # details) — SPEC §18 already decided a `ProjectAccessDenied`'s
        # project_slug/owner_type, for instance, is meant to reach the
        # caller; nothing here adds a new leak, it just stops MCP from
        # throwing that decision away.
        metrics.ERRORS.labels(code=exc.code).inc()
        activity.set_error(exc.code)
        raise MCPToolError(exc.code, exc.message, exc.details) from None
    except ValidationError as exc:
        metrics.ERRORS.labels(code="INVALID_REQUEST").inc()
        activity.set_error("INVALID_REQUEST")
        raise MCPToolError("INVALID_REQUEST", _validation_message(exc)) from None
    except MCPToolError as exc:
        # Already the intended shape (e.g. the malformed-upstream-body
        # branch above, raised INSIDE the try on purpose so it reaches THIS
        # except chain rather than the SDK dispatcher). Without this branch
        # `except Exception` below caught it too, logged a second, identical
        # "unhandled MCP tool error", and re-raised an equivalent error --
        # noise, since the first log line already said everything (review
        # finding 6, 2026-08-23).
        activity.set_error(getattr(exc, "code", "INTERNAL_ERROR"))
        raise
    except Exception as exc:
        # Anything else is unexpected and may carry backend internals (SQL,
        # a connection string, a bank id) in its text — logged here, for our
        # eyes only, and never echoed to the caller.
        logger.error("unhandled MCP tool error", exc_info=exc)
        metrics.ERRORS.labels(code="INTERNAL_ERROR").inc()
        activity.set_error("INTERNAL_ERROR")
        raise MCPToolError("INTERNAL_ERROR", "internal error") from None
    finally:
        activity.finish("mcp")


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
        update_mode: UpdateMode = "replace",
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
        update_mode: UpdateMode = "replace",
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
        # No readOnlyHint. `create=True` below mints a Project row for an
        # unseen slug -- permanently, since invariant 8 makes a slug unique
        # across live AND retired names, so none is ever recoverable.
        # readOnlyHint is what an MCP client uses to skip confirmation and
        # auto-approve inside an agent loop, so advertising it here invited
        # exactly the squat the comment below measures (80 projects in 5.1s).
        description="Search memory and return the matching facts.",
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
        def body_factory() -> ScopedRequest:
            body = ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            )
            # Same MEMORY_MAX_CONTENT_BYTES ceiling REST's recall carries
            # (SPEC §20) -- REST's _check_content_size(body.query) was never
            # mirrored here, so MCP forwarded an oversize query straight to
            # Hindsight.
            _check_content_size(query)
            return body

        return _run(
            ctx,
            body_factory,
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
        # No readOnlyHint, same reason as recall: create=True mints a
        # permanent Project row for an unseen slug.
    )
    def reflect(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        def body_factory() -> ScopedRequest:
            body = ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            )
            # reflect spends model tokens on a server-level credential with no
            # per-user cost attribution (SPEC §19.4) -- the same cap REST's
            # _check_content_size(body.query) already applies, mirrored here.
            _check_content_size(query)
            return body

        return _run(
            ctx,
            body_factory,
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
        type: FactType | None = None,
        state: MemoryState | None = None,
        document_id: str | None = None,
        limit: PageLimit = None,
        offset: PageOffset = None,
    ) -> ToolResult:
        def body_factory() -> ListMemoriesRequest:
            # Reuses ListMemoriesRequest itself (the REST model) rather than a
            # bare ScopedRequest -- the same fix _retain already got for
            # RetainRequest. A bare ScopedRequest here dropped `state`'s
            # Literal["valid","invalidated"] bound and both `Field(ge=0)`
            # bounds, so a bogus state or a negative limit reached Hindsight
            # as a 502 blaming the backend instead of a typed rejection at
            # the boundary.
            body = ListMemoriesRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator,
                q=q, type=type, state=state, document_id=document_id,
                limit=limit, offset=offset,
            )
            # q is a caller-authored search query, same embedding-spend risk
            # class as recall's query; optional, so guarded.
            if q is not None:
                _check_content_size(q)
            return body

        return _run(
            ctx,
            body_factory,
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
        # idempotentHint, not destructiveHint: this description says in so
        # many words that the record survives and `restore` brings it back.
        annotations=ToolAnnotations(idempotentHint=True),
    )
    def forget(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        reason: str | None = None,
        project_slug: str | None = None,
        git_locator: str | None = None,
    ) -> ToolResult:
        def body_factory() -> ScopedRequest:
            body = ScopedRequest(
                scope=scope, project_slug=project_slug, git_locator=git_locator
            )
            # reason is caller free text forwarded verbatim to Hindsight;
            # optional, so guarded like the UPDATE routes' `if x is not None`.
            if reason is not None:
                _check_content_size(reason)
            return body

        return _run(
            ctx,
            body_factory,
            "memory.forget",
            lambda bank, db, p, slug: get_client().curate(
                bank, memory_id, state="invalidated", reason=reason
            ),
            create=False,
            is_write=True,
        )

    @mcp.tool(
        description="Replace the text of an existing memory.",
        # The one memory operation that irreversibly overwrites caller text,
        # and it carried no annotations at all.
        annotations=ToolAnnotations(destructiveHint=True),
    )
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
        # destructiveHint=False stated explicitly: the MCP spec DEFAULTS it to
        # true, so a purely additive operation was advertised as destructive.
        annotations=ToolAnnotations(idempotentHint=True, destructiveHint=False),
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
        limit: PageLimit = None,
        offset: PageOffset = None,
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
        limit: PageLimit = None,
        offset: PageOffset = None,
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
        # SPEC §13.4: a reserved metadata key must raise with NOTHING
        # written -- and the same holds for an oversize metadata mapping
        # (CONTENT_TOO_LARGE). body_factory runs before _resolve_bank/commit
        # (Task 19), so checking here -- rather than in `call`, which only
        # runs after the project row is committed -- is what keeps a refused
        # retain from permanently squatting the project slug it named
        # (invariant 8: slugs are unique across live AND retired names,
        # never recoverable). `provenance.build` runs both checks (reserved
        # keys, then the size cap); its return value is discarded here
        # because it doesn't have the resolved slug yet -- `call` below
        # re-runs `build` to stamp that in after resolution. Review finding
        # 3 (2026-08-23): the size cap used to run only inside `call`, after
        # `tc.db.commit()`, so an oversize retain left the project row
        # committed -- reproduced live as `mcp-squat` staying unreclaimable.
        provenance.build(metadata, project_slug=None)
        return body

    def call(bank_id, db, principal, slug):
        # The reserved-key check and the size cap both already ran in
        # body_factory, before the project row was committed (SPEC §13.4).
        # build() re-runs both (cheap, and keeps build() correct on its own
        # for REST's callers), but its real job here is stamping the
        # RESOLVED slug -- unavailable until after _resolve_bank -- into the
        # extraction mapping.
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
        body = ListDocumentsRequest(**kwargs)
        # q is a caller-authored search query, same embedding-spend risk
        # class as recall's query; optional, so guarded.
        if q is not None:
            _check_content_size(q)
        return body

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
