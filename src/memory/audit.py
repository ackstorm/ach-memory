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

    on_behalf_of is passed in, never derived from the principal. A master key
    has no identity of its own — principal.user_id is None for every master
    call — so deriving it would make delegation unrecordable in exactly the
    case §5.2 cares about. For a user key it stays None: the caller acts for
    itself, and actor_key_id already says who that is. It is provenance and
    never authorization evidence.
    """
    db.add(
        AuditEvent(
            id=ids.new_audit_id(),
            tenant_id=principal.tenant_id,
            actor_key_id=principal.key_id,
            on_behalf_of=on_behalf_of,
            action=action,
            resource=resource,
        )
    )
