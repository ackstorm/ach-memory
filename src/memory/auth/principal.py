from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth import keys
from memory.config import get_settings
from memory.errors import Unauthorized
from memory.models import ApiKey

BEARER = "bearer "

#: Dedicated credential header. `Authorization` still works and is not going
#: away, but it is contested: anything fronting this service (LiteLLM, an API
#: gateway, ACH) has its own claim on `Authorization`, and whoever writes it
#: last wins. A caller that sends this header states unambiguously which
#: credential is meant for ach-memory.
API_KEY_HEADER = "x-ach-memory-key"


@dataclass(frozen=True)
class Principal:
    """Who is calling, derived only from the credential (SPEC §2.3)."""

    tenant_id: str
    user_id: str | None
    is_master: bool
    key_id: str | None
    #: Group ids asserted by an external identity provider (SPEC §5.3).
    #: Empty for a local key, whose membership lives in `group_members` and is
    #: read from the database instead. Never merged with the database set:
    #: `projects.authorize` consults both independently, so an IdP that stops
    #: asserting a group revokes it immediately without touching a row.
    groups: frozenset[str] = frozenset()
    #: Stable identity of the *credential*, for rate limiting and audit.
    #: `key_id` for a local key, `ext_<hash>` for an external identity (see
    #: `auth.provisioning.credential_id_for`). None only for the master key,
    #: which `ratelimit.check` buckets by On-Behalf-Of instead.
    #:
    #: This exists because `key_id` is None for every external caller, which
    #: silently dropped them all into the master's shared rate-limit bucket
    #: and wrote `actor_key_id=NULL` into every audit row -- both SPEC §20
    #: MUSTs, failing with no error.
    credential_id: str | None = None


def _credential(authorization: str | None, api_key: str | None) -> str:
    """The plaintext key, from the dedicated header or from `Authorization`.

    `x-ach-memory-key` is checked first and, once present, is the ONLY source
    considered — a malformed value raises rather than falling through. Falling
    through would mean a typo'd key silently authenticates as whoever
    `Authorization` happens to name, which is a confused deputy that stays
    invisible until it matters.
    """
    if api_key is not None:
        value = api_key.strip()
        # Tolerated, not documented. The neighbouring platform header
        # (`x-litellm-api-key`) *requires* a "Bearer " prefix, so pasting the
        # habit across is the likely mistake, and it would otherwise fail as
        # "unknown API key" — indistinguishable from a wrong key. Same
        # reasoning as the master_key_hash normaliser in config.py.
        if value.lower().startswith(BEARER):
            value = value[len(BEARER) :].strip()
        if not value:
            raise Unauthorized(f"malformed {API_KEY_HEADER} header")
        return value

    # RFC 7235 makes the auth scheme case-insensitive. `bearer <key>` used to
    # answer "missing or malformed Authorization header", indistinguishable
    # from a bad key.
    if not authorization or not authorization.lower().startswith(BEARER):
        raise Unauthorized(
            f"missing or malformed credential: send {API_KEY_HEADER} "
            "or Authorization: Bearer"
        )

    return authorization[len(BEARER) :].strip()


def resolve_principal(
    authorization: str | None, db: Session, *, api_key: str | None = None
) -> Principal:
    """Verify the caller's credential.

    `db` stays the second positional argument so every existing call site is
    unaffected; the new header arrives keyword-only.
    """
    plaintext = _credential(authorization, api_key)
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
