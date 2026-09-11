"""The memory MCP tools of SPEC §11.

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
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from memory import (
    activity,
    curation_service,
    metrics,
    read_context,
    read_models,
    read_service,
    retention,
)
from memory.api.curation import CorrectRequest, ListMemoriesRequest
from memory.api.documents import ListDocumentsRequest
from memory.api.memory import (
    MAX_PAGE_SIZE,
    ScopedRequest,
    _check_content_size,
    _resolve_bank,
    _strip_bank_id,
)
from memory.api.operations import ListOperationsRequest
from memory.bootstrap import provision_before_retain
from memory.errors import DomainError, ProjectNotFound
from memory.hindsight.client import get_client
from memory.mcp.compact import compact as compact_payload
from memory.mcp.server import tool_session
from memory.mcp.tools import (
    ABSENT_PROJECT_STILL_RAISES,
    REGISTRY,
    MCPToolError,
    ToolResult,
    _internal_error,
    _invalid_request,
)
from memory.memory_types import EvidenceBasis, MemoryType, RetainTrigger
from memory.retained_records import get_by_document_id, get_by_source_memory_id
from memory.retention import submit_retain
from memory.sanitization import normalize_claim
from memory.tags import normalize_caller_tags
from memory.v040_contracts import RetainEvidence, TypedRetainRequest

logger = logging.getLogger("memory.mcp")

Scope = Literal["user", "project"]

MemoryState = Literal["valid", "invalidated"]
FactType = Literal["world", "experience", "observation"]
PageLimit = Annotated[int | None, Field(ge=1, le=MAX_PAGE_SIZE)]
PageOffset = Annotated[int | None, Field(ge=0)]

# The eight read tools carry this. Kept to one short sentence on purpose: a
# parameter's description ships in the tool listing of every context, so a
# verbose explanation here is a permanent cost paid to save an occasional one.
Verbose = Annotated[
    bool,
    Field(description="Return the full upstream payload instead of the reduced one."),
]

# What a list tool asks for when the caller named no limit. Hindsight's own
# default is 100 (`hindsight_api/api/http.py`), which spends an agent's context
# on two orders of magnitude more rows than a first look needs. The response
# carries `total`, `limit` and `offset`, so the rest stays one paged call away.
DEFAULT_PAGE_SIZE = 20


def _default_limit(limit: int | None, verbose: bool) -> int | None:
    """The page size a list tool actually asks Hindsight for.

    An explicit limit always wins, at any size the bound allows — this caps
    nothing the caller asked for, it only replaces "unspecified".
    """
    if limit is not None or verbose:
        return limit
    return DEFAULT_PAGE_SIZE


def _run(
    ctx: Context,
    body_factory,
    action: str,
    call,
    *,
    create: bool,
    is_write: bool = False,
    verbose: bool = True,
    empty_result: dict[str, Any] | object = ABSENT_PROJECT_STILL_RAISES,
    provision=None,
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
    strips operator authority from every principal it yields (invariant 22 —
    authority does not reside in an ordinary agent runtime, and MCP is exactly
    that), so `principal.is_master` is False for every principal `_run` ever
    sees, and `on_behalf_of` is always None for such a caller on REST too.
    There is no MCP equivalent of REST's `current_on_behalf_of` header
    dependency to wire, and none is needed unless a future task adds operator
    delegation over MCP on purpose.

    `verbose` defaults to True — no reduction — because that is what the seven
    write tools want: their responses are small envelopes with nothing to
    strip. The eight read tools forward the caller's own flag, and only they
    have a rule in `compact`.

    `is_write` forwards to `_resolve_bank`'s rate-limit gate (SPEC §20) — a
    caller cannot dodge the REST limit by switching to the MCP twin, since
    both funnel through the same `_resolve_bank` and the same per-credential
    `memory.ratelimit.Limiter`.

    `empty_result`, when given, is what a read tool returns instead of
    raising PROJECT_NOT_FOUND: an agent does not know whether today is its
    first day, and its first call is one of these, never retain, so an error
    here teaches it to stop calling (decision 3). Caught ONLY around this
    call's own resolution, never around `call` below -- by the time `call`
    runs the project is confirmed to exist, so a ProjectNotFound raised from
    inside it (retention.resolve_bank_ref's own, separate, always-create=False
    resolution) would be a genuine anomaly, not the absent-project case this
    parameter exists for.

    Only COLLECTION reads pass it (list_*, recall, reflect; load_context does
    the same in context_service). A get-by-id never does: nobody holds an id
    before something was listed or created, so decision 3's first-day case
    cannot reach it -- and softening it produced the one shape an agent could
    not branch on, a bare `{}` where the caller expects a record. Absence is
    therefore one rule everywhere: a collection read of nothing is an empty
    collection, a get of nothing is a *_NOT_FOUND error with a code, whether
    the missing thing is the id or the whole project. Write and curation
    tools never pass it either.
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
            try:
                bank_id, resolved_from, slug = _resolve_bank(
                    body,
                    tc.db,
                    tc.principal,
                    None,
                    action,
                    create=create,
                    is_write=is_write,
                )
            except ProjectNotFound:
                if empty_result is ABSENT_PROJECT_STILL_RAISES:
                    raise
                return ToolResult(result=dict(empty_result))
            # `provision`, when given, runs BEFORE the commit -- the REST twin
            # (`api/memory.py:_typed_retain`) and POST /v1/projects both order
            # it that way for the same reason: a caller must never be left
            # holding a committed project whose bank has no retain strategy
            # and no built-in.
            #
            # It protects the first half of provisioning, not all of it. A
            # failure in `ensure_exact_retain_strategy` propagates with the
            # Project row still uncommitted, so the slug is released and no
            # hourly creation is burnt. Built-in creation is different:
            # `mental_model_service._create_builtin` commits its `creating`
            # row before calling Hindsight so a crash cannot orphan an
            # upstream model, and that commit takes this session's pending
            # Project with it. Past that point the slug is taken for good
            # (invariant 8). See the longer note on the REST twin.
            if provision is not None:
                provision(bank_id, tc.db, tc.principal)
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
                # Compaction runs AFTER the redaction, never instead of it:
                # invariant 29 must not come to depend on which keys the rules
                # in `compact` happen to drop today (`chunk_id` is both the
                # first thing recall discards and the field a bank id hides
                # inside). `_strip_bank_id` has already rebuilt the structure,
                # so `compact` is free to mutate it.
                payload = _strip_bank_id(result, bank_id)
                if not verbose:
                    payload = compact_payload(action, payload)
                return ToolResult(
                    result=payload,
                    project_slug=slug,
                    resolved_from=resolved_from,
                    notice="PROJECT_RENAMED" if resolved_from else None,
                )
            except ValidationError as exc:
                logger.error("upstream response was not a JSON object", exc_info=exc)
                raise _internal_error() from None
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
        raise _invalid_request(exc) from None
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
        raise _internal_error() from None
    finally:
        activity.finish("mcp")


def _read_run(
    ctx: Context,
    body_factory,
    action: str,
    call,
    *,
    empty_result: dict[str, Any] | object = ABSENT_PROJECT_STILL_RAISES,
) -> ToolResult:
    """MCP pipeline for the genuinely read-only recall/history tools.

    `empty_result` has the same meaning and the same collection-only rule as
    on `_run`: recall passes it, memory_history (a get-by-id) does not.
    """
    activity.new_call()
    try:
        with tool_session(ctx) as tc:
            body = body_factory()
            try:
                resolved = read_context.resolve_read_bank(
                    tc.db,
                    tc.principal,
                    None,
                    action,
                    body.scope,
                    user_id=body.user_id,
                    project_slug=body.project_slug,
                )
            except ProjectNotFound:
                if empty_result is ABSENT_PROJECT_STILL_RAISES:
                    raise
                return ToolResult(result=dict(empty_result))
            tc.db.commit()
            result = call(resolved, tc.db, tc.principal, body)
            payload = result.model_dump() if isinstance(result, BaseModel) else result
            return ToolResult(
                result=payload,
                project_slug=resolved.current_slug,
                resolved_from=resolved.resolved_from,
                notice="PROJECT_RENAMED" if resolved.resolved_from else None,
            )
    except DomainError as exc:
        metrics.ERRORS.labels(code=exc.code).inc()
        activity.set_error(exc.code)
        raise MCPToolError(exc.code, exc.message, exc.details) from None
    except ValidationError as exc:
        metrics.ERRORS.labels(code="INVALID_REQUEST").inc()
        activity.set_error("INVALID_REQUEST")
        raise _invalid_request(exc) from None
    except Exception as exc:
        logger.error("unhandled MCP read tool error", exc_info=exc)
        metrics.ERRORS.labels(code="INTERNAL_ERROR").inc()
        activity.set_error("INTERNAL_ERROR")
        raise _internal_error() from None
    finally:
        activity.finish("mcp")


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Store one durable, independently-correctable claim plus its "
            "evidence -- for an explicit human 'remember this' request or "
            "an agent's own well-grounded observation. Evidence is bounded "
            "provenance (1-4 short excerpts); it is never stored as "
            "searchable memory itself. Write content in English whatever "
            "language the conversation is in: retrieval reranks in English "
            "only. `tags` are additive caller labels merged after the "
            "server-derived ones, which are never overridable; use "
            "`repo:<path>` to scope a claim to one repository in a "
            "project bank shared by many. Returns immediately with an "
            "operation you can follow with get_operation; use sync_retain "
            "when you need to read it back straight away."
        ),
    )
    def retain(
        scope: Scope,
        content: str,
        memory_type: MemoryType,
        basis: EvidenceBasis,
        trigger: RetainTrigger,
        evidence: list[RetainEvidence],
        ctx: Context,
        project_slug: str | None = None,
        valid_until: datetime | None = None,
        operation_id: str | None = None,
        tags: list[str] | None = None,
    ) -> ToolResult:
        return _retain(
            ctx,
            scope,
            content,
            memory_type,
            basis,
            trigger,
            evidence,
            project_slug,
            valid_until,
            operation_id,
            tags,
            wait=False,
        )

    @mcp.tool(
        description=(
            "Store one durable, independently-correctable claim plus its "
            "evidence, and wait until it is searchable. Same semantics as "
            "retain, `tags` included."
        ),
    )
    def sync_retain(
        scope: Scope,
        content: str,
        memory_type: MemoryType,
        basis: EvidenceBasis,
        trigger: RetainTrigger,
        evidence: list[RetainEvidence],
        ctx: Context,
        project_slug: str | None = None,
        valid_until: datetime | None = None,
        operation_id: str | None = None,
        tags: list[str] | None = None,
    ) -> ToolResult:
        # No idempotentHint: two calls with no operation_id write two
        # separate memories, same as retain -- this only blocks longer while
        # Hindsight makes the write searchable before returning. A true hint
        # here would invite an LLM client to retry blindly on a timeout and
        # duplicate the write.
        return _retain(
            ctx,
            scope,
            content,
            memory_type,
            basis,
            trigger,
            evidence,
            project_slug,
            valid_until,
            operation_id,
            tags,
            wait=True,
        )

    @mcp.tool(
        description=(
            "Search memory and return bounded, grounded matching facts. May "
            "also expire a bounded batch (at most 32) of claims already past "
            "their stated expiry as a side effect of this access. "
            "`tags_filter` narrows results to memories carrying ALL of the "
            "given tags, in addition to the server's own filters (e.g. "
            "`repo:<path>` to search one repository in a project bank "
            "shared by many). Results below a relevance floor are withheld "
            "rather than returned as padding, so a query with no good answer "
            "returns nothing instead of a confident-looking list; each hit "
            "carries the `score` it was ranked by. The floor is server-owned "
            "and cannot be lowered per request."
        ),
        # readOnlyHint=False: run_access_maintenance below can claim expiry
        # work and commit database changes -- a client that skips
        # confirmation for "read-only" tools must not skip it here.
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def recall(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        verbose: Verbose = False,
        view: read_models.View = "current",
        kinds: list[MemoryType] | None = None,
        max_results: int = read_models.DEFAULT_MAX_RESULTS,
        tags_filter: list[str] | None = None,
    ) -> ToolResult:
        def body_factory() -> read_models.RecallRequest:
            _check_content_size(query)
            return read_models.RecallRequest(
                scope=scope,
                project_slug=project_slug,
                query=query,
                view=view,
                kinds=tuple(kinds) if kinds else None,
                max_results=max_results,
                tags_filter=tags_filter,
            )

        def call(resolved, db, principal, body):
            recall_bank_ref = read_service.bank_ref(principal, resolved)
            read_service.ensure_current_read_allowed(db, recall_bank_ref)
            read_service.run_access_maintenance(db, recall_bank_ref)
            hits = read_service._recall_hits(
                resolved.bank_id,
                body.query,
                body.view,
                body.kinds,
                body.tags_filter,
            )
            return read_models.build_recall_response(
                project_slug=resolved.current_slug,
                resolved_from=resolved.resolved_from,
                hits=hits[: body.max_results],
            )

        return _read_run(
            ctx,
            body_factory,
            "read.recall",
            call,
            empty_result={"hits": [], "truncated": False},
        )

    @mcp.tool(
        description=(
            "Fetch bounded history and rationale for a recalled memory."
            " Nothing there is an error with a code, never an empty result: MEMORY_NOT_FOUND for an unknown id, PROJECT_NOT_FOUND for an unknown project."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def memory_history(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        project_slug: str | None = None,
    ) -> ToolResult:
        return _read_run(
            ctx,
            lambda: read_models.HistoryRequest(
                scope=scope, project_slug=project_slug, memory_id=memory_id
            ),
            "read.history",
            lambda _resolved, db, principal, body: read_service.history(db, principal, None, body),
        )

    @mcp.tool(
        description=(
            "Ask memory a question and get a synthesized answer rather than "
            "a list of facts. Costs more than recall. May also expire a "
            "bounded batch (at most 32) of claims already past their stated "
            "expiry as a side effect of this access. `tags_filter` narrows "
            "the answer to memories carrying ALL of the given tags, same "
            "convention as recall. The answer comes with `based_on.memories`, "
            "the stored facts it was grounded on, so a claim in the prose can "
            "be traced to a memory or seen to have none."
        ),
        # Reflect still spends LLM tokens and keeps confirmation/rate limiting.
        # readOnlyHint=False, explicit rather than relying on the SDK's
        # unannotated default: run_access_maintenance below can commit
        # database changes, same as recall.
        annotations=ToolAnnotations(readOnlyHint=False),
    )
    def reflect(
        scope: Scope,
        query: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        verbose: Verbose = False,
        tags_filter: list[str] | None = None,
    ) -> ToolResult:
        def body_factory() -> ScopedRequest:
            body = ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator)
            # reflect spends model tokens on a server-level credential with no
            # per-user cost attribution (SPEC §19.4) -- the same cap REST's
            # _check_content_size(body.query) already applies, mirrored here.
            _check_content_size(query)
            return body

        def call(bank, db, principal, slug):
            reflect_bank_ref = retention.resolve_bank_ref(db, principal, body_factory())
            read_service.ensure_current_read_allowed(db, reflect_bank_ref)
            read_service.run_access_maintenance(db, reflect_bank_ref)
            # Normalised here (inside _run's DomainError-mapping try), not at
            # the top of `reflect`: an InvalidTag raised before _run starts
            # would escape the MCPToolError conversion every other error goes
            # through.
            normalized = normalize_caller_tags(tags_filter)
            # Same server-owned scoping as recall, from the same function.
            reflect_filters = read_models.resolve_filters("current", None, normalized)
            return read_service.whitelist_reflect_evidence(
                get_client().reflect(
                    bank,
                    query,
                    tag_groups=list(reflect_filters.tag_groups),
                    fact_types=list(reflect_filters.types),
                    include_facts=True,
                )
            )

        return _run(
            ctx,
            body_factory,
            "memory.reflect",
            call,
            create=False,
            is_write=True,
            verbose=verbose,
            empty_result={"text": "", "usage": {}},
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
        verbose: Verbose = False,
    ) -> ToolResult:
        # A caller that named no limit gets DEFAULT_PAGE_SIZE instead of
        # Hindsight's 100. Only in the reduced shape: `verbose` keeps the old
        # behaviour whole, upstream default included.
        page = _default_limit(limit, verbose)

        def body_factory() -> ListMemoriesRequest:
            # Reuses ListMemoriesRequest itself (the REST model) rather than a
            # bare ScopedRequest -- the same fix _retain already got for
            # RetainRequest. A bare ScopedRequest here dropped `state`'s
            # Literal["valid","invalidated"] bound and both `Field(ge=0)`
            # bounds, so a bogus state or a negative limit reached Hindsight
            # as a 502 blaming the backend instead of a typed rejection at
            # the boundary.
            body = ListMemoriesRequest(
                scope=scope,
                project_slug=project_slug,
                git_locator=git_locator,
                q=q,
                type=type,
                state=state,
                document_id=document_id,
                limit=page,
                offset=offset,
            )
            # q is a caller-authored search query, same embedding-spend risk
            # class as recall's query; optional, so guarded.
            if q is not None:
                _check_content_size(q)
            return body

        def call(bank, db, principal, slug):
            read_service.ensure_current_read_allowed(
                db, retention.resolve_bank_ref(db, principal, body_factory())
            )
            return get_client().list_memories(
                bank,
                q=q,
                type=type,
                state=state,
                document_id=document_id,
                limit=page,
                offset=offset,
            )

        return _run(
            ctx,
            body_factory,
            "memory.list",
            call,
            create=False,
            verbose=verbose,
            empty_result={"items": []},
        )

    @mcp.tool(
        description=(
            "Fetch one memory by id."
            " Nothing there is an error with a code, never an empty result: MEMORY_NOT_FOUND for an unknown id, PROJECT_NOT_FOUND for an unknown project."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_memory(
        scope: Scope,
        memory_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        verbose: Verbose = False,
    ) -> ToolResult:
        def body_factory() -> ScopedRequest:
            return ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator)

        def call(bank, db, principal, slug):
            read_service.ensure_current_read_allowed(
                db, retention.resolve_bank_ref(db, principal, body_factory())
            )
            return get_client().get_memory(bank, memory_id)

        return _run(
            ctx,
            body_factory,
            "memory.get",
            call,
            create=False,
            verbose=verbose,
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
            body = ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator)
            # reason is caller free text forwarded verbatim to Hindsight;
            # optional, so guarded like the UPDATE routes' `if x is not None`.
            if reason is not None:
                _check_content_size(reason)
            return body

        def call(bank, db, principal, slug):
            bank_ref = retention.resolve_bank_ref(db, principal, body_factory())
            read_service.ensure_current_read_allowed(db, bank_ref)
            retained = get_by_source_memory_id(db, bank_ref, memory_id)
            if retained is not None:
                curation_service.forget_record(
                    db, retained, reason=reason, client=get_client(), bank_id=bank
                )
                return {"id": memory_id, "state": "invalidated"}
            return get_client().curate(bank, memory_id, state="invalidated", reason=reason)

        return _run(ctx, body_factory, "memory.forget", call, create=False, is_write=True)

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
        operation_id: str | None = None,
    ) -> ToolResult:
        accepted_operation_id: str | None = None

        def body_factory() -> CorrectRequest:
            nonlocal accepted_operation_id
            # Reuses CorrectRequest itself rather than a bare ScopedRequest --
            # the same fix _retain already got for RetainRequest. A bare
            # ScopedRequest here dropped `content`'s min_length=1/_not_blank
            # bound, so a blank correct on a valid memory reached Hindsight and
            # came back as 409 MEMORY_NOT_CURATABLE -- telling the caller the
            # memory is a derived observation when it simply sent nothing
            # (review finding I5, reopened as F1). Canonicalization (secret
            # rejection, the 4096-byte claim boundary) happens in `call`,
            # AFTER bank resolution -- not here -- so it runs only once
            # authorization has already cleared (see `call`'s comment).
            accepted_operation_id = operation_id or str(uuid.uuid4())
            return CorrectRequest(
                scope=scope,
                project_slug=project_slug,
                git_locator=git_locator,
                memory_id=memory_id,
                content=content,
                operation_id=accepted_operation_id,
            )

        def call(bank, db, principal, slug):
            bank_ref = retention.resolve_bank_ref(db, principal, body_factory())
            read_service.ensure_current_read_allowed(db, bank_ref)
            retained = get_by_source_memory_id(db, bank_ref, memory_id)
            if retained is not None:
                assert accepted_operation_id is not None
                curation_service.correct_record(
                    db,
                    retained,
                    content,
                    operation_id=accepted_operation_id,
                    client=get_client(),
                    bank_id=bank,
                )
                # Canonical text `correct_record` actually stored, never the
                # caller's raw input.
                return {"id": memory_id, "text": retained.canonical_content}
            # Untracked legacy fallback: no `curation_service` call in this
            # branch, so canonicalization happens here, right before the one
            # upstream call, same as the REST twin.
            canonical_content = normalize_claim(content)
            return get_client().curate(bank, memory_id, text=canonical_content)

        return _run(ctx, body_factory, "memory.correct", call, create=False, is_write=True)

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
        def body_factory() -> ScopedRequest:
            return ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator)

        def call(bank, db, principal, slug):
            bank_ref = retention.resolve_bank_ref(db, principal, body_factory())
            read_service.ensure_current_read_allowed(db, bank_ref)
            retained = get_by_source_memory_id(db, bank_ref, memory_id)
            if retained is not None:
                curation_service.restore_record(db, retained, client=get_client(), bank_id=bank)
                return {"id": memory_id, "state": "valid"}
            return get_client().curate(bank, memory_id, state="valid")

        return _run(ctx, body_factory, "memory.restore", call, create=False, is_write=True)

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
        verbose: Verbose = False,
    ) -> ToolResult:
        return _list_documents(ctx, scope, project_slug, git_locator, q, limit, offset, verbose)

    @mcp.tool(
        description=(
            "Fetch one document by its id."
            " Nothing there is an error with a code, never an empty result: DOCUMENT_NOT_FOUND for an unknown id, PROJECT_NOT_FOUND for an unknown project."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_document(
        scope: Scope,
        document_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        verbose: Verbose = False,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator),
            "memory.documents.get",
            lambda bank, db, p, slug: get_client().get_document(bank, document_id),
            create=False,
            verbose=verbose,
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
        def body_factory() -> ScopedRequest:
            return ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator)

        def call(bank, db, principal, slug):
            bank_ref = retention.resolve_bank_ref(db, principal, body_factory())
            read_service.ensure_current_read_allowed(db, bank_ref)
            retained = get_by_document_id(db, bank_ref, document_id)
            if retained is not None:
                curation_service.delete_record(db, retained, client=get_client(), bank_id=bank)
                return {"deleted": True}
            return get_client().delete_document(bank, document_id)

        return _run(ctx, body_factory, "memory.documents.delete", call, create=False, is_write=True)

    @mcp.tool(
        description=(
            "Check whether an async retain has finished."
            " Nothing there is an error with a code, never an empty result: OPERATION_NOT_FOUND for an unknown id, PROJECT_NOT_FOUND for an unknown project."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def get_operation(
        scope: Scope,
        operation_id: str,
        ctx: Context,
        project_slug: str | None = None,
        git_locator: str | None = None,
        verbose: Verbose = False,
    ) -> ToolResult:
        return _run(
            ctx,
            lambda: ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator),
            "memory.operations.get",
            lambda bank, db, p, slug: get_client().get_operation(bank, operation_id),
            create=False,
            verbose=verbose,
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
        verbose: Verbose = False,
    ) -> ToolResult:
        return _list_operations(
            ctx,
            scope,
            project_slug,
            git_locator,
            status,
            type,
            limit,
            offset,
            verbose,
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
            lambda: ScopedRequest(scope=scope, project_slug=project_slug, git_locator=git_locator),
            "memory.operations.cancel",
            lambda bank, db, p, slug: get_client().cancel_operation(bank, operation_id),
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
        memory_history=memory_history,
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


def _retain(
    ctx,
    scope,
    content,
    memory_type,
    basis,
    trigger,
    evidence,
    project_slug,
    valid_until,
    operation_id,
    tags,
    *,
    wait: bool,
) -> ToolResult:
    # Generated before the first network attempt (SPEC §6.1) and reused for
    # every retry this call makes -- a direct REST client must supply its own.
    op_id = operation_id or str(uuid.uuid4())

    def body_factory() -> TypedRetainRequest:
        return TypedRetainRequest(
            scope=scope,
            project_slug=project_slug,
            content=content,
            memory_type=memory_type,
            basis=basis,
            trigger=trigger,
            valid_until=valid_until,
            evidence=tuple(evidence),
            operation_id=op_id,
            tags=tags,
        )

    def provision(bank_id, db, principal):
        provision_before_retain(db, principal, scope=scope, bank_id=bank_id, client=get_client())

    def call(bank_id, db, principal, slug):
        return submit_retain(
            db,
            principal,
            body_factory(),
            client=get_client(),
            wait=wait,
        ).model_dump(mode="json")

    # create=True: retain is the one place allowed to mint an unknown
    # project (lazy-provisioning plan, decision 1), guarded by
    # projects.create's own per-user hourly limit. Every other v0.4.0 retain
    # surface stays existing-only.
    return _run(
        ctx,
        body_factory,
        "memory.retain",
        call,
        create=True,
        is_write=True,
        provision=provision,
    )


def _list_documents(
    ctx, scope, project_slug, git_locator, q, limit, offset, verbose=False
) -> ToolResult:
    page = _default_limit(limit, verbose)

    def body_factory() -> ListDocumentsRequest:
        # Reuses ListDocumentsRequest itself for its Field(ge=0) bound on
        # limit/offset -- the same fix _retain already got for RetainRequest.
        # A bare ScopedRequest here let a negative value reach Hindsight as a
        # 502 blaming the backend instead of a typed rejection at the
        # boundary. Unset fields are OMITTED, not passed as None: the model's
        # own concrete defaults (100/0) are for validation only here. In the
        # reduced shape `page` supplies DEFAULT_PAGE_SIZE, so what goes on the
        # wire is 20 rather than Hindsight's 100; under `verbose` it stays
        # None and the old behavior of sending nothing is preserved whole.
        kwargs: dict[str, Any] = {
            "scope": scope,
            "project_slug": project_slug,
            "git_locator": git_locator,
            "q": q,
        }
        if page is not None:
            kwargs["limit"] = page
        if offset is not None:
            kwargs["offset"] = offset
        body = ListDocumentsRequest(**kwargs)
        # q is a caller-authored search query, same embedding-spend risk
        # class as recall's query; optional, so guarded.
        if q is not None:
            _check_content_size(q)
        return body

    def call(bank_id, db, principal, slug):
        return get_client().list_documents(bank_id, q=q, limit=page, offset=offset)

    return _run(
        ctx,
        body_factory,
        "memory.documents.list",
        call,
        create=False,
        verbose=verbose,
        empty_result={"items": []},
    )


def _list_operations(
    ctx, scope, project_slug, git_locator, status, type, limit, offset, verbose=False
) -> ToolResult:
    page = _default_limit(limit, verbose)

    def body_factory() -> ListOperationsRequest:
        # Same reasoning as _list_documents above, against ListOperationsRequest.
        kwargs: dict[str, Any] = {
            "scope": scope,
            "project_slug": project_slug,
            "git_locator": git_locator,
            "status": status,
            "type": type,
        }
        if page is not None:
            kwargs["limit"] = page
        if offset is not None:
            kwargs["offset"] = offset
        return ListOperationsRequest(**kwargs)

    def call(bank_id, db, principal, slug):
        return get_client().list_operations(
            bank_id, status=status, type=type, limit=page, offset=offset
        )

    return _run(
        ctx,
        body_factory,
        "memory.operations.list",
        call,
        create=False,
        verbose=verbose,
        empty_result={"items": []},
    )
