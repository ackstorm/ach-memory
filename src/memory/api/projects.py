import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from memory import audit
from memory import projects as domain
from memory.api.app import current_on_behalf_of, current_principal
from memory.api.common import RenameForwarding
from memory.auth.principal import Principal
from memory.bootstrap import provision_project_bank
from memory.db import get_session
from memory.errors import Forbidden, ProjectAccessDenied
from memory.hindsight.client import get_client
from memory.identifiers import has_control_character
from memory.models import Project, ProjectSlug

router = APIRouter(prefix="/v1/projects", tags=["projects"])
logger = logging.getLogger("memory.api.projects")


class Owner(BaseModel):
    # extra="forbid": this doubles as the PATCH .../owner request body, where
    # a typoed key would transfer ownership to a None id.
    model_config = ConfigDict(extra="forbid")

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

    @field_validator("git_locator")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        # Reaches the projects INSERT. A control character there is a
        # psycopg DataError -> 500, not the IntegrityError this route's
        # `except` guards for -- same reasoning as ScopedRequest.git_locator.
        if value and has_control_character(value):
            raise ValueError("git_locator must not contain control characters")
        return value


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
    # Deterministic orientation delivered without spending memory on it.
    # Lengths mirror the projects columns so an oversize value is a typed 422
    # here, not a DB error; min_length=1 for the same reason as git_locator
    # above -- null is a deliberate clear, "" is a caller bug.
    name: str | None = Field(default=None, max_length=128, min_length=1)
    canonical_spec: str | None = Field(default=None, max_length=512, min_length=1)
    purpose: str | None = Field(default=None, max_length=256, min_length=1)

    @field_validator("name", "canonical_spec", "purpose")
    @classmethod
    def _metadata_no_control_characters(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        # Same psycopg DataError -> 500 as git_locator: these reach the
        # projects UPDATE too.
        if value and has_control_character(value):
            raise ValueError(f"{info.field_name} must not contain control characters")
        return value

    @field_validator("git_locator")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        # Reaches the projects UPDATE. A control character there is a
        # psycopg DataError -> 500, same as CreateProjectRequest.git_locator.
        if value and has_control_character(value):
            raise ValueError("git_locator must not contain control characters")
        return value


class ProjectResponse(RenameForwarding):
    project_slug: str
    owner: Owner
    git_locator: str | None = None
    name: str | None = None
    canonical_spec: str | None = None
    purpose: str | None = None


def _response(
    project: Project, current_slug: str, resolved_from: str | None = None
) -> ProjectResponse:
    """Built field by field. Never serialize the row: it carries bank_id and
    internal_id, neither of which may cross the boundary (inv. 29, inv. 34)."""
    return ProjectResponse(
        project_slug=current_slug,
        owner=Owner(type=project.owner_type, id=project.owner_id),
        git_locator=project.git_locator,
        name=project.name,
        canonical_spec=project.canonical_spec,
        purpose=project.purpose,
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
        (owner.type == "user" and owner.id == principal.user_id)
        or (owner.type == "group" and owner.id in principal.groups)
    ):
        # A user key may only create a project owned by itself (SPEC §16.2).
        # An external caller may also name a group its identity provider
        # asserts: it already owns that group's existing projects through
        # `authorize`, so refusing creation would let it reach a group project
        # by transfer but never make one.
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
    try:
        # A savepoint, not a bare try/except: provisioning writes to this
        # session, so a DB-level failure inside it (an IntegrityError from a
        # concurrent bootstrap registering the same built-in) leaves the
        # session in a failed state and makes the db.commit() below raise
        # PendingRollbackError -- a 500 with nothing committed, the exact
        # opposite of what this handler promises. begin_nested() rolls back
        # only the provisioning work and leaves the outer transaction usable.
        with db.begin_nested():
            provision_project_bank(db, principal, project, client=get_client())
    except Exception:
        # The project row is real and the caller gets its 201: provisioning is
        # idempotent, so a later bootstrap or the next creation attempt repairs
        # it. Failing the request here would leave a committed project the
        # caller was told did not exist.
        # exc_info: without the cause, a Hindsight outage, a name collision and
        # a code bug are one indistinguishable log line.
        logger.warning(
            "project created but not provisioned",
            extra={"project_slug": body.project_slug},
            exc_info=True,
        )
    db.commit()
    return _response(project, domain.canonical_slug(db, project))


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
    rows = db.execute(
        select(Project, ProjectSlug.slug)
        .join(
            ProjectSlug,
            (ProjectSlug.tenant_id == Project.tenant_id)
            & (ProjectSlug.project_internal_id == Project.internal_id)
            & ProjectSlug.is_canonical.is_(True),
        )
        .where(Project.tenant_id == principal.tenant_id)
        .order_by(ProjectSlug.slug)
    ).all()
    visible = []
    for project, current_slug in rows:
        try:
            domain.authorize(db, principal, project)
        except ProjectAccessDenied:
            continue
        visible.append((project, current_slug))
    return [_response(project, current_slug) for project, current_slug in visible]


@router.get("/{project_slug}", response_model=ProjectResponse)
def get_project(
    project_slug: str,
    principal: Annotated[Principal, Depends(current_principal)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    result = domain.resolve(db, principal, project_slug, create=False)
    return _response(result.project, result.current_slug, result.resolved_from)


@router.patch("/{project_slug}", response_model=ProjectResponse)
def update_project(
    project_slug: str,
    body: UpdateProjectRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    on_behalf_of: Annotated[str | None, Depends(current_on_behalf_of)],
    db: Session = Depends(get_session),
) -> ProjectResponse:
    """Rename, repair the locator, set the metadata record, or any of them (SPEC §8.4, §9).

    `model_fields_set`, not a None check: §8.4 says "clear or update", so an
    explicit null must clear the column while an omitted key must leave it
    alone. A rename that silently wiped the locator would hand the next
    caller the first-toucher enrichment all over again.
    """
    result = domain.resolve(db, principal, project_slug, create=False)
    project = result.project
    current_slug = result.current_slug

    if body.project_slug is not None:
        project = domain.rename(
            db, principal, project, body.project_slug, on_behalf_of=on_behalf_of
        )
        current_slug = domain.canonical_slug(db, project)

    if "git_locator" in body.model_fields_set:
        project.git_locator = (
            domain.canonical_locator(body.git_locator) if body.git_locator else None
        )
        audit.record(
            db, principal, "project.locator.update", current_slug,
            on_behalf_of=on_behalf_of,
        )

    # Same field-presence rule as the locator, for the same reason: a console
    # editor saving one field must not wipe the two it did not send, and an
    # explicit null must retract orientation rather than be ignored.
    # No audit event: the log records identity, credential, membership,
    # ownership and resolution changes -- the locator is in it because it is
    # the key an agent's repo resolves through. These three are descriptive,
    # like directives, which record none either.
    if "name" in body.model_fields_set:
        project.name = body.name
    if "canonical_spec" in body.model_fields_set:
        project.canonical_spec = body.canonical_spec
    if "purpose" in body.model_fields_set:
        project.purpose = body.purpose

    db.commit()
    # result.resolved_from, not a bare _response(project): SPEC §8.6 says a
    # request that reached the project through a rename tombstone is
    # annotated, and this route is forwarding-capable exactly like
    # get_project. Dropping it meant a client keying off `notice` to update a
    # stale MEMORY_PROJECT never learned it had followed one -- which is the
    # entire purpose of the tombstone.
    return _response(project, current_slug, result.resolved_from)


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
    # Same SPEC §8.6 forwarding annotation as update_project above.
    return _response(project, result.current_slug, result.resolved_from)
