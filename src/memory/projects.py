from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import audit, ids
from memory.auth.principal import Principal
from memory.errors import (
    GroupNotFound,
    InvalidOwnerType,
    ProjectAccessDenied,
    ProjectLocatorMismatch,
    ProjectNotFound,
    ProjectSlugConflict,
    UserNotFound,
)
from memory.models import Group, GroupMember, Project, RetiredSlug, User
from memory.slugs import canonical_locator, normalize_slug


@dataclass(frozen=True)
class Resolution:
    project: Project
    # The slug the caller asked for, when it was a retired one (SPEC §8.6).
    # None when they used the project's current slug.
    resolved_from: str | None


def _live(db: Session, tenant_id: str, slug: str) -> Project | None:
    return db.scalar(
        select(Project).where(
            Project.tenant_id == tenant_id, Project.project_slug == slug
        )
    )


def _forwarded(db: Session, tenant_id: str, slug: str) -> Project | None:
    tombstone = db.get(RetiredSlug, (tenant_id, slug))
    if tombstone is None:
        return None
    # Tenant-filtered again rather than a bare PK load: the tombstone key is
    # already tenant-scoped, so this is redundant today, but it keeps the
    # isolation guarantee local to this function instead of inferred from a
    # caller three layers up.
    return db.scalar(
        select(Project).where(
            Project.internal_id == tombstone.project_internal_id,
            Project.tenant_id == tenant_id,
        )
    )


def _slug_taken(db: Session, tenant_id: str, slug: str) -> bool:
    """Uniqueness spans live projects AND tombstones (inv. 13): a retired
    name stays reserved, or a forward would start pointing somewhere new.
    Shared by create() and rename() so the rule has one definition."""
    return _live(db, tenant_id, slug) is not None or (
        db.get(RetiredSlug, (tenant_id, slug)) is not None
    )


def _validate_owner(
    db: Session, tenant_id: str, owner_type: str, owner_id: str
) -> None:
    """The owner must exist in this tenant. An unchecked id silently orphans
    the project: authorize() then denies everyone and only a master key can
    undo it. Shared by create() and transfer()."""
    if owner_type == "user":
        owner = db.get(User, owner_id)
        if owner is None or owner.tenant_id != tenant_id:
            raise UserNotFound(user_id=owner_id)
    elif owner_type == "group":
        owner = db.get(Group, owner_id)
        if owner is None or owner.tenant_id != tenant_id:
            raise GroupNotFound(group_id=owner_id)
    else:
        # Guarded here and not only at the API edge: a bad owner_type would
        # make authorize() fall through to a denial for everyone, silently
        # orphaning the project.
        raise InvalidOwnerType("owner type must be user or group")


def authorize(
    db: Session,
    principal: Principal,
    project: Project,
    requested_slug: str | None = None,
) -> None:
    """SPEC §7. The error names the slug and the owner KIND, never the owner.

    Revealing owner_type turns "denied" into "ask a human or ask for a group",
    which is the recovery path §8.5 trades that disclosure for. Revealing
    owner_id would leak who works on what.

    Echo back the slug the caller ASKED for, not the project's current one: a
    denial after following a tombstone would otherwise disclose the rename
    target to someone who only knew the retired name.
    """
    if principal.is_master:
        return
    if project.owner_type == "user" and project.owner_id == principal.user_id:
        return
    if project.owner_type == "group" and db.get(
        GroupMember, (project.owner_id, principal.user_id)
    ):
        return
    raise ProjectAccessDenied(
        "no access to that project",
        project_slug=requested_slug or project.project_slug,
        owner_type=project.owner_type,
    )


def resolve(
    db: Session,
    principal: Principal,
    slug: str,
    git_locator: str | None = None,
    create: bool = True,
) -> Resolution:
    """Slug -> project, creating it lazily for a user credential.

    Order matters: live projects, then retired slugs, then creation. Checking
    tombstones before creating is what stops a rename from silently producing a
    second, empty project (SPEC §8.6).
    """
    slug = normalize_slug(slug)

    project = _live(db, principal.tenant_id, slug)
    resolved_from = None
    if project is None:
        project = _forwarded(db, principal.tenant_id, slug)
        if project is not None:
            resolved_from = slug

    if project is None:
        if not create or principal.is_master:
            # A master key has no identity, so there is no owner to assign.
            raise ProjectNotFound("no such project", project_slug=slug)
        return Resolution(_create(db, principal, slug, git_locator), None)

    authorize(db, principal, project, requested_slug=slug)

    if git_locator:
        # Canonicalize before comparing: the same repository spelled two ways
        # (scp-style vs. https, trailing .git, a differing scheme) must not
        # look like two different repositories (SPEC §8.4). Raises
        # ProjectInvalidSlug — a typed 400, not a 500 — for a locator that
        # names no host/path.
        git_locator = canonical_locator(git_locator)
        if project.git_locator and project.git_locator != git_locator:
            raise ProjectLocatorMismatch(
                "that project is bound to a different repository",
                project_slug=project.project_slug,
            )
        if not project.git_locator:
            # Enrichment, only for a caller already authorized (SPEC §8.3).
            project.git_locator = git_locator

    return Resolution(project, resolved_from)


