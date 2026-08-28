"""GET /v1/session-brief -- the instructions payload for one session.

Composed here rather than in the client because the source queries and the
output format are what decide whether a digest is useful or misleading, and
they must be changeable with a deploy. Putting them in the proxy would mean a
release, a tag and a plugin update on every host to fix a hallucination.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from memory import brief
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.memory import ScopedRequest, _resolve_bank, scoped_query_params
from memory.auth.principal import Principal
from memory.db import get_session
from memory.errors import DomainError
from memory.hindsight.client import get_client
from memory.mcp.server import INSTRUCTIONS

router = APIRouter(prefix="/v1/session-brief", tags=["session-brief"])


class BriefResponse(BaseModel):
    instructions: str
    generated_at: str | None
    sections: dict[str, bool]


def _oldest(*sections: brief.Section | None) -> str | None:
    """ISO-8601 UTC strings from one source sort lexicographically, and every
    section here came from the same upstream field, so `min` is the oldest."""
    stamps = [s.refreshed_at for s in sections if s and s.refreshed_at]
    return min(stamps) if stamps else None


@router.get("", response_model=BriefResponse)
def session_brief(
    scoped: Annotated[ScopedRequest, Depends(scoped_query_params)],
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> BriefResponse:
    now = datetime.now(UTC)
    client = get_client()

    user_bank, _, _ = _resolve_bank(
        ScopedRequest(scope="user"), db, principal, on_behalf_of, "brief.get",
        create=False,
    )
    user_section = brief.ensure_section(client, user_bank, brief.USER_QUERY, now)

    project_section = None
    project_slug = None
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
        except DomainError:
            # A project this caller cannot reach, or one that does not exist
            # yet, is a missing section -- never an error. The session starts
            # either way, and the agent is told nothing rather than something
            # wrong.
            project_slug = None
        else:
            project_section = brief.ensure_section(
                client, project_bank, brief.PROJECT_QUERY, now
            )
    db.commit()

    return BriefResponse(
        instructions=brief.compose(
            INSTRUCTIONS, user_section, project_section, project_slug
        ),
        generated_at=_oldest(user_section, project_section),
        sections={
            "user": user_section is not None,
            "project": project_section is not None,
        },
    )
