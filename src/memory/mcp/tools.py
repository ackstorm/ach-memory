"""MCP tool contracts and registration (SPEC §6): thin wrappers over the service layer.

Every tool authenticates, opens one DB session, builds a service request from
its arguments, calls the service, and wraps the response. `tool_session`
centralizes the first two steps and the error mapping so no tool body repeats
them.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ValidationError, model_serializer
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, authenticate
from memory.backend import get_backend
from memory.bank_ref import resolve_bank
from memory.context import LoadContextRequest
from memory.context import load as do_load_context
from memory.curation import CorrectRequest, CurationRequest
from memory.curation import correct as do_correct
from memory.curation import delete as do_delete
from memory.curation import forget as do_forget
from memory.curation import restore as do_restore
from memory.db import session_scope
from memory.errors import DomainError
from memory.read import GetRequest, HistoryRequest, ListRequest, RecallRequest, ReflectRequest
from memory.read import get_memory as do_get_memory
from memory.read import history as do_history
from memory.read import list_memories as do_list_memories
from memory.read import recall as do_recall
from memory.read import reflect as do_reflect
from memory.retain import RetainRequest
from memory.retain import submit as do_retain
from memory.tags import Basis, MemoryType
from memory.working_state import PutWorkingStateRequest, WorkingStateRequest
from memory.working_state import delete as do_ws_delete
from memory.working_state import get as do_ws_get
from memory.working_state import put as do_ws_put

logger = logging.getLogger("memory.mcp")
Scope = Literal["user", "project"]


class ToolResult(BaseModel):
    result: dict[str, Any]
    notice: str | None = None

    @model_serializer(mode="plain")
    def _serialize(self) -> dict[str, Any]:
        out: dict[str, Any] = {"result": self.result}
        if self.notice is not None:
            out["notice"] = self.notice
        return out


class MCPToolError(ToolError):
    """The only exception shape a tool lets escape.

    Subclasses the SDK's own `ToolError` (still a plain `Exception` in every way that
    matters here) rather than `Exception` directly: mcp 2.2.0's tool runner treats any
    OTHER exception as an unattributed crash and replaces its text with a bare
    "Error executing tool <name>", discarding `.code`/`.message` entirely. A `ToolError`
    is instead treated as anticipated and keeps `str(exc)` -- prefixed with
    "Error executing tool <name>: " by the SDK, which is the only channel that reaches
    an MCP client either way.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
        for e in exc.errors()
    )


@contextmanager
def tool_session(ctx: Context) -> Iterator[tuple[Principal, Session]]:
    """Authenticate the caller and open one DB session for this call.

    Commits on success; rolls back and translates the exception otherwise --
    a `DomainError` keeps its code, a pydantic `ValidationError` becomes
    `INVALID_REQUEST` with safe field messages, anything else is logged and
    becomes a generic `INTERNAL_ERROR`.
    """
    headers = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    with session_scope() as db:
        try:
            principal = authenticate(headers, db)
            yield principal, db
        except MCPToolError:
            raise
        except DomainError as exc:
            raise MCPToolError(exc.code, exc.message) from None
        except ValidationError as exc:
            raise MCPToolError("INVALID_REQUEST", _validation_message(exc)) from None
        except Exception as exc:
            logger.error("unhandled MCP tool error", exc_info=exc)
            raise MCPToolError("INTERNAL_ERROR", "internal error") from None
        else:
            db.commit()


def _result(response: BaseModel) -> ToolResult:
    return ToolResult(result=response.model_dump(mode="json"), notice=getattr(response, "notice", None))


# forget/restore/delete_memory share one request shape and one call pattern;
# only the service function, description and annotations differ.
_CURATION: tuple[tuple[str, Callable, str, ToolAnnotations | None], ...] = (
    (
        "forget",
        do_forget,
        (
            "Retire a memory that is wrong or obsolete. Soft-remove and reversible: "
            "`restore` brings it back. `reason` is recorded in the journal."
        ),
        None,
    ),
    (
        "restore",
        do_restore,
        "Undo a forget: bring a retired memory back to the active set.",
        None,
    ),
    (
        "delete_memory",
        do_delete,
        (
            "Erase a memory for good, including anything derived from it. "
            "Irreversible -- unlike forget, nothing restores it."
        ),
        ToolAnnotations(destructive_hint=True),
    ),
)


