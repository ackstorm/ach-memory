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
from memory.identifiers import reject_control_characters
from memory.models import Group, GroupMember, Project, ProjectSlug, User
from memory.slugs import canonical_locator, normalize_slug


@dataclass(frozen=True)
class Resolution:
    project: Project
    current_slug: str
    # The slug the caller asked for, when it was a retired one (SPEC §8.6).
    # None when they used the project's current slug.
    resolved_from: str | None


def _raise_missing_canonical(project_internal_id: str) -> None:
    raise RuntimeError(f"project {project_internal_id} has no canonical slug")


def canonical_slug(db: Session, project: Project) -> str:
    return db.scalar(
        select(ProjectSlug.slug).where(
            ProjectSlug.tenant_id == project.tenant_id,
            ProjectSlug.project_internal_id == project.internal_id,
            ProjectSlug.is_canonical.is_(True),
        )
    ) or _raise_missing_canonical(project.internal_id)


def _mapping(db: Session, tenant_id: str, slug: str) -> ProjectSlug | None:
    return db.get(ProjectSlug, (tenant_id, slug))


def _project_for_mapping(
    db: Session, tenant_id: str, mapping: ProjectSlug
) -> Project | None:
    # Tenant-filtered rather than a bare PK load so tenant isolation stays
    # local even if a corrupt mapping ever points across tenants.
    return db.scalar(
        select(Project).where(
            Project.internal_id == mapping.project_internal_id,
            Project.tenant_id == tenant_id,
        )
    )


def _slug_taken(db: Session, tenant_id: str, slug: str) -> bool:
    """All live and alias names occupy the same tenant-global namespace."""
    return _mapping(db, tenant_id, slug) is not None


def _validate_owner(
    db: Session, principal: Principal, owner_type: str, owner_id: str
) -> None:
    """The owner must exist in this tenant. An unchecked id silently orphans
    the project: authorize() then denies everyone and only a master key can
    undo it. Shared by create() and transfer()."""
    if owner_type == "user":
        reject_control_characters(owner_id, UserNotFound)
        owner = db.get(User, owner_id)
        if owner is None or owner.tenant_id != principal.tenant_id:
            raise UserNotFound(user_id=owner_id)
    elif owner_type == "group":
        reject_control_characters(owner_id, GroupNotFound)
        owner = db.get(Group, owner_id)
        if owner is None:
            # A group asserted by the caller's identity provider is real, it
            # just has no row yet -- nothing provisions one, because groups
            # arrive in a token rather than through POST /v1/groups. Created
            # here and only here: `authorize` needs no row, so this is the one
            # path that actually requires the group to exist, and creating it
            # on every authenticated request instead would write rows for
            # groups nobody ever uses.
            if owner_id not in principal.groups:
                raise GroupNotFound(group_id=owner_id)
            try:
                with db.begin_nested():
                    db.add(Group(id=owner_id, tenant_id=principal.tenant_id))
            except IntegrityError:
                # Lost the race; the row exists, which is all we needed.
                pass
        elif owner.tenant_id != principal.tenant_id:
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
    if project.owner_type == "group" and (
        # Asserted by the caller's identity provider, or recorded locally.
        # Checked independently and never merged: an IdP that stops asserting
        # a group revokes access on the next request without any row changing,
        # while a local key's membership stays a database fact.
        project.owner_id in principal.groups
        or db.get(GroupMember, (project.owner_id, principal.user_id))
    ):
        return
    raise ProjectAccessDenied(
        "no access to that project",
        project_slug=requested_slug or canonical_slug(db, project),
        owner_type=project.owner_type,
    )


def _authorize_resolution(
    db: Session, principal: Principal, project: Project, requested_slug: str
) -> None:
    """Hide whether a requested project name exists from unauthorized callers.

    Direct mutations still use ``authorize`` and its actionable 403. Slug
    resolution is the discovery boundary, so its denial deliberately has the
    same code, message and public details as an absent mapping.
    """
    try:
        authorize(db, principal, project, requested_slug=requested_slug)
    except ProjectAccessDenied as exc:
        raise ProjectNotFound(
            "no such project", project_slug=requested_slug
        ) from exc


