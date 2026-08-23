from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import audit
from memory import projects as domain
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.common import RenameForwarding
from memory.auth.principal import Principal
from memory.db import get_session
from memory.errors import Forbidden, ProjectAccessDenied
from memory.models import Project

router = APIRouter(prefix="/v1/projects", tags=["projects"])


class Owner(BaseModel):
    type: str
    id: str


class CreateProjectRequest(BaseModel):
    # extra="forbid": same fix as UpdateProjectRequest below -- a typoed
    # git_locator key (e.g. "gti_locator") otherwise 201s silently with
    # git_locator left null, and the next retain's first-toucher enrichment
    # poisons the field the caller thought was already pinned (review
    # finding F2, a sibling of I1 on the create route).
    model_config = ConfigDict(extra="forbid")

    project_slug: str
    owner: Owner | None = None
    # Bounded to match the projects.git_locator column (String(512)) so an
    # oversize value is a typed 422 at the boundary, not a 500 from the DB.
    git_locator: str | None = Field(default=None, max_length=512)


class UpdateProjectRequest(BaseModel):
    # extra="forbid": the silent no-op this replaces IS review finding I1 --
    # a caller following SPEC §8.4 PATCHed git_locator, got 200 OK, and
    # nothing changed, because the model ignored the field it did not declare.
    model_config = ConfigDict(extra="forbid")

    project_slug: str | None = None
    # max_length: bounded to match the projects.git_locator column
    # (String(512)) so an oversize value is a typed 422 at the boundary, not
    # a 500 from the DB. min_length=1 keeps "" out of the clear-the-column
    # branch below: SPEC §8.4's "clear" is an explicit `null` (a caller who
    # deliberately has no locator to give), while an empty string carries no
    # locator information and almost always signals a caller bug -- the two
    # intents must not collapse into the same silent-clear behavior.
    git_locator: str | None = Field(default=None, max_length=512, min_length=1)


class ProjectResponse(RenameForwarding):
    project_slug: str
    owner: Owner
    git_locator: str | None = None


def _response(project: Project, resolved_from: str | None = None) -> ProjectResponse:
    """Built field by field. Never serialize the row: it carries bank_id and
    internal_id, neither of which may cross the boundary (inv. 29, inv. 34)."""
    return ProjectResponse(
        project_slug=project.project_slug,
        owner=Owner(type=project.owner_type, id=project.owner_id),
        git_locator=project.git_locator,
        resolved_from=resolved_from,
    )


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(
    body: CreateProjectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    owner = body.owner
    if owner is None:
        if principal.is_master:
            raise Forbidden("a master-key create must name an owner")
        owner = Owner(type="user", id=principal.user_id)
    elif not principal.is_master and not (
        owner.type == "user" and owner.id == principal.user_id
    ):
        # A user key may only create a project owned by itself (SPEC §16.2).
        raise Forbidden("a user key may only create a project it owns")

    # No ensure_tenant() here, unlike create_group: every reachable create
    # names an owner that already exists, and User/Group both carry a tenant
    # foreign key — so by the time an owner validates, the tenant row is
    # already there. A call would be dead in every path.
    #
    # Uniqueness, the creation race and owner validation all live in
    # memory.projects.create() — the route owns only the HTTP-facing rules
    # above (who may name which owner) and the commit.
    project = domain.create(
        db,
        principal,
        body.project_slug,
        owner.type,
        owner.id,
        body.git_locator,
        on_behalf_of=on_behalf_of,
    )
    db.commit()
    return _response(project)


@router.get("", response_model=list[ProjectResponse])
def list_projects(
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> list[ProjectResponse]:
    # Authorization is domain.authorize()'s job and only its job (SPEC §7):
    # running each row through it — rather than re-deriving the rule as a
    # membership set + filter here — means this list can never drift from
    # what get_project() would allow for the same caller. The master-key
    # bypass falls out of authorize() for free. The tenant clause stays
    # because authorize() itself does not check tenant.
    rows = db.scalars(
        select(Project)
        .where(Project.tenant_id == principal.tenant_id)
        .order_by(Project.project_slug)
    ).all()
    visible = []
    for p in rows:
        try:
            domain.authorize(db, principal, p)
        except ProjectAccessDenied:
            continue
        visible.append(p)
    return [_response(p) for p in visible]


@router.get("/{project_slug}", response_model=ProjectResponse)
def get_project(
    project_slug: str,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    result = domain.resolve(db, principal, project_slug, create=False)
    return _response(result.project, result.resolved_from)


@router.patch("/{project_slug}", response_model=ProjectResponse)
def update_project(
    project_slug: str,
    body: UpdateProjectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    """Rename, repair the locator, or both (SPEC §8.4, §9).

    `model_fields_set`, not a None check: §8.4 says "clear or update", so an
    explicit null must clear the column while an omitted key must leave it
    alone. A rename that silently wiped the locator would hand the next
    caller the first-toucher enrichment all over again.
    """
    result = domain.resolve(db, principal, project_slug, create=False)
    project = result.project

    if body.project_slug is not None:
        project = domain.rename(
            db, principal, project, body.project_slug, on_behalf_of=on_behalf_of
        )

    if "git_locator" in body.model_fields_set:
        project.git_locator = (
            domain.canonical_locator(body.git_locator) if body.git_locator else None
        )
        audit.record(
            db, principal, "project.locator.update", project.project_slug,
            on_behalf_of=on_behalf_of,
        )

    db.commit()
    return _response(project)


@router.patch("/{project_slug}/owner", response_model=ProjectResponse)
def transfer_project(
    project_slug: str,
    body: Owner,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    result = domain.resolve(db, principal, project_slug, create=False)
    project = domain.transfer(
        db, principal, result.project, body.type, body.id, on_behalf_of=on_behalf_of
    )
    db.commit()
    return _response(project)
