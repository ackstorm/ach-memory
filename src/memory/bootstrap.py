"""SPEC §7.5: an idempotent pre-warm, and nothing more.

Bootstrap ensures the authenticated User bank and its enabled `user-context`
built-in, and -- when a project slug is configured and that project already
exists -- its Project bank and `project-context` built-in. It never waits for
a built-in's synthesis to finish: `reconcile_builtin` records the upstream
operation and returns immediately.

**It creates nothing.** `retain` is the one place allowed to mint an unknown
slug, and it is the place carrying the audited per-caller hourly ceiling;
creating here spent one of those on every MCP session start, for a configured
slug the agent might never write to. An absent project and a foreign one are
reported identically -- as nothing at all -- because raising on the foreign
one would make this an existence oracle AND poison every project-scoped tool
call at startup, including the `retain` meant to create the absent one.

What still makes this worth calling: `link_identity` deliberately does not
provision the user's bank (a Hindsight round trip on the authentication path),
and no read path provisions anything. An agent's first call is almost always
`load_context`, so without this pre-warm a brand-new user's first session
finds an unprovisioned bank and no `user-context` at all.

`provision_project_bank`/`provision_user_bank` below are also called directly
from `api/projects.py` at creation time, so a bank created through the control
plane is usable immediately.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from memory import banks, mental_model_service, projects
from memory.auth.principal import Principal
from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT
from memory.errors import ProjectNotFound
from memory.mental_model_service import MentalModelView
from memory.models import Project, User
from memory.retain_strategy import ensure_exact_retain_strategy
from memory.retained_records import LogicalBankRef


def provision_project_bank(
    db: Session, principal: Principal, project: Project, *, client
) -> MentalModelView:
    """Everything a project bank needs before it can serve: the exact retain
    strategy, and its built-in model.

    Idempotent by construction -- `ensure_exact_retain_strategy` and
    `reconcile_builtin` are both no-ops on an already-provisioned bank -- so
    calling it again from bootstrap repairs anything a failed creation left
    half-done.
    """
    bank = LogicalBankRef(
        principal.tenant_id, "project", None, project.internal_id, project.bank_id
    )
    ensure_exact_retain_strategy(client, bank.bank_id)
    return mental_model_service.reconcile_builtin(db, bank, PROJECT_CONTEXT, client=client)


def provision_user_bank(db: Session, user: User, *, client) -> MentalModelView:
    """Everything a user bank needs before it can serve: the exact retain
    strategy, and its built-in model. Sibling of `provision_project_bank`,
    idempotent the same way."""
    bank = LogicalBankRef(user.tenant_id, "user", user.id, None, user.bank_id)
    ensure_exact_retain_strategy(client, bank.bank_id)
    return mental_model_service.reconcile_builtin(db, bank, USER_CONTEXT, client=client)


def provision_before_retain(
    db: Session, principal: Principal, *, scope: str, bank_id: str, client
) -> None:
    """Ensure both banks a first retain might need are ready -- retain is the
    one place allowed to create a project (lazy-provisioning plan, decision
    1). Called right after the REST/MCP gate's own create=True resolution, so
    the project row -- if this retain is what minted it -- already exists by
    the time this runs.

    Always provisions the calling user's own bank: a platform-authenticated
    user never passes through POST /v1/users, so `link_identity` leaves it
    unprovisioned (see its docstring) -- true regardless of this retain's
    scope. An operator is no exception: authority and identity are separate
    now, so they have a bank of their own and it is provisioned like anyone
    else's.

    Additionally provisions the project bank for a project-scoped retain.
    """
    if principal.user_id is not None:
        user = db.get(User, principal.user_id)
        if user is not None:
            provision_user_bank(db, user, client=client)
    if scope == "project":
        project = (
            db.query(Project)
            .filter_by(tenant_id=principal.tenant_id, bank_id=bank_id)
            .one()
        )
        provision_project_bank(db, principal, project, client=client)


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_slug: str | None = Field(default=None, max_length=128)
    builtins_enabled: bool = True


class ProjectOwnerView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["user"]
    id: str


class BootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_model: MentalModelView | None
    project_model: MentalModelView | None
    project_slug: str | None
    project_owner: ProjectOwnerView | None
    project_status: Literal["ready", "pending", "unavailable"] | None
    warnings: tuple[str, ...]


def bootstrap(
    db: Session, principal: Principal, request: BootstrapRequest, *, client
) -> BootstrapResult:
    user_bank_id = banks.resolve_user_bank(db, principal, None)
    user_bank = LogicalBankRef(principal.tenant_id, "user", principal.user_id, None, user_bank_id)
    ensure_exact_retain_strategy(client, user_bank.bank_id)

    user_model = None
    if request.builtins_enabled:
        user_model = mental_model_service.reconcile_builtin(
            db, user_bank, USER_CONTEXT, client=client
        )

    project_model = None
    project_slug = None
    project_owner = None
    project_status = None

    if request.project_slug:
        # create=False, and the miss is swallowed. Both halves matter: an
        # unknown slug is `retain`'s to mint, and a foreign one has to be
        # indistinguishable from an unknown one -- see the module docstring.
        try:
            resolution = projects.resolve(
                db, principal, request.project_slug, create=False
            )
        except ProjectNotFound:
            resolution = None

        if resolution is not None:
            project = resolution.project
            project_slug = resolution.current_slug
            project_owner = ProjectOwnerView(type="user", id=project.owner_id)
            project_bank = LogicalBankRef(
                principal.tenant_id, "project", None, project.internal_id, project.bank_id
            )
            # Retain strategy always applies, regardless of builtins_enabled --
            # mirrors the user bank above and ensure_exact_retain_strategy is a
            # no-op once already provisioned, so this is cheap either way.
            ensure_exact_retain_strategy(client, project_bank.bank_id)
            if request.builtins_enabled:
                project_model = provision_project_bank(db, principal, project, client=client)
            project_status = "ready"

    db.commit()
    return BootstrapResult(
        user_model=user_model,
        project_model=project_model,
        project_slug=project_slug,
        project_owner=project_owner,
        project_status=project_status,
        # Nothing warns any more: the only producer was the project
        # creation this no longer does. The field stays because callers parse it.
        warnings=(),
    )
