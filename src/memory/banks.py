from sqlalchemy.orm import Session

from memory import projects
from memory.auth.principal import Principal
from memory.errors import (
    Forbidden,
    ProjectContextUnavailable,
    UserNotFound,
)
from memory.models import User


def resolve_user_bank(
    db: Session, principal: Principal, requested_user_id: str | None
) -> str:
    """Map scope=user to a bank ID.

    Everyone addresses themselves by default, operators included: authority
    and identity are separate now, so an operator has a bank of their own and
    reaches it the same way anybody does. Naming somebody ELSE is the
    authority part, and for anyone without it that is a 403, never a silent
    redirect.
    """
    target_id = requested_user_id or principal.user_id
    if target_id != principal.user_id and not principal.is_master:
        raise Forbidden("this caller cannot address another user's memory")

    user = db.get(User, target_id)
    if user is None or user.tenant_id != principal.tenant_id:
        if principal.is_master:
            # An operator already bypasses ownership inside their tenant (SPEC
            # §20.3), so there is no existence fact to withhold from them, and
            # §18 names USER_NOT_FOUND for exactly this case. A 403 sent an
            # operator with a typo hunting a permissions problem that does not
            # exist. From tenant A's view a user living only in tenant B does
            # not exist either, so this still discloses nothing cross-tenant.
            raise UserNotFound(user_id=target_id)
        # For everyone else the shape stays: same as a cross-tenant miss, no
        # existence signal either way.
        raise Forbidden("no accessible memory for the requested user")

    return user.bank_id


def resolve_project_bank(
    db: Session,
    principal: Principal,
    slug: str | None,
    git_locator: str | None = None,
    *,
    create: bool = False,
) -> tuple[str, str | None, str, bool]:
    """Map scope=project to a bank ID.

    Returns (bank_id, resolved_from, project_slug, created) — project_slug is
    the project's current, live slug, so a caller who followed a rename
    tombstone (resolved_from set) learns what to switch to without a second
    round trip; created is True only when this very call minted the project.

    create=False by default: every read, write and curation path in this
    service resolves existing-only, and lazy creation there would let any
    authenticated caller squat an arbitrary slug (SPEC §11.3). Creation is
    explicit and belongs to retain (SPEC §16.2), the only caller that
    passes create=True.
    """
    if not slug:
        # git_locator is deliberately absent from this message. It is metadata
        # that never resolves identity (inv. 11) and is not unique (§17), so
        # this service cannot turn one into a project -- naming it here sent
        # models down a path that always ends in this same error. Deriving the
        # slug from the remote is the client's job (§8.2, §10); ach-memory's
        # stdio proxy does it, so the model rarely sees this at all.
        raise ProjectContextUnavailable(
            "scope=project needs a project: pass project_slug"
        )
    result = projects.resolve(db, principal, slug, git_locator, create=create)
    return result.project.bank_id, result.resolved_from, result.current_slug, result.created
