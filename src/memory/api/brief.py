"""GET /v1/session-brief -- the instructions payload for one session.

Composed here rather than in the client because the source queries and the
output format are what decide whether a digest is useful or misleading, and
they must be changeable with a deploy. Putting them in the proxy would mean a
release, a tag and a plugin update on every host to fix a hallucination.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from memory import brief, projects, revisions
from memory import working_state as working_state_domain
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.memory import ScopedRequest, _resolve_bank, scoped_query_params
from memory.auth.principal import Principal
from memory.config import get_settings
from memory.db import get_session
from memory.errors import DomainError
from memory.hindsight.client import get_client
from memory.working_state import WORKSPACE_ID_PATTERN

router = APIRouter(prefix="/v1/session-brief", tags=["session-brief"])


class BriefResponse(BaseModel):
    instructions: str
    generated_at: str | None
    # What the composed tier CARRIES, not what the compiler was handed: a
    # budget drops sections, and the next task caches this pair alongside the
    # text it describes.
    sections: dict[str, bool]
    brief_revision: int
    memory_protocol: int
    tier: Literal["index", "full"]
    # Which revision counter the number above came from -- one per (user,
    # project). Without it, an index tier cached with no project and a full
    # tier fetched with one compare two unrelated sequences.
    project_slug: str | None
    # Which workspace's revision sequence, if any -- "" and no workspace are
    # both possible callers, and this is how a JSON consumer tells them apart.
    workspace_id: str | None


def _oldest(*sections: brief.Section | None) -> str | None:
    """ISO-8601 UTC strings from one source sort lexicographically, and every
    section here came from the same upstream field, so `min` is the oldest."""
    stamps = [s.refreshed_at for s in sections if s and s.refreshed_at]
    return min(stamps) if stamps else None


def _revision_stamp(section: brief.Section | None) -> str | None:
    """What this section contributes to the revision digest.

    A structured section carries a digest of what it SAYS, so a refresh that
    re-derived an identical profile -- or an upstream array that came back
    permuted -- keeps the revision and leaves every consumer's cache valid. A
    legacy prose section has no normalized form to hash, only whatever
    markdown the refresh happened to emit, so it keeps contributing its
    refresh timestamp as before.

    Branching on the field rather than on the delivery mode: whichever loader
    built the section already answered the question, and reading the flag
    again here would let the two disagree.
    """
    if section is None:
        return None
    if section.content_fingerprint is not None:
        return section.content_fingerprint
    return section.refreshed_at


@router.get(
    "",
    response_model=BriefResponse,
    # `format=text` returns a PlainTextResponse, which bypasses the response
    # model. Declared, or the schema promises JSON to a hook that asks for
    # text and gets it.
    responses={200: {"content": {"text/plain": {}}}},
)
def session_brief(
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    # Query params on the handler, not fields on ScopedRequest: that model is
    # extra="forbid" and shared by every data-plane route, so a delivery
    # concern added there would start rejecting requests everywhere else.
    #
    # `tier` defaults to "full" so the MCP proxy, which asks for neither,
    # keeps getting a whole brief until it moves to the index tier.
    tier: Annotated[Literal["index", "full"], Query()] = "full",
    host: Annotated[str | None, Query()] = None,
    # Absent by default: a client that never resolved a workspace (not in a
    # git worktree, or an older client) gets the existing no-workspace
    # behavior, not a validation error.
    workspace_id: Annotated[str | None, Query(pattern=WORKSPACE_ID_PATTERN.pattern)] = None,
    # Aliased rather than named `format`: the query parameter has to keep that
    # name for the hook, the argument must not shadow the builtin.
    response_format: Annotated[Literal["json", "text"], Query(alias="format")] = "json",
    db: Session = Depends(get_session),
) -> BriefResponse | PlainTextResponse:
    now = datetime.now(UTC)
    client = get_client()
    # The one thing MEMORY_PROFILE_DELIVERY_MODE changes: which function
    # builds a scope's Section. Both loaders return the same shape, so the
    # allocator below composes structured and prose sections identically --
    # mandatory orientation and Working State keep their reservations either
    # way, and a profile is still the optional material dropped first.
    #
    # There is deliberately no fallback between them: a structured read that
    # finds nothing serves no section, rather than a prose item the structured
    # synthesis may already have superseded.
    structured = get_settings().profile_delivery_mode == "structured"

    # `on_behalf_of` is the only identity a master key has here: the route is
    # read-on-behalf-of by construction (§16.5), and `_resolve_bank` uses the
    # header for the audit row, never for resolution. A user key never sees the
    # header (`current_on_behalf_of` blanks it) and its `?user_id` reaches
    # `resolve_user_bank`, where naming somebody else is a 403, not a silent
    # redirect.
    user_bank, _, _ = _resolve_bank(
        ScopedRequest(scope="user", user_id=on_behalf_of or scoped.user_id),
        db, principal, on_behalf_of, "brief.get",
        create=False,
    )
    user_section = (
        brief.get_structured_section(client, user_bank, "user", now)
        if structured
        else brief.get_section(client, user_bank, now)
    )

    project_section = None
    project_slug = None
    project_internal_id = None
    orientation = None
    project_stamp = None
    if scoped.project_slug or scoped.git_locator:
        try:
            project_bank, _, project_slug = _resolve_bank(
                ScopedRequest(
                    scope="project",
                    project_slug=scoped.project_slug,
                    git_locator=scoped.git_locator,
                ),
                db, principal, on_behalf_of, "brief.get", create=False,
            )
            # Resolved twice on purpose: `_resolve_bank` hands back a bank id
            # and a slug, never the row, and the orientation record lives on
            # the row. Same authorization, same tombstone forwarding.
            project = projects.resolve(db, principal, project_slug, create=False).project
        except DomainError:
            # A project this caller cannot reach, or one that does not exist
            # yet, is a missing section -- never an error. The session starts
            # either way, and the agent is told nothing rather than something
            # wrong.
            project_slug = None
        else:
            project_section = (
                brief.get_structured_section(client, project_bank, "project", now)
                if structured
                else brief.get_section(client, project_bank, now)
            )
            # Orientation is unaffected by the delivery mode on purpose: it is
            # Project Metadata, a record compiled from the row below, and no
            # learned profile -- structured or prose -- may supply it.
            orientation = brief.Orientation(
                # Nothing seeds `name`: the metadata columns landed unset
                # rather than storing a copy of the slug, so the slug names the
                # project until somebody writes a name.
                name=project.name or project_slug,
                canonical_spec=project.canonical_spec,
                purpose=project.purpose,
            )
            # onupdate=utcnow, so a metadata edit moves this and the revision
            # bumps with it.
            project_stamp = project.updated_at.isoformat()
            project_internal_id = project.internal_id

    # Working State is read only once the project it belongs to is
    # authorized AND a workspace was named -- an unresolved project or a bare
    # /v1/session-brief call (no workspace_id) never sees it.
    working_state_row = None
    if project_internal_id and workspace_id:
        working_state_row = working_state_domain.get_current(
            db,
            principal,
            project_internal_id,
            workspace_id,
            user_id=on_behalf_of or scoped.user_id or principal.user_id,
        )
    working_state_stamp = working_state_domain.state_fingerprint(working_state_row)

    revision = revisions.current(
        db,
        principal.tenant_id,
        # The user whose memory was actually read. A master key has no identity
        # of its own, so On-Behalf-Of is it; `resolve_user_bank` above has
        # already refused every case where none of the three is set.
        on_behalf_of or scoped.user_id or principal.user_id,
        project_slug or "",
        revisions.fingerprint(
            _revision_stamp(user_section),
            _revision_stamp(project_section),
            project_stamp,
            working_state_stamp,
        ),
        workspace_id=workspace_id or "",
    )
    # One commit for the audit rows and the revision together: a consumer must
    # never hold a revision this service did not record issuing.
    db.commit()

    # Working State is item 3 of both tiers. Index and Full use separate
    # renderers (working_state.py) because INDEX_CAPS["working_state"] == 1 --
    # the Full multiline body can never be reused as the Index input.
    working_state_index = (
        working_state_domain.render_index_headline(working_state_row, now)
        if working_state_row
        else None
    )
    working_state_full = (
        working_state_domain.render_full_section(working_state_row, now)
        if working_state_row
        else None
    )

    if tier == "index":
        instructions = brief.compose_index(
            revision, project_slug, user_section, orientation, project_section,
            working_state_index, brief.budget_for(host),
        )
    else:
        instructions = brief.compose_full(
            revision, project_slug, user_section, orientation, project_section,
            working_state_full, max_tokens=brief.FULL_MAX_TOKENS,
        )

    if response_format == "text":
        # So a SessionStart hook stays a curl and a cat: no jq, no node, no
        # runtime that has to be installed before memory works.
        return PlainTextResponse(instructions)

    working_state_section = working_state_index if tier == "index" else working_state_full
    return BriefResponse(
        instructions=instructions,
        generated_at=_oldest(user_section, project_section, working_state_section),
        sections=brief.survived(instructions),
        brief_revision=revision,
        memory_protocol=brief.MEMORY_PROTOCOL,
        tier=tier,
        project_slug=project_slug,
        workspace_id=workspace_id,
    )
