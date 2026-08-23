from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth import keys
from memory.config import get_settings
from memory.errors import Unauthorized
from memory.models import ApiKey

BEARER = "bearer "


@dataclass(frozen=True)
class Principal:
    """Who is calling, derived only from the credential (SPEC §2.3)."""

    tenant_id: str
    user_id: str | None
    is_master: bool
    key_id: str | None


def resolve_principal(authorization: str | None, db: Session) -> Principal:
    # RFC 7235 makes the auth scheme case-insensitive. `bearer <key>` used to
    # answer "missing or malformed Authorization header", indistinguishable
    # from a bad key.
    if not authorization or not authorization.lower().startswith(BEARER):
        raise Unauthorized("missing or malformed Authorization header")

    plaintext = authorization[len(BEARER) :].strip()
    settings = get_settings()

    # The bootstrap master key is configuration, never a database row (§5.2).
    if keys.verify_key(plaintext, settings.master_key_hash):
        return Principal(
            tenant_id=settings.tenant_id, user_id=None, is_master=True, key_id=None
        )

    row = db.execute(
        select(ApiKey).where(ApiKey.secret_hash == keys.hash_key(plaintext))
    ).scalar_one_or_none()

    if row is None or row.status != "active":
        raise Unauthorized("unknown or revoked API key")

    # A stored key is always a user key: is_master is a constant here, never
    # derived from a column. Deriving it would mean a single bad row could
    # mint tenant-wide authority.
    return Principal(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        is_master=False,
        key_id=row.id,
    )
