from sqlalchemy.orm import Session

from memory import projects
from memory.auth.principal import Principal
from memory.errors import Forbidden, InvalidScope, ProjectContextUnavailable
from memory.models import User


def resolve_user_bank(
    db: Session, principal: Principal, requested_user_id: str | None
) -> str:
    """Map scope=user to a bank ID.

    A user key always addresses itself; naming somebody else is a 403, not a
    silent redirect. A master key has no identity of its own, so it must name
    its target (SPEC §5.2).
    """
    if principal.is_master:
        if not requested_user_id:
            raise InvalidScope("master-key requests with scope=user must set user_id")
        target_id = requested_user_id
    else:
        if requested_user_id and requested_user_id != principal.user_id:
            raise Forbidden("a user key cannot address another user's memory")
        target_id = principal.user_id

    user = db.get(User, target_id)
    if user is None or user.tenant_id != principal.tenant_id:
        # Same shape as a cross-tenant miss: no existence signal either way.
        raise Forbidden("no accessible memory for the requested user")

    return user.bank_id


def resolve_project_bank(
    db: Session,
    principal: Principal,
    slug: str | None,
    git_locator: str | None = None,
    *,
    create: bool = True,
) -> tuple[str, str | None, str]:
    """Map scope=project to a bank ID.

    Returns (bank_id, resolved_from, project_slug) — project_slug is the
    project's current, live slug, so a caller who followed a rename tombstone
    (resolved_from set) learns what to switch to without a second round trip.

    create=False for the curation routes (list/get/forget/correct/restore):
    a memory cannot exist in a bank the lookup just created, and lazy
    creation there would let any authenticated caller squat an arbitrary
    slug (SPEC §11.3 vs. the first-touch creation SPEC §16.2 blesses for
    retain/recall/reflect, which keep the default).
    """
    if not slug:
        raise ProjectContextUnavailable(
            "scope=project needs MEMORY_PROJECT or a Git repository"
        )
    result = projects.resolve(db, principal, slug, git_locator, create=create)
    return result.project.bank_id, result.resolved_from, result.project.project_slug
