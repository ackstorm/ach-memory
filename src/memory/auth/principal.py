from dataclasses import dataclass

from sqlalchemy.orm import Session

from memory.config import Settings, get_settings
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
    #: Group ids asserted by an external identity provider (SPEC §5.3). The
    #: IdP is now the only source: membership is re-read from the credential
    #: on every request, so an IdP that stops asserting a group revokes
    #: access immediately, with no row anywhere to go stale.
    groups: frozenset[str] = frozenset()
    #: Stable identity of the *credential*, for rate limiting and audit:
    #: `ext_<hash>`, from `auth.provisioning.credential_id_for`. Every
    #: authenticated caller has one, because every caller is external.
    credential_id: str | None = None
    #: The external identity as its issuer names it -- an email, an opaque
    #: `sub`. This, NOT `user_id`, is what `MEMORY_MASTER_USERS` matches:
    #: `user_id` is minted locally by `link_identity` on first sight, so
    #: naming an operator by it would mean configuring an id that does not
    #: exist until after that operator's first login, and that nobody can
    #: predict. An operator is named by the identity their IdP asserts.
    subject: str | None = None
    #: Surfaces that must not exercise operator authority set this to False.
    #: See `mcp/server.py`: authority bypasses ownership in `_resolve_bank`,
    #: and MCP has no On-Behalf-Of header to attribute the delegation to.
    authority_allowed: bool = True

    @property
    def is_master(self) -> bool:
        """Operator authority, derived rather than carried.

        A credential can no longer assert this. It is configuration read over
        an already-resolved external identity, so an operator is an ordinary
        user who also happens to be named in `MEMORY_MASTER_USERS` or to hold
        a group in `MEMORY_MASTER_GROUPS`.

        `authority_allowed` gates it because authority is a property of the
        surface as well as the identity: the same person is an operator over
        REST and an ordinary user over MCP.
        """
        return self.authority_allowed and is_operator(self, get_settings())


def is_operator(principal: Principal, settings: Settings) -> bool:
    """Whether configuration grants this principal operator authority.

    Takes the settings explicitly so the rule can be tested against a
    Settings object without reaching through the module-level cache.

    Matches on `subject` and on IdP-asserted `groups` -- both external
    namespaces an administrator can actually write into configuration. A
    principal with no subject (none exists today; every caller is external)
    can never match the user branch, rather than matching a configured empty
    string.
    """
    return (
        (principal.subject is not None and principal.subject in settings.master_user_ids)
        or bool(principal.groups & settings.master_group_ids)
    )


def resolve_principal(
    authorization: str | None,
    db: Session,
    *,
    api_key: str | None = None,
    platform_token: str | None = None,
) -> Principal:
    """Authenticate the caller against every configured provider, in order.

    Every credential is issued elsewhere. This service mints none, stores
    none and verifies none of its own, so there is nothing left to
    discriminate between on `Authorization` -- the `mem_` prefix went with
    the local keys it existed to tell apart.

    Fail-closed at each step: once a credential names a provider, that
    provider is the ONLY one consulted, and a JWT whose signature is bad is
    never downgraded to the platform resolver. Falling through would mean a
    bad credential silently authenticates as whoever the *next* header names,
    which is a confused deputy that stays invisible until it matters.
    """
    settings = get_settings()

    # 1. The dedicated header names the credential meant for THIS service and
    #    is the only source considered once present (SPEC §5.1). It carries a
    #    token, exactly like `Authorization`; the point is only that no proxy
    #    in front of us has a claim on this header's name.
    token = (
        _strip_bearer(api_key, API_KEY_HEADER)
        if api_key is not None
        else _bearer_token(authorization)
    )

    # 2. A token is an externally-issued JWT.
    if token is not None and settings.auth_jwt_enabled:
        from memory.auth.providers import jwt_provider

        return jwt_provider.authenticate(token, db)

    # 3. The platform header is the documented fallback, reached only when
    #    neither token header carried anything we could use.
    if platform_token and settings.auth_platform_enabled:
        from memory.auth.providers import platform

        return platform.authenticate(platform_token, db)

    if token is not None:
        # The only refusal left, so it carries the whole model: there is no
        # key here to be wrong, revoked or misspelled -- only an identity
        # provider that is not configured, or a token this deployment's
        # issuer did not mint.
        raise Unauthorized(
            "no identity provider accepts this credential. ach-memory mints "
            "no credentials of its own: the token must come from the "
            "configured JWT issuer or the platform that issued your key"
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
