"""MCP tool contracts and registration (SPEC §6): thin wrappers over the service layer.

Every tool authenticates, opens one DB session, builds a service request from
its arguments, calls the service, and wraps the response. `tool_session`
centralizes the first two steps and the error mapping so no tool body repeats
them.
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ValidationError, model_serializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from memory import audit, catalog, projects
from memory import context as standing_context
from memory import families as family_list
from memory.auth.principal import Principal, authenticate
from memory.backend import get_backend
from memory.bank_ref import resolve_bank
from memory.context import LoadContextRequest
from memory.context import load as do_load_context
from memory.curation import CorrectRequest, CurationRequest, PinRequest
from memory.curation import correct as do_correct
from memory.curation import delete as do_delete
from memory.curation import forget as do_forget
from memory.curation import pin as do_pin
from memory.curation import restore as do_restore
from memory.db import session_scope
from memory.errors import DomainError
from memory.models import Memory, Project, ProjectSlug
from memory.read import GetRequest, HistoryRequest, ListRequest, RecallRequest, ReflectRequest
from memory.read import get_memory as do_get_memory
from memory.read import history as do_history
from memory.read import list_memories as do_list_memories
from memory.read import recall as do_recall
from memory.read import reflect as do_reflect
from memory.retain import RetainRequest
from memory.retain import submit as do_retain
from memory.slugs import normalize_slug
from memory.tags import Basis, MemoryType

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


def _read(ctx: Context, tool: str, call: Callable[[Principal, Session], BaseModel], *,
          scope: str | None, project_slug: str | None, results: Callable[[BaseModel], int],
          query: str | None = None, family: str | None = None,
          relevance: Callable[[BaseModel], float | None] = lambda r: None,
          session_id: str | None = None) -> ToolResult:
    """Run one read and log it (`read_events`): who, project, tool, query or family, how many
    results, how long. Failed reads are not logged."""
    started = time.perf_counter()
    with tool_session(ctx) as (principal, db):
        response = call(principal, db)
        audit.log_read(db, principal=principal, tool=tool, scope=scope, project_slug=project_slug,
                       query=query, family=family, results=results(response), relevance=relevance(response),
                       latency_ms=round((time.perf_counter() - started) * 1000), session_id=session_id)
        return _result(response)


#: A listed project's purpose is cut to about its first sentence.
LISTED_PURPOSE_CHARS = 200


class ProjectSummary(BaseModel):
    project_slug: str
    purpose: str | None
    memories: int


class ProjectsResponse(BaseModel):
    items: list[ProjectSummary]


def list_readable_projects(db: Session, principal: Principal) -> ProjectsResponse:
    """Every project the caller may read, by slug, with its purpose and valid memory count.
    ponytail: one engine read per project for the purpose; cache it if the list grows past ~100."""
    counts = dict(db.execute(select(Memory.owner_id, func.count()).where(Memory.state == "valid")
                             .group_by(Memory.owner_id)).all())
    slugs = dict(db.execute(select(ProjectSlug.project_internal_id, ProjectSlug.slug)
                            .where(ProjectSlug.is_canonical.is_(True))).all())
    backend = get_backend()
    items = []
    for project in db.scalars(select(Project)):
        if not projects.authorized(principal, project) or project.internal_id not in slugs:
            continue
        try:
            purpose = standing_context.read_purpose(backend, project.bank_id)
        except DomainError:  # one unreadable bank must not hide the rest of the list
            purpose = None
        items.append(ProjectSummary(
            project_slug=slugs[project.internal_id],
            purpose=standing_context.shorten(purpose, LISTED_PURPOSE_CHARS) if purpose else None,
            memories=counts.get(project.internal_id, 0)))
    return ProjectsResponse(items=sorted(items, key=lambda item: item.project_slug))


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
                memory_id: str,
                reason: str,
                ctx: Context,
                scope: Scope | None = None,
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
            "reranks in English only. `title`: one line of at most 80 characters naming what the "
            "claim is about (component, command, setting), the way session start lists it. "
            "`family`: the project area it belongs to; once the project has a family list it is "
            "required and must be on it (an unknown one is refused with the list). `pin=true` "
            "delivers it in full at every session start: only for what every session needs; a pin "
            "past the bank's cap is refused with what is pinned. `operation_id` is a UUID you "
            "generate; reuse it verbatim to retry the same claim safely. The response `memory_id` "
            "is the stable handle for forget, correct, restore, delete_memory, pin, get_memory "
            "and history, which infer `scope` from it when you leave it out. `wait=true` blocks "
            "until the claim is searchable; leave false to return immediately. `similar` lists up "
            "to 3 other memories closest to the new one, unfiltered: if one is the same claim, is "
            "replaced by the new one, or covers the same subject with less detail, keep one (the "
            "ach-memory skill says how) without asking the user."
        ),
    )
    def retain(
        scope: Scope,
        title: str,
        content: str,
        memory_type: MemoryType,
        basis: Basis,
        operation_id: str,
        ctx: Context,
        family: str | None = None,
        pin: bool = False,
        project_slug: str | None = None,
        tags: list[str] = [],  # noqa: B006 -- never mutated, only read into a pydantic model
        wait: bool = False,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = RetainRequest(
                scope=scope,
                project_slug=project_slug,
                title=title,
                family=family,
                pin=pin,
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
            "Search memory and return matching facts, most relevant first. The first hits carry "
            "`relevance`, the probability that each helps answer the query; when even the best "
            "is low, `note` says so first: then the hits are only the nearest memories, not an "
            "answer, so check one before relying on it. Filter with `memory_types`/`basis`. "
            "scope='all' searches your own memories and the project's together -- use it when "
            "you do not know which of the two holds the answer; scope='project' needs "
            "`project_slug`. Query in English whatever language the conversation uses: it finds "
            "the right memory more often than a Spanish one."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def recall(
        scope: Literal["user", "project", "all"],
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        max_results: int = 10,
        memory_types: list[str] = [],  # noqa: B006
        basis: list[str] = [],  # noqa: B006
        session_id: str | None = None,
    ) -> ToolResult:
        request = RecallRequest(
            scope=scope,
            project_slug=project_slug,
            query=query,
            max_results=max_results,
            memory_types=memory_types,
            basis=basis,
        )
        return _read(ctx, "recall", lambda p, db: do_recall(db, p, request, backend=get_backend()),
                     scope=scope, project_slug=project_slug, query=query, session_id=session_id,
                     results=lambda r: r.total,
                     relevance=lambda r: max((h.relevance for h in r.items if h.relevance is not None), default=None))

    @mcp.tool(
        description=(
            "Ask memory a question and get one synthesized answer rather than a list of "
            "facts. Costs more than recall; only use it when a single answer is worth more "
            "than the underlying facts. Narrow it with `memory_types`/`basis`, same filter "
            "as recall. Returns UNSUPPORTED if the backend has no synthesis "
            "capability."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def reflect(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        memory_types: list[str] = [],  # noqa: B006
        basis: list[str] = [],  # noqa: B006
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = ReflectRequest(
                scope=scope,
                project_slug=project_slug,
                query=query,
                memory_types=memory_types,
                basis=basis,
            )
            return _result(do_reflect(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Replace the text of a memory, keeping the same memory_id. The previous text is "
            "kept in history. `reason` is recorded in the journal. Pass `title` too when the "
            "old one no longer fits the new text: session start lists memories by title."
        ),
    )
    def correct(
        memory_id: str,
        content: str,
        reason: str,
        ctx: Context,
        scope: Scope | None = None,
        project_slug: str | None = None,
        title: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = CorrectRequest(
                scope=scope, project_slug=project_slug, memory_id=memory_id, content=content, reason=reason,
                title=title,
            )
            return _result(do_correct(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "List stored memories, most recent first: titles and ids, the text only with "
            "`full=true`. Filter by `family`, `memory_types`, `pinned`, or `state` "
            "('valid'/'invalid') to see retired memories."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def list_memories(
        scope: Scope,
        ctx: Context,
        project_slug: str | None = None,
        family: str | None = None,
        memory_types: list[str] = [],  # noqa: B006
        pinned: bool | None = None,
        state: Literal["valid", "invalid"] | None = None,
        full: bool = False,
        limit: int = 20,
        offset: int = 0,
        session_id: str | None = None,
    ) -> ToolResult:
        request = ListRequest(
            scope=scope,
            project_slug=project_slug,
            family=family,
            memory_types=memory_types,
            pinned=pinned,
            state=state,
            full=full,
            limit=limit,
            offset=offset,
        )
        return _read(ctx, "list_memories",
                     lambda p, db: do_list_memories(db, p, request, backend=get_backend()),
                     scope=scope, project_slug=project_slug, family=family, session_id=session_id,
                     results=lambda r: len(r.items))

    @mcp.tool(
        description="Fetch one memory by id. Raises MEMORY_NOT_FOUND for an unknown id.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def get_memory(
        memory_id: str,
        ctx: Context,
        scope: Scope | None = None,
        project_slug: str | None = None,
        session_id: str | None = None,
    ) -> ToolResult:
        request = GetRequest(scope=scope, project_slug=project_slug, memory_id=memory_id)
        return _read(ctx, "get_memory", lambda p, db: do_get_memory(db, p, request, backend=get_backend()),
                     scope=scope, project_slug=project_slug, query=memory_id, session_id=session_id,
                     results=lambda r: 1)

    @mcp.tool(
        description=(
            "Fetch a memory's journal: forget/restore/correct/delete events with their "
            "reasons, plus its current state if it still has one."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def history(
        memory_id: str,
        ctx: Context,
        scope: Scope | None = None,
        project_slug: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = HistoryRequest(scope=scope, project_slug=project_slug, memory_id=memory_id)
            return _result(do_history(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Load this session's standing context again: the project's purpose, your pinned "
            "memories and the project's in full, and an index of the project's memory by family. "
            "Creates nothing."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def load_context(ctx: Context, project_slug: str | None = None, session_id: str | None = None) -> ToolResult:
        request = LoadContextRequest(project_slug=project_slug)
        return _read(ctx, "load_context", lambda p, db: do_load_context(db, p, request, backend=get_backend()),
                     scope=None, project_slug=project_slug, session_id=session_id, results=lambda r: r.shown)

    @mcp.tool(
        description=(
            "Pin a memory so every session start delivers it in full, or unpin it with "
            "`pinned=false`. Pin only what every session needs before doing anything. A pin past "
            "the bank's cap is refused with the list of what is pinned: unpin or shorten one."
        ),
    )
    def pin(
        memory_id: str,
        ctx: Context,
        pinned: bool = True,
        reason: str | None = None,
        scope: Scope | None = None,
        project_slug: str | None = None,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            request = PinRequest(scope=scope, project_slug=project_slug, memory_id=memory_id,
                                 pinned=pinned, reason=reason)
            return _result(do_pin(db, principal, request, backend=get_backend()))

    @mcp.tool(
        description=(
            "Show the project's family list with how many valid memories each holds. Without a "
            "list yet, families are optional; one is drafted when the project first holds "
            f"{family_list.THRESHOLD} memories."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def list_families(project_slug: str, ctx: Context) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            project = projects.resolve(db, principal, project_slug, create=False).project
            counts: dict[str, int] = {}
            for row in catalog.valid(db, project.internal_id):
                counts[row.family or family_list.OTHER] = counts.get(row.family or family_list.OTHER, 0) + 1
            listed = project.families or []
            return ToolResult(result={
                "families": [{"family": f, "memories": counts.get(f, 0)} for f in listed],
                "unlisted": {f: n for f, n in counts.items() if f not in listed},
                "has_list": project.families is not None,
            })

    @mcp.tool(
        description=(
            "Replace the project's family list (at most 12, plus 'other', which is always kept). "
            "`rename` maps an old family to a new one and moves its memories; memories in a "
            "family that leaves the list go to 'other'. Owner or operator only. For people "
            "through the CLI (`ach-memory families set`): do not call it unless asked to."
        ),
    )
    def set_families(
        project_slug: str,
        families: list[str],
        ctx: Context,
        rename: dict[str, str] = {},  # noqa: B006 -- never mutated
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            project = projects.resolve(db, principal, project_slug, create=False).project
            return ToolResult(result={"families": family_list.replace(db, principal, project, families, rename)})

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

    @mcp.tool(
        description=(
            "List the projects you can read: slug, purpose and how many memories each holds. "
            "Use it where no project is set (a folder with no git remote and no `.ach-memory` "
            "marker, or a host with no folder) to find the project to name, and before starting "
            "a new project, so you offer an existing one instead of a near-duplicate."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    def list_projects(ctx: Context, session_id: str | None = None) -> ToolResult:
        return _read(ctx, "list_projects", lambda p, db: list_readable_projects(db, p),
                     scope=None, project_slug=None, session_id=session_id, results=lambda r: len(r.items))

    @mcp.tool(
        description=(
            "Point a project's memory at a new slug after its git remote moved or was "
            "renamed. Without this a moved repository resolves to a brand-new empty "
            "project and its memory is silently orphaned. The old slug keeps resolving "
            "to the same memory (a retain through it returns a PROJECT_RENAMED notice), "
            "so nothing breaks mid-flight and renaming back undoes it. Only the "
            "project's owner (or an operator) may call it."
        ),
    )
    def rename_project(
        project_slug: str,
        new_slug: str,
        ctx: Context,
    ) -> ToolResult:
        with tool_session(ctx) as (principal, db):
            target = normalize_slug(new_slug)
            resolved = projects.resolve(db, principal, project_slug, create=False)
            retired = projects.rename(db, principal, resolved.project, target)
            audit.record(
                db, principal=principal, action="project.rename",
                resource=resolved.project.internal_id, operation_id=str(uuid4()),
                details={"from": retired, "to": target},
            )
            return ToolResult(result={"project_slug": target, "previous_slug": retired})
