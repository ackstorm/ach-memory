from dataclasses import dataclass

from sqlalchemy.orm import Session

from memory.auth import keys
from memory.config import get_settings
from memory.errors import Unauthorized

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


def resolve_principal(
    authorization: str | None,
    db: Session,
    *,
    api_key: str | None = None,
    platform_token: str | None = None,
) -> Principal:
    """Authenticate the caller against every configured provider, in order.

    Fail-closed at each step: once a credential names a provider, that provider
    is the ONLY one consulted. A `mem_` key that does not verify is never
    retried as a JWT, and a JWT whose signature is bad is never downgraded to a
    key lookup or to the platform resolver. Falling through would mean a bad
    credential silently authenticates as whoever the *next* header names, which
    is a confused deputy that stays invisible until it matters.
    """
    from memory.auth.providers import local_key

    # 1. The dedicated header names the local credential unambiguously and is
    #    the only source considered once present (SPEC §5.1).
    if api_key is not None:
        return local_key.authenticate(_strip_bearer(api_key, API_KEY_HEADER), db)

    token = _bearer_token(authorization)

    # 2. A `mem_` prefix is a total discriminator: keys.generate_key()
    #    guarantees it, and a JWT -- three dot-separated base64url segments --
    #    can never produce it. So `Authorization` still carries local keys,
    #    which is not a convenience: codex and pi cannot send a custom header
    #    at all, and codex ignores a `headers` block silently rather than
    #    erroring (TODO.md, "What each host actually supports"). Reserving
    #    this header for JWTs would leave those two hosts unauthenticated with
    #    no error anywhere.
    if token is not None and token.startswith(keys.KEY_PREFIX):
        return local_key.authenticate(token, db)

    settings = get_settings()

    # 3. Anything else on Authorization is an externally-issued token.
    if token is not None and settings.auth_jwt_enabled:
        from memory.auth.providers import jwt_provider

        return jwt_provider.authenticate(token, db)

    # 4. The platform header is the documented fallback, reached only when
    #    Authorization carried nothing we could use.
    if platform_token and settings.auth_platform_enabled:
        from memory.auth.providers import platform

        return platform.authenticate(platform_token, db)

    if token is not None:
        raise Unauthorized(
            "no identity provider accepts this credential: it is not a "
            f"{keys.KEY_PREFIX} key and no external provider is enabled"
        )
    raise Unauthorized(
        f"missing or malformed credential: send {API_KEY_HEADER} "
        "or Authorization: Bearer"
    )


def _strip_bearer(value: str, header: str) -> str:
    """Tolerated, not documented. The neighbouring platform header
    (`x-litellm-api-key`) *requires* a "Bearer " prefix, so pasting the habit
    across is the likely mistake, and it would otherwise fail as "unknown API
    key" -- indistinguishable from a wrong key."""
    value = value.strip()
    if value.lower().startswith(BEARER):
        value = value[len(BEARER) :].strip()
    if not value:
        raise Unauthorized(f"malformed {header} header")
    return value


def _bearer_token(authorization: str | None) -> str | None:
    # RFC 7235 makes the auth scheme case-insensitive. `bearer <key>` used to
    # answer "missing or malformed Authorization header", indistinguishable
    # from a bad key.
    if not authorization or not authorization.lower().startswith(BEARER):
        return None
    token = authorization[len(BEARER) :].strip()
    return token or None