def resolve(
    db: Session,
    principal: Principal,
    slug: str,
    git_locator: str | None = None,
    create: bool = True,
) -> Resolution:
    """Slug -> project, creating it lazily for a user credential.

    One lookup covers both canonical names and forwarding aliases. Checking
    that namespace before creating is what stops a rename from silently
    producing a second, empty project (SPEC §8.6).
    """
    slug = normalize_slug(slug)

    mapping = _mapping(db, principal.tenant_id, slug)
    project = (
        _project_for_mapping(db, principal.tenant_id, mapping)
        if mapping is not None
        else None
    )

    if project is None:
        if not create or principal.is_master:
            # A master key has no identity, so there is no owner to assign.
            raise ProjectNotFound("no such project", project_slug=slug)
        project = _create(db, principal, slug, git_locator)
        return Resolution(project, canonical_slug(db, project), None)

    _authorize_resolution(db, principal, project, slug)
    current_slug = slug if mapping.is_canonical else canonical_slug(db, project)
    resolved_from = None if mapping.is_canonical else slug

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
                project_slug=current_slug,
            )
        if not project.git_locator:
            # Enrichment, only for a caller already authorized (SPEC §8.3).
            project.git_locator = git_locator

    return Resolution(project, current_slug, resolved_from)


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
    _validate_owner(db, principal, owner_type, owner_id)

    project = Project(
        internal_id=ids.new_project_internal_id(),
        tenant_id=principal.tenant_id,
        git_locator=git_locator,
        owner_type=owner_type,
        owner_id=owner_id,
        bank_id=ids.new_project_bank_id(),
    )
    try:
        with db.begin_nested():
            db.add(project)
            db.add(
                ProjectSlug(
                    tenant_id=principal.tenant_id,
                    slug=slug,
                    project_internal_id=project.internal_id,
                    is_canonical=True,
                )
            )
    except IntegrityError as exc:
        # A savepoint, not a bare rollback, so any earlier write in this
        # request survives the lost race.
        raise ProjectSlugConflict("that slug is taken", project_slug=slug) from exc

    # SPEC §20 MUST: record master-key actions -- and every creation, not
    # only those, since the audit trail is also the counting source for the
    # per-user creation rate limit.
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
        mapping = _mapping(db, principal.tenant_id, slug)
        if mapping is None:
            raise
        existing = _project_for_mapping(db, principal.tenant_id, mapping)
        if existing is None:
            raise
        _authorize_resolution(db, principal, existing, slug)
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

    # The project-row lock serializes two concurrent renames before the fresh
    # canonical-row lookup; locking only the old canonical row can wake the
    # loser after that row was demoted, with the winner's new row absent from
    # the original SELECT snapshot. Demotion and insertion then share the
    # savepoint. The explicit flush orders the partial-unique transition; if
    # the new name loses a race, rolling back restores the old row.
    try:
        with db.begin_nested():
            db.scalar(
                select(Project.internal_id)
                .where(Project.internal_id == project.internal_id)
                .with_for_update()
            )
            current = db.scalar(
                select(ProjectSlug)
                .where(
                    ProjectSlug.tenant_id == project.tenant_id,
                    ProjectSlug.project_internal_id == project.internal_id,
                    ProjectSlug.is_canonical.is_(True),
                )
                .with_for_update()
            )
            if current is None:
                _raise_missing_canonical(project.internal_id)
            old_slug = current.slug
            if new_slug == old_slug:
                return project
            if _slug_taken(db, principal.tenant_id, new_slug):
                raise ProjectSlugConflict("that slug is taken", project_slug=new_slug)
            current.is_canonical = False
            db.flush()
            db.add(
                ProjectSlug(
                    tenant_id=project.tenant_id,
                    slug=new_slug,
                    project_internal_id=project.internal_id,
                    is_canonical=True,
                )
            )
    except IntegrityError as exc:
        raise ProjectSlugConflict("that slug is taken", project_slug=new_slug) from exc

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
    _validate_owner(db, principal, owner_type, owner_id)

    previous = f"{project.owner_type}:{project.owner_id}"
    slug = canonical_slug(db, project)
    project.owner_type = owner_type
    project.owner_id = owner_id
    audit.record(
        db,
        principal,
        "project.transfer",
        f"{slug}: {previous} -> {owner_type}:{owner_id}",
        on_behalf_of=on_behalf_of,
    )
    return project
