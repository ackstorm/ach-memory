import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)
from sqlalchemy.orm import Session

from memory import activity, audit, ratelimit, read_context, read_models, read_service
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.common import RenameForwarding
from memory.auth.principal import Principal
from memory.banks import resolve_project_bank, resolve_user_bank
from memory.config import get_settings
from memory.db import get_session
from memory.errors import ContentTooLarge
from memory.hindsight.client import get_client
from memory.identifiers import has_control_character
from memory.retention import submit_retain
from memory.v040_contracts import TypedRetainRequest, TypedRetainResponse

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _must_be_uuid(value: str) -> str:
    uuid.UUID(value)
    return value


Scope = Literal["user", "project"]

# One ceiling for every paginated route. `admin.list_audit` chose le=500 and
# was the ONLY route that bounded the high side; the other five bounded only
# ge=0, so `limit=10**20` reached Hindsight and came back as a 502 blaming the
# backend for the caller's typo -- the exact outcome curation.py's own comment
# claimed to prevent.
MAX_PAGE_SIZE = 500

# A UUID in any form uuid.UUID() accepts. Not `pydantic.UUID4`, which would
# coerce the value to a UUID object and re-serialize it in canonical form —
# SPEC §15 says the caller's id is passed through verbatim. Reused by
# api/mental_models.py's own operation_id fields, not just this module's
# (now-typed) retain contract -- kept here rather than removed when retain
# stopped needing it directly, since it is still a real cross-module utility.
UUID4Str = Annotated[str, AfterValidator(_must_be_uuid)]


class ScopedRequest(BaseModel):
    """Everything `_resolve_bank` needs, and nothing else.

    Every data-plane request carries this. `user_id` is meaningful only under
    scope=user (a master key naming its target); it is ignored under
    scope=project, where the project slug selects the bank (pinned by
    test_user_id_is_ignored_under_project_scope).

    extra="forbid" on the BASE, so every data-plane and §14 model inherits it:
    a typoed field used to validate cleanly with the real field left None, so
    a PATCH sent an empty body upstream and answered 200 having changed
    nothing -- Plan 6's finding I1, which was fixed on the project models and
    on no others.
    """

    model_config = ConfigDict(extra="forbid")

    scope: Scope
    user_id: str | None = None
    project_slug: str | None = None
    # Bounded to match the projects.git_locator column (String(512)) so an
    # oversize value is a typed 422 at the boundary, not a 500 from the DB.
    git_locator: str | None = Field(default=None, max_length=512)

    @field_validator("user_id")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        # Reaches `db.get(User, ...)` in banks.resolve_user_bank. A control
        # character there is a psycopg DataError -> 500; a typed 422 at the
        # boundary is the same treatment git_locator's max_length gets.
        if value and has_control_character(value):
            raise ValueError("user_id must not contain control characters")
        return value

    @field_validator("git_locator")
    @classmethod
    def _git_locator_no_control_characters(cls, value: str | None) -> str | None:
        # Reaches the projects INSERT in projects.py. A control character
        # there is a psycopg DataError -> 500, same as user_id above.
        if value and has_control_character(value):
            raise ValueError("git_locator must not contain control characters")
        return value


def scoped_query_params(
    scope: Scope,
    # pattern: FastAPI enforces this pre-route as a 422, the same shape
    # current_on_behalf_of's header already uses (memory/api/app.py). Without
    # it, a control character reaches ScopedRequest's own validator INSIDE
    # this function body -- not the route -- so pydantic's ValidationError
    # escapes as a 500 from api/app.py's catch-all instead of FastAPI's 422.
    user_id: Annotated[str | None, Query(pattern=r"^[^\x00-\x1f\x7f]*$")] = None,
    project_slug: str | None = None,
    git_locator: Annotated[
        str | None, Query(max_length=512, pattern=r"^[^\x00-\x1f\x7f]*$")
    ] = None,
) -> ScopedRequest:
    """`ScopedRequest`, sourced from the query string instead of a JSON body.

    Directives and mental models (SPEC §14) are REST-only resources exposed
    over real HTTP verbs (`GET`/`DELETE` on `/v1/directives/{id}`, unlike the
    all-POST data plane in this module which mirrors MCP tool call shapes) --
    a `GET`/`DELETE` carries no body, so the same four scope-resolution
    fields have to arrive as query params for those routes instead.
    """
    return ScopedRequest(
        scope=scope,
        user_id=user_id,
        project_slug=project_slug,
        git_locator=git_locator,
    )


