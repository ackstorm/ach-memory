from sqlalchemy.orm import Session

from memory import ids
from memory.auth.principal import Principal
from memory.models import AuditEvent


def record(
    db: Session,
    principal: Principal,
    action: str,
    resource: str,
    on_behalf_of: str | None = None,
) -> None:
    """Append an audit event. Caller commits.

    SPEC §20 MUST: master-key actions, ownership changes and renames are
    recorded.

    on_behalf_of is passed in, never derived from the principal. An operator
    acting for somebody else is indistinguishable, from inside this function,
    from one acting for themselves — both carry their own identity — so
    deriving it would make delegation unrecordable in exactly the case §5.2
    cares about. When the caller acts for itself it stays None: actor_key_id
    already says who that is. It is provenance and never authorization
    evidence.

    `actor_key_id` holds the credential that acted: an `ext_`-prefixed
    identity that `external_identities` resolves back to a human. Every
    authenticated caller now has one, operators included — authority is
    configuration read over an ordinary external identity, not a credential
    standing in place of one — so a NULL here no longer means "the master
    key" and is not reachable from any live path.
    """
    db.add(
        AuditEvent(
            id=ids.new_audit_id(),
            tenant_id=principal.tenant_id,
            actor_key_id=principal.credential_id,
            on_behalf_of=on_behalf_of,
            action=action,
            resource=resource,
        )
    )
