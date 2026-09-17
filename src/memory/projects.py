"""Slug -> Project resolution, lazy creation, and rename history (SPEC §7-8)."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.errors import Forbidden, InvalidRequest, ProjectNotFound
from memory.models import Project, ProjectSlug
from memory.slugs import normalize_slug


@dataclass(frozen=True)
class Resolved:
    project: Project
    notice: str | None


def _authorized(principal: Principal, project: Project) -> bool:
    if principal.is_operator:
        return True
    if project.owner_type == "user":
        return project.owner_id == principal.user_id
    if project.owner_type == "group":
        return project.owner_id in principal.groups
    return False


def _create(db: Session, principal: Principal, slug: str) -> Project:
    project = Project(
        internal_id=f"prj_{uuid4().hex}",
        owner_type="user",
        owner_id=principal.user_id,
        bank_id=f"project_{uuid4()}",
    )
    db.add(project)
    db.flush()
    db.add(ProjectSlug(slug=slug, project_internal_id=project.internal_id, is_canonical=True))
    db.flush()
    return project


def resolve(db: Session, principal: Principal, slug: str, *, create: bool) -> Resolved:
    """Slug -> project. A retired slug (one an earlier `rename` moved away
    from) still resolves, with a PROJECT_RENAMED notice. An unknown slug is
    created lazily when `create=True`, owned by the calling user, with a
    PROJECT_CREATED notice; otherwise it raises ProjectNotFound."""
    slug = normalize_slug(slug)
    mapping = db.get(ProjectSlug, slug)

    if mapping is None:
        if not create:
            raise ProjectNotFound(f"no project for slug {slug}; a first retain creates it")
        # Two first retains for one slug (parallel subagents) must not race the insert.
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"project:{slug}"})
        if (mapping := db.get(ProjectSlug, slug)) is None:
            return Resolved(_create(db, principal, slug), notice="PROJECT_CREATED")

    project = db.get(Project, mapping.project_internal_id)
    if not _authorized(principal, project):
        raise Forbidden()
    notice = None if mapping.is_canonical else "PROJECT_RENAMED"
    return Resolved(project, notice=notice)


def rename(db: Session, principal: Principal, project: Project, new_slug: str) -> str:
    """Retire the current canonical slug and mint a new one; the retired row
    stays as a forwarding tombstone (SPEC §8.6's slug history). Returns the
    retired slug.

    A slug another project already holds is a caller error, not a collision to
    resolve: slugs are globally unique, so taking one would silently steer that
    project's retains into this bank. Renaming back to one of this project's own
    retired slugs is allowed and just flips the tombstone.
    """
    if not _authorized(principal, project):
        raise Forbidden()
    new_slug = normalize_slug(new_slug)
    current = db.scalars(
        select(ProjectSlug).where(
            ProjectSlug.project_internal_id == project.internal_id,
            ProjectSlug.is_canonical.is_(True),
        )
    ).one()
    if new_slug == current.slug:
        raise InvalidRequest(f"{new_slug} is already this project's slug")

    existing = db.get(ProjectSlug, new_slug)
    if existing is not None and existing.project_internal_id != project.internal_id:
        raise InvalidRequest(f"another project already uses the slug {new_slug}")

    current.is_canonical = False
    db.flush()  # release the partial unique index before the new canonical row lands
    if existing is not None:
        existing.is_canonical = True
    else:
        db.add(ProjectSlug(slug=new_slug, project_internal_id=project.internal_id, is_canonical=True))
    db.flush()
    return current.slug