class RecallRequest(ScopedRequest):
    query: str


class MemoryResponse(RenameForwarding):
    result: dict[str, Any]
    # Mirrors memory.api.projects.ProjectResponse.project_slug (SPEC §8.6):
    # the project's current slug, set for scope=project so a caller who
    # followed a rename tombstone (resolved_from set) can update its config
    # without a second GET /v1/projects/{slug}. Always None for scope=user.
    project_slug: str | None = None


def _check_content_size(content: str) -> None:
    """Shared by REST and MCP: `CONTENT_TOO_LARGE` is in SPEC §18's closed
    list, so both surfaces must be able to produce it -- a check that lived
    only in the REST handler let an oversize body sail through MCP straight
    to Hindsight.
    """
    limit = get_settings().max_content_bytes
    if len(content.encode("utf-8")) > limit:
        raise ContentTooLarge(f"content exceeds {limit} bytes")


def _describe(
    action: str,
    scope: str,
    principal: Principal,
    bank_id: str,
    *,
    body: ScopedRequest,
    user_id: str | None = None,
    project_slug: str | None = None,
) -> None:
    """Fill in the activity record for this call (memory/activity.py).

    Here, not at each route, for exactly the reason the rate-limit check
    lives here: every REST handler and every MCP tool already funnels
    through `_resolve_bank`, so one call site covers both surfaces and
    neither can drift.

    `getattr` rather than isinstance: only RetainRequest carries content,
    document_id and metadata, and a type check would have to be repeated for
    every future subclass that adds one.
    """
    content = getattr(body, "content", None)
    metadata = getattr(body, "metadata", None) or {}
    activity.describe(
        action=action,
        scope=scope,
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        user_id=user_id,
        # The RESOLVED slug, never body.project_slug: a caller that followed
        # a rename tombstone would otherwise show up as a second project.
        project_slug=project_slug,
        bank_fingerprint=activity.fingerprint(bank_id),
        document_id=getattr(body, "document_id", None),
        content_bytes=len(content.encode("utf-8")) if content else None,
        agent=metadata.get("agent"),
    )


def _resolve_bank(
    body: ScopedRequest,
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
    *,
    create: bool = True,
    is_write: bool = False,
) -> tuple[str, str | None, str | None]:
    """Resolve a request's bank and audit master-key access to it.

    `action` names what the caller is about to do (e.g. "memory.recall",
    "memory.documents.delete") so the audit trail can tell routes apart
    instead of recording the byte-identical placement it used to.

    `is_write` gates SPEC §20's "rate-limit memory writes per credential"
    MUST (`memory.ratelimit.check`). Checked HERE, not per-route: every REST
    write handler and every MCP tool already funnels through this function
    (directly, or via the `_bank` wrapper in curation.py/documents.py/
    operations.py, or via `_run` in mcp/tools.py), so a boolean set once at
    each of those call sites is enough to cover both surfaces from one place
    instead of duplicating the check eight times. `reflect` passes
    `is_write=True` too even though it writes nothing to Hindsight -- it is a
    read that spends model tokens on a server-level key with no per-user cost
    attribution (SPEC §19.4), which is the actual thing this limiter defends
    against. `recall` passes `is_write=True` for a different reason: with
    `create=True` (the default) an unauthenticated-in-effect loop of
    `recall(project_slug=<random>)` mints a Project row per call -- each one
    permanently squatting a tenant-unique slug (invariant 8 makes slugs
    unique across live AND retired names, so none of them is ever
    recoverable) -- measured live at 80 projects in 5.1s against one key with
    no limiter on this route. It is a write by its actual effect even though
    the read it performs is free.

    Every caller must db.commit() after calling this — recording an audit
    row here is not itself a commit. An uncommitted row is invisible to any
    other session and vanishes if the request fails before the caller's own
    commit. All current call sites (_retain/recall/reflect in this module,
    and the `_bank` wrapper in curation.py/documents.py/operations.py)
    already commit right after; a new call site must too, or it silently
    drops audit rows.
    """
    if is_write:
        ratelimit.check(principal, on_behalf_of)
    if body.scope == "user":
        bank_id = resolve_user_bank(db, principal, body.user_id)
        if principal.is_master and body.user_id:
            # A master key reaching into a user's private bank. §20.3's
            # delegation case, and the only bank access in the service that is
            # not the caller's own — so it is the one that must not be
            # traceless.
            audit.record(db, principal, action, body.user_id, on_behalf_of=on_behalf_of)
        _describe(
            action, "user", principal, bank_id,
            user_id=body.user_id or principal.user_id, body=body,
        )
        return bank_id, None, None

    bank_id, resolved_from, project_slug = resolve_project_bank(
        # getattr: TypedRetainRequest (v0.4.0 retain) has no git_locator at
        # all -- that concept only ever served enrichment/mismatch-checking
        # for a lazily-CREATED project, and typed retain never creates one.
        db, principal, body.project_slug, getattr(body, "git_locator", None), create=create
    )
    if principal.is_master:
        # Mirrors the scope=user branch above: a master key reaching a
        # project's shared bank is the same delegation-shaped access, and
        # usually the larger blast radius (a team's shared memory, not one
        # person's). Same choke point, so no data-plane route can bypass it.
        audit.record(db, principal, action, project_slug, on_behalf_of=on_behalf_of)
    _describe(action, "project", principal, bank_id, project_slug=project_slug, body=body)
    return bank_id, resolved_from, project_slug


