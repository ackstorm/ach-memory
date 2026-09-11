"""Bank provisioning, at the one moment that needs it: a retain.

A bank becomes usable when its ACH-owned retain strategy exists and its
built-in model has been reconciled. `link_identity` deliberately does not do
it (a Hindsight round trip on the authentication path), and no read path does
either -- it needs none: Hindsight banks auto-create on first use, so a read
against a never-retained bank is empty, not an error.

So the first `retain` provisions, for a user exactly as for a project. There
is no separate pre-warm step and no `/v1/bootstrap`: one rule, one moment.
A brand-new identity's reads return nothing until it has written something,
which is the true answer.

`provision_project_bank`/`provision_user_bank` are also called directly from
`api/projects.py` at creation time, so a bank created through the control
plane is usable immediately.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from memory import mental_model_service
from memory.auth.principal import Principal
from memory.builtin_models import PROJECT_CONTEXT, USER_CONTEXT
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
    every later retain repairs anything a failed creation left half-done.
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
        # The tenant clause is not redundant: `User.id` is a global primary
        # key, not tenant-scoped, so `db.get` alone would provision a bank for
        # a row that belongs to another tenant. `banks.resolve_user_bank`
        # makes the same check on the same lookup.
        if user is not None and user.tenant_id == principal.tenant_id:
            provision_user_bank(db, user, client=client)
    if scope == "project":
        project = (
            db.query(Project)
            .filter_by(tenant_id=principal.tenant_id, bank_id=bank_id)
            .one()
        )
        provision_project_bank(db, principal, project, client=client)
