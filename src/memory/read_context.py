"""Existing-only, non-enriching bank resolution for read surfaces.

Every write path resolves a bank through `banks.py`/`projects.py` directly,
or through `api/memory.py`'s `_resolve_bank`, and both accept a caller
`git_locator` and (on `_resolve_bank`'s own default) create a project on
first touch. A read must do neither: the Phase 5 plan's non-negotiable
contracts forbid a read minting or enriching a `User`, `Project`, retired
slug or bank mapping. `resolve_read_bank` always resolves `create=False` and
never accepts a `git_locator` at all, so calling it can never create a
project, and can never repair or bind repository metadata onto one it merely
looked up -- `projects.resolve`'s enrichment branch only runs `if
git_locator:`, and this module has no parameter to feed it one.

The only tolerated writes around a call here are content-free operational
activity and mandatory delegated-master audit (SPEC §20 MUST), exactly like
`_resolve_bank`'s own -- both go through the same `activity`/`audit` modules,
isolated from any domain transaction: `activity.finish()` uses its own
`session_scope()`, decoupled from the caller's session entirely, and
`audit.record` only appends a row the caller must still commit itself,
alongside whatever else the caller's own transaction does.
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from memory import activity, audit, projects
from memory.auth.principal import Principal
from memory.banks import resolve_user_bank
from memory.errors import ProjectContextUnavailable

ReadScope = Literal["user", "project"]


@dataclass(frozen=True)
class ReadBank:
    """What a read needs to reach Hindsight, and nothing more.

    Exactly one of `user_id`/`project_internal_id` is set, matching `scope`.
    `current_slug` and `resolved_from` are project-only: `resolved_from` is
    the slug the caller asked for when it named a retired name that still
    forwards (None when they used the project's current slug, or under
    scope=user, which has no slugs at all).
    """

    bank_id: str
    scope: ReadScope
    user_id: str | None
    project_internal_id: str | None
    current_slug: str | None
    resolved_from: str | None


def resolve_read_bank(
    db: Session,
    principal: Principal,
    on_behalf_of: str | None,
    action: str,
    scope: ReadScope,
    *,
    user_id: str | None = None,
    project_slug: str | None = None,
) -> ReadBank:
    """Authorize and resolve an EXISTING bank for a read.

    `action` names the read for the activity row (e.g. "read.recall",
    "read.history"), matching `_resolve_bank`'s own `action` convention so
    the two surfaces stay distinguishable in the same table.

    Never creates, never enriches, never commits: the caller still owns its
    own transaction (for whatever domain-read work it goes on to do) and must
    still commit it to make the audit row this may have added durable, same
    as every existing `_resolve_bank` call site.
    """
    if scope == "user":
        bank_id = resolve_user_bank(db, principal, user_id)
        resolved_user_id = user_id or principal.user_id
        if principal.is_master and user_id:
            # Mirrors _resolve_bank: a master key reaching into a user's
            # private bank (SPEC §20.3 delegation) is the one access in this
            # service that must not be traceless.
            audit.record(db, principal, action, user_id, on_behalf_of=on_behalf_of)
        activity.describe(
            action=action,
            scope="user",
            tenant_id=principal.tenant_id,
            credential_id=principal.credential_id,
            user_id=resolved_user_id,
            project_slug=None,
            bank_fingerprint=activity.fingerprint(bank_id),
        )
        return ReadBank(
            bank_id=bank_id,
            scope="user",
            user_id=resolved_user_id,
            project_internal_id=None,
            current_slug=None,
            resolved_from=None,
        )

    # git_locator is deliberately absent from both the signature above and
    # this message: it is metadata that never resolves identity (inv. 11)
    # and the new read contract does not accept it at all (Phase 5 plan).
    # Naming it here would send a caller down a path that always ends in
    # this same error -- same reasoning as banks.resolve_project_bank's
    # identical message for the write path.
    if not project_slug:
        raise ProjectContextUnavailable(
            "scope=project needs a project: pass project_slug"
        )

    resolution = projects.resolve(db, principal, project_slug, create=False)
    project = resolution.project
    if principal.is_master:
        # Mirrors _resolve_bank: a master key reaching a project's shared
        # bank is the same delegation-shaped access, usually larger blast
        # radius (a team's memory, not one person's).
        audit.record(
            db, principal, action, resolution.current_slug, on_behalf_of=on_behalf_of
        )
    activity.describe(
        action=action,
        scope="project",
        tenant_id=principal.tenant_id,
        credential_id=principal.credential_id,
        user_id=None,
        # The RESOLVED slug, never the caller's: a caller who followed a
        # rename tombstone would otherwise show up as a second project.
        project_slug=resolution.current_slug,
        bank_fingerprint=activity.fingerprint(project.bank_id),
    )
    return ReadBank(
        bank_id=project.bank_id,
        scope="project",
        user_id=None,
        project_internal_id=project.internal_id,
        current_slug=resolution.current_slug,
        resolved_from=resolution.resolved_from,
    )