def resolve_bank_and_commit(
    body: ScopedRequest,
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
    *,
    is_write: bool = False,
) -> tuple[str, str | None, str | None]:
    """Authorize first, always -- then commit what resolution recorded.

    `create=False`: the curation, document and operation routes are lookups
    and maintenance over an EXISTING bank (SPEC §11.3), never first-touch
    creation -- one of them on an unknown slug must 404, not squat the slug
    for whoever asked first. A memory cannot exist in a bank the lookup just
    created either.

    The commit is not optional: `_resolve_bank` only APPENDS the master-key
    audit row, and an uncommitted row is invisible to every other session and
    vanishes if the request fails later.

    `is_write` forwards to `_resolve_bank`'s rate-limit gate (SPEC §20) --
    forget/correct/restore, document delete and operation cancel pass it; the
    list/get routes do not.
    """
    bank_id, resolved_from, project_slug = _resolve_bank(
        body, db, principal, on_behalf_of, action, create=False, is_write=is_write
    )
    db.commit()
    return bank_id, resolved_from, project_slug


def _strip_bank_id(value: Any, bank_id: str | None = None) -> Any:
    """Hindsight echoes bank_id; it must not reach the caller (SPEC inv. 29).

    Recursive on purpose. A top-level-only filter holds for today's flat retain
    response, but recall returns nested items and the invariant is absolute —
    it should not depend on an upstream response shape we do not control.

    Also redacts the bank_id as a SUBSTRING of an otherwise-unrelated field:
    measured against a live server (hindsight-api 0.9.1, 2026-08-22),
    `memories/list` embeds it inside `chunk_id`
    (f"{bank_id}_{document_id}_{n}"), so a key-only filter lets it straight
    through under a field name no mock ever included. Every caller must pass
    its own `bank_id` for this to close.
    """
    if isinstance(value, dict):
        return {
            k: _strip_bank_id(v, bank_id)
            for k, v in value.items()
            if k != "bank_id"
        }
    if isinstance(value, list):
        return [_strip_bank_id(item, bank_id) for item in value]
    if isinstance(value, str) and bank_id and bank_id in value:
        return value.replace(bank_id, "REDACTED")
    return value