def create(
    db: Session,
    principal: Principal,
    slug: str,
    owner_type: str,
    owner_id: str,
    git_locator: str | None = None,
    on_behalf_of: str | None = None,
) -> Project:
    """Explicit creation for the HTTP control plane (SPEC §16.2).

    Unlike the lazy path in _create() below, the caller named a specific slug
    and owner, so a uniqueness race is reported back as PROJECT_SLUG_CONFLICT
    rather than silently resolved by attaching to whoever won it.
    """
    slug = normalize_slug(slug)
    if git_locator:
        # Same canonicalization as resolve()'s comparison, so a locator
        # stored at creation is never a different spelling than one a later
        # resolve() compares it against.
        git_locator = canonical_locator(git_locator)
    if _slug_taken(db, principal.tenant_id, slug):
        raise ProjectSlugConflict("that slug is taken", project_slug=slug)
    _validate_owner(db, principal.tenant_id, owner_type, owner_id)

    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=principal.tenant_id,
        project_slug=slug,
        git_locator=git_locator,
        owner_type=owner_type,
        owner_id=owner_id,
        bank_id=ids.new_project_bank_id(),
    )
    try:
        with db.begin_nested():
            db.add(project)
    except IntegrityError as exc:
        # A savepoint, not a bare rollback, so any earlier write in this
        # request survives the lost race.
        raise ProjectSlugConflict("that slug is taken", project_slug=slug) from exc

    if principal.is_master:
        # SPEC §20 MUST: record master-key actions.
        audit.record(db, principal, "project.create", slug, on_behalf_of=on_behalf_of)
    return project


def _create(
    db: Session, principal: Principal, slug: str, git_locator: str | None
) -> Project:
    """The lazy path used by resolve(): auto-vivify a project for its first
    toucher, always owned by the calling user (resolve() already refuses this
    for a master key, which has no identity to own it)."""
    try:
        return create(db, principal, slug, "user", principal.user_id, git_locator)
    except ProjectSlugConflict:
        # Lost the creation race (SPEC §9). The winner's project is now the
        # truth; reload it and authorize this caller against it — which is
        # usually a denial, and correctly so.
        existing = _live(db, principal.tenant_id, slug)
        if existing is None:
            raise
        authorize(db, principal, existing)
        return existing


def rename(
    db: Session,
    principal: Principal,
    project: Project,
    new_slug: str,
    on_behalf_of: str | None = None,
) -> Project:
    """Change the public slug, leaving a forwarding tombstone (SPEC §8.6)."""
    authorize(db, principal, project)
    new_slug = normalize_slug(new_slug)
    if new_slug == project.project_slug:
        return project

    if _slug_taken(db, principal.tenant_id, new_slug):
        raise ProjectSlugConflict("that slug is taken", project_slug=new_slug)

    old_slug = project.project_slug
    project.project_slug = new_slug

    # Nothing repoints existing tombstones, and nothing needs to: a rename
    # mutates the slug on the same Project row, so internal_id never changes.
    # Every tombstone already points at the row, so resolution after a chain of
    # renames is still ONE lookup — no transitive walk, no cycles.
    db.add(
        RetiredSlug(
            tenant_id=principal.tenant_id,
            retired_slug=old_slug,
            project_internal_id=project.internal_id,
        )
    )
    audit.record(
        db,
        principal,
        "project.rename",
        f"{old_slug} -> {new_slug}",
        on_behalf_of=on_behalf_of,
    )
    return project


def transfer(
    db: Session,
    principal: Principal,
    project: Project,
    owner_type: str,
    owner_id: str,
    on_behalf_of: str | None = None,
) -> Project:
    """Move ownership. Any authorized caller may do this in v1 (inv. 15).

    Accepted consequence, stated in SPEC §6.1: a single group member can
    transfer a group-owned project to themselves and lock the group out. The
    alternative is a group-admin role, and v1 has no permission model. The
    audit event is the mitigation.
    """
    authorize(db, principal, project)
    _validate_owner(db, principal.tenant_id, owner_type, owner_id)

    previous = f"{project.owner_type}:{project.owner_id}"
    project.owner_type = owner_type
    project.owner_id = owner_id
    audit.record(
        db,
        principal,
        "project.transfer",
        f"{project.project_slug}: {previous} -> {owner_type}:{owner_id}",
        on_behalf_of=on_behalf_of,
    )
    return project
