"""Slug -> Project resolution, lazy creation, and rename history (SPEC §7-8)."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.errors import Forbidden, ProjectNotFound
from memory.models import Project, ProjectSlug
from memory.slugs import normalize_slug


@dataclass(frozen=True)
class Resolved:
    project: Project
    created: bool
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
        bank_id=f"project_{slug}",
        git_locator=None,
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
            raise ProjectNotFound(f"no project for slug {slug}; a first retain creates it", project_slug=slug)
        # Two first retains for one slug (parallel subagents) must not race the insert.
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"project:{slug}"})
        if (mapping := db.get(ProjectSlug, slug)) is None:
            return Resolved(_create(db, principal, slug), created=True, notice="PROJECT_CREATED")

    project = db.get(Project, mapping.project_internal_id)
    if not _authorized(principal, project):
        raise Forbidden(project_slug=slug)
    notice = None if mapping.is_canonical else "PROJECT_RENAMED"
    return Resolved(project, created=False, notice=notice)


def rename(db: Session, principal: Principal, project: Project, new_slug: str) -> None:
    """Retire the current canonical slug and mint a new one; the retired row
    stays as a forwarding tombstone (SPEC §8.6's slug history)."""
    if not _authorized(principal, project):
        raise Forbidden(project_slug=new_slug)
    new_slug = normalize_slug(new_slug)
    current = db.scalars(
        select(ProjectSlug).where(
            ProjectSlug.project_internal_id == project.internal_id,
            ProjectSlug.is_canonical.is_(True),
        )
    ).one()
    current.is_canonical = False
    db.flush()
    db.add(ProjectSlug(slug=new_slug, project_internal_id=project.internal_id, is_canonical=True))
    db.flush()