def register(mcp: MCPServer) -> None:
    for name, action, description, annotations in _CURATION:

        def make_curation_tool(action: Callable = action) -> Callable:
            def call(
                scope: Scope,
                memory_id: str,
                reason: str,
                ctx: Context,
                project_slug: str | None = None,
            ) -> ToolResult:
                with tool_session(ctx) as (principal, db):
                    request = CurationRequest(
                        scope=scope, project_slug=project_slug, memory_id=memory_id, reason=reason
                    )
                    return _result(action(db, principal, request, backend=get_backend()))

            return call

        mcp.tool(name=name, description=description, annotations=annotations)(make_curation_tool())

    @mcp.tool(
        description=(
            "Store one durable, independently-correctable claim -- for an explicit human "
            "'remember this' request or an agent's own well-verified conclusion. One claim "
            "per call, written in English whatever language the conversation uses: retrieval "
            "reranks in English only. `operation_id` is a UUID you generate; reuse it verbatim "
            "to retry the same claim safely. The response `memory_id` is the stable handle for "
            "forget, correct, restore, delete_memory and history. `wait=true` blocks until the "
            "claim is searchable; leave false to return immediately."
        ),
    )
    def retain(
        scope: Scope,
        content: str,
        memory_type: MemoryType,
        basis: Basis,
        operation_id: str,
        ctx: Context,
        project_slug: str | None = None,
        tags: list[str] = [],  # noqa: B006 -- never mutated, only read into a pydantic model
        wait: bool = False,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = RetainRequest(
                scope=scope,
                project_slug=project_slug,
                content=content,
                memory_type=memory_type,
                basis=basis,
                operation_id=operation_id,
                tags=tags,
                wait=wait,
            )
            return _result(do_retain(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Search memory and return bounded, grounded matching facts, most relevant first. "
            "Results below a relevance floor are withheld rather than returned as padding, so "
            "a query with no good answer returns nothing instead of a confident-looking list; "
            "each hit carries the `score` it was ranked by. Filter with `memory_types`/`basis`; "
            "scope='project' needs `project_slug`."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def recall(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        max_results: int = 10,
        memory_types: list[str] = [],  # noqa: B006
        basis: list[str] = [],  # noqa: B006
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = RecallRequest(
                scope=scope,
                project_slug=project_slug,
                query=query,
                max_results=max_results,
                memory_types=memory_types,
                basis=basis,
            )
            return _result(do_recall(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Ask memory a question and get one synthesized answer rather than a list of "
            "facts. Costs more than recall; only use it when a single answer is worth more "
            "than the underlying facts. Returns UNSUPPORTED if the backend has no synthesis "
            "capability."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def reflect(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = ReflectRequest(scope=scope, project_slug=project_slug, query=query)
            return _result(do_reflect(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Replace the text of a memory, keeping the same memory_id. The previous text is "
            "kept in history. `reason` is recorded in the journal."
        ),
    )
    def correct(
        scope: Scope,
        memory_id: str,
        content: str,
        reason: str,
        ctx: Context,
        project_slug: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = CorrectRequest(
                scope=scope, project_slug=project_slug, memory_id=memory_id, content=content, reason=reason
            )
            return _result(do_correct(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "List stored memories, most recent first. Filter by `memory_types`/`basis`, or "
            "`state` ('valid'/'invalid') to see retired memories too."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def list_memories(
        scope: Scope,
        ctx: Context,
        project_slug: str | None = None,
        memory_types: list[str] = [],  # noqa: B006
        basis: list[str] = [],  # noqa: B006
        state: Literal["valid", "invalid"] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = ListRequest(
                scope=scope,
                project_slug=project_slug,
                memory_types=memory_types,
                basis=basis,
                state=state,
                limit=limit,
                offset=offset,
            )
            return _result(do_list_memories(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description="Fetch one memory by id. Raises MEMORY_NOT_FOUND for an unknown id.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def get_memory(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        project_slug: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = GetRequest(scope=scope, project_slug=project_slug, memory_id=memory_id)
            return _result(do_get_memory(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Fetch a memory's journal: forget/restore/correct/delete events with their "
            "reasons, plus its current state if it still has one."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def history(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        project_slug: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = HistoryRequest(scope=scope, project_slug=project_slug, memory_id=memory_id)
            return _result(do_history(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Read this workspace's Working State checkpoint: where the work was left, not "
            "what is true. Empty if none was ever set."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def working_state_get(workspace_id: str, ctx: Context) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = WorkingStateRequest(workspace_id=workspace_id)
            return _result(do_ws_get(db, principal, request))

    @mcp.tool(
        description=(
            "Replace this workspace's Working State checkpoint in full. Ephemeral handoff "
            "state, never durable memory -- creates no memory or claim; use retain for "
            "anything that should outlive this session."
        ),
    )
    def working_state_put(workspace_id: str, state: dict[str, str], ctx: Context) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = PutWorkingStateRequest(workspace_id=workspace_id, state=state)
            return _result(do_ws_put(db, principal, request))

    @mcp.tool(description="Clear this workspace's Working State checkpoint. No-op if none exists.")
    def working_state_delete(workspace_id: str, ctx: Context) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            do_ws_delete(db, principal, WorkingStateRequest(workspace_id=workspace_id))
            return ToolResult(result={"workspace_id": workspace_id})

    @mcp.tool(
        description=(
            "Load the bounded standing context for this session: user memory always, plus "
            "project memory when `project_slug` names a known project. Creates nothing; call "
            "once at session start."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def load_context(ctx: Context, project_slug: str | None = None) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = LoadContextRequest(project_slug=project_slug)
            return _result(do_load_context(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Fetch one async engine operation by the `operation_ref` a pending retain "
            "returned. Returns UNSUPPORTED if the backend has no async operations capability."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def get_operation(
        scope: Scope,
        operation_ref: str,
        ctx: Context,
        project_slug: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            ref, _ = resolve_bank(db, principal, scope, project_slug, create=False)
            return ToolResult(result=get_backend().get_operation(ref.bank_id, operation_ref))
