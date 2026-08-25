"""Turn a verified external identity into a local user.

An IdP tells us who someone is; it cannot tell us where their memory lives.
`User.bank_id` is what makes memory exist (SPEC §19.2), so an externally
authenticated caller still needs a row here -- created once, on first sight,
and reused forever after through `external_identities`.
"""

import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import ids
from memory.db import ensure_tenant
from memory.errors import Unauthorized
from memory.models import ExternalIdentity, User

CREDENTIAL_PREFIX = "ext_"


def credential_id_for(issuer: str, subject: str) -> str:
    """A stable, opaque credential identity for audit and rate limiting.

    Hashed rather than composed, because `AuditEvent.actor_key_id` is
    String(64) and an issuer URL plus a subject routinely exceeds it -- a
    truncated actor is a wrong actor. The newline separator is not decoration:
    plain concatenation makes ("ab", "c") and ("a", "bc") the same credential,
    which would silently merge two identities' rate-limit buckets and audit
    trails. An issuer is a URL and a subject is an email or an opaque id, so
    neither can contain a newline to forge a collision with.
    """
    digest = hashlib.sha256(f"{issuer}\n{subject}".encode()).hexdigest()
    return f"{CREDENTIAL_PREFIX}{digest[:32]}"


def link_identity(
    db: Session, *, issuer: str, subject: str, tenant_id: str
) -> tuple[str, str]:
    """Return `(user_id, credential_id)` for an external identity.

    Creates the user and the link on first sight. Idempotent, and safe under
    concurrent first requests for the same identity: the loser of the race
    reloads the winner's row rather than surfacing an IntegrityError as a 500.
    """
    row = db.get(ExternalIdentity, (issuer, subject))
    if row is not None:
        if row.tenant_id != tenant_id:
            # Mono-tenant in v1, so this is unreachable today. It stays a hard
            # refusal rather than a silent re-link because re-pointing an
            # existing identity at another tenant would hand that tenant an
            # existing user's whole bank.
            raise Unauthorized("identity belongs to another tenant")
        return row.user_id, row.credential_id

    ensure_tenant(db, tenant_id)
    user_id = ids.new_user_id()
    credential_id = credential_id_for(issuer, subject)
    try:
        with db.begin_nested():
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    bank_id=ids.new_user_bank_id(),
                )
            )
            # Ordered explicitly, inside the savepoint. Neither mapper declares
            # a relationship() -- the FK lives on the column alone -- so the
            # unit of work has no dependency edge to sort on and falls back to
            # the alphabetical mapper sort key, which emits the
            # `external_identities` INSERT before `users` and trips
            # external_identities_user_id_fkey on every first sight.
            db.flush()
            db.add(
                ExternalIdentity(
                    issuer=issuer,
                    subject=subject,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    credential_id=credential_id,
                )
            )
    except IntegrityError:
        # Same shape as ensure_tenant and projects.create: losing the race is
        # success, the row exists either way.
        row = db.get(ExternalIdentity, (issuer, subject))
        if row is None:
            raise
        return row.user_id, row.credential_id

    return user_id, credential_id
