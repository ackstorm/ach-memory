"""SPEC §7.5: idempotent, explicit, auditable MCP/user/project bootstrap.

Bootstrap ensures the authenticated User bank and its enabled `user-context`
built-in, and -- when a project slug is configured -- resolves or creates
that project (owner=user), ensures its Project bank and its enabled
`project-context` built-in. It never waits for a built-in's synthesis to
finish: `reconcile_builtin` records the upstream operation and returns
immediately.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from memory import audit, banks, mental_model_service, projects
from memory.auth.principal import Principal
from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT
from memory.mental_model_service import MentalModelView
from memory.models import Project, ProjectSlug
from memory.retain_strategy import ensure_exact_retain_strategy
from memory.retained_records import LogicalBankRef
from memory.slugs import normalize_slug

logger = logging.getLogger("memory.bootstrap")


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


def _project_slug_exists(db: Session, tenant_id: str, slug: str) -> bool:
    return db.get(ProjectSlug, (tenant_id, normalize_slug(slug))) is not None


def bootstrap(
    db: Session, principal: Principal, request: BootstrapRequest, *, client
) -> BootstrapResult:
    warnings: list[str] = []

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
        existed_before = _project_slug_exists(db, principal.tenant_id, request.project_slug)

        # create=True: an MCP-configured project slug is trusted product
        # input (SPEC §4.3) -- a typo here is repaired through rename/alias,
        # not blocked at bootstrap. An existing but unauthorized slug still
        # resolves to the same indistinguishable ProjectNotFound as any
        # other unauthorized/unknown project.
        resolution = projects.resolve(db, principal, request.project_slug, create=True)
        project = resolution.project
        project_slug = resolution.current_slug
        project_owner = ProjectOwnerView(type="user", id=project.owner_id)

        if not existed_before:
            message = (
                f"created project '{project_slug}' tenant={principal.tenant_id} "
                f"actor={principal.credential_id} slug={project_slug} "
                "creation_source=mcp_bootstrap"
            )
            logger.warning(message)
            warnings.append(message)
            audit.record(
                db, principal, "project.create", f"{project_slug} creation_source=mcp_bootstrap"
            )
        db.commit()

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
        warnings=tuple(warnings),
    )
