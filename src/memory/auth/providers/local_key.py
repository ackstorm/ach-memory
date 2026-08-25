"""The local credential: a `mem_` key from `api_keys`, or the master key.

Moved out of `principal.py` unchanged -- this is the provider the service
started with, and it stays the default and the only one enabled out of the box.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth import keys
from memory.auth.principal import Principal
from memory.config import get_settings
from memory.errors import Unauthorized
from memory.models import ApiKey


def authenticate(plaintext: str, db: Session) -> Principal:
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
        credential_id=row.id,
    )