@router.post("/retain", response_model=TypedRetainResponse, status_code=202)
def retain(
    body: TypedRetainRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> TypedRetainResponse:
    return _typed_retain(body, principal, on_behalf_of, db, wait=False)


@router.post("/sync_retain", response_model=TypedRetainResponse)
def sync_retain(
    body: TypedRetainRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> TypedRetainResponse:
    return _typed_retain(body, principal, on_behalf_of, db, wait=True)


def _typed_retain(
    body: TypedRetainRequest,
    principal: Principal,
    on_behalf_of: str | None,
    db: Session,
    *,
    wait: bool,
) -> TypedRetainResponse:
    # Existing-only (create=False): ordinary retain never mints an unknown
    # project. Resolved here too (not just inside submit_retain) so the
    # shared rate-limit/audit/activity choke point every other data-plane
    # route funnels through still covers typed retain.
    _resolve_bank(body, db, principal, on_behalf_of, "memory.retain", create=False, is_write=True)
    db.commit()
    return submit_retain(db, principal, body, client=get_client(), wait=wait)


# SPEC-cited elsewhere in this file, repeated here so the deprecation
# header's rationale is legible from this route alone: superseded by
# POST /v1/read/recall (Phase 5 plan), which returns the same closed, bounded
# hit shape without a legacy envelope, and is never itself a write (no
# first-touch project creation, no rate limit -- it cannot be, since it never
# creates anything to squat with).
_DEPRECATED_RECALL_HEADERS = {
    "Deprecation": "true",
    "Link": '</v1/read/recall>; rel="successor-version"',
}


@router.post("/recall", response_model=MemoryResponse)
def recall(
    body: RecallRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    response: Response,
    db: Session = Depends(get_session),
) -> MemoryResponse:
    # Same MEMORY_MAX_CONTENT_BYTES ceiling `retain` and `correct` carry
    # (SPEC §20), reused rather than a new bound: `query` is the same shared
    # field `reflect` below spends model tokens on.
    _check_content_size(body.query)
    read_bank = read_context.resolve_read_bank(
        db,
        principal,
        on_behalf_of,
        "memory.recall",
        body.scope,
        user_id=body.user_id,
        project_slug=body.project_slug,
    )
    db.commit()
    read_bank_ref = read_service.bank_ref(principal, read_bank)
    read_service.ensure_current_read_allowed(db, read_bank_ref)
    read_service.run_access_maintenance(db, read_bank_ref)

    response.headers.update(_DEPRECATED_RECALL_HEADERS)
    hits = read_service._recall_hits(read_bank.bank_id, body.query, "current", None)
    capped = hits[: read_models.DEFAULT_MAX_RESULTS]
    recalled = read_models.build_recall_response(
        project_slug=read_bank.current_slug,
        resolved_from=read_bank.resolved_from,
        hits=capped,
    )
    truncated = recalled.truncated or len(hits) > len(capped)
    return MemoryResponse(
        result={
            "hits": [hit.model_dump() for hit in recalled.hits],
            "truncated": truncated,
        },
        resolved_from=read_bank.resolved_from,
        project_slug=read_bank.current_slug,
    )


@router.post("/reflect", response_model=MemoryResponse)
def reflect(
    body: RecallRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> MemoryResponse:
    # `reflect` shares RecallRequest.query with `recall` above and spends
    # model tokens on a server-level credential with no per-user cost
    # attribution (SPEC §19.4) -- the actual thing the write limiter defends
    # against. `retain` was capped and this token-spending route was not.
    _check_content_size(body.query)
    # The rate-limit gate, kept: `_resolve_bank`'s own `is_write=True` path
    # is exactly this one call (memory/api/memory.py's own `_resolve_bank`,
    # a few dozen lines up), invoked here directly because bank resolution
    # itself is switching below to a resolver with no rate-limit concept of
    # its own (Phase 5 plan Task 4 Step 3, "keep its LLM-spend limiter").
    ratelimit.check(principal, on_behalf_of)
    # Bank resolution switched to the existing-only resolver (create=False,
    # no git_locator forwarded): unlike `recall` above, this route's write
    # is entirely LLM spend, not a Project row, so there is nothing here
    # that first-touch project creation is protecting -- switching away from
    # it immediately, rather than keeping it "for symmetry" with `recall`,
    # closes the same squatting surface `recall`'s own `is_write=True`
    # guard exists for, one call sooner.
    read_bank = read_context.resolve_read_bank(
        db,
        principal,
        on_behalf_of,
        "memory.reflect",
        body.scope,
        user_id=body.user_id,
        project_slug=body.project_slug,
    )
    db.commit()
    reflect_bank_ref = read_service.bank_ref(principal, read_bank)
    read_service.ensure_current_read_allowed(db, reflect_bank_ref)
    read_service.run_access_maintenance(db, reflect_bank_ref)
    bank_id = read_bank.bank_id
    resolved_from = read_bank.resolved_from
    project_slug = read_bank.current_slug if read_bank.scope == "project" else None
    result = get_client().reflect(bank_id, body.query)
    return MemoryResponse(
        result=_strip_bank_id(result, bank_id),
        resolved_from=resolved_from,
        project_slug=project_slug,
    )
