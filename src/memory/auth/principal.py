from dataclasses import dataclass

from sqlalchemy.orm import Session

from memory.config import Settings, get_settings
from memory.errors import Unauthorized

BEARER = "bearer "


@dataclass(frozen=True, kw_only=True)
class Principal:
    """Who is calling, derived only from the credential (SPEC §2.3).

    Keyword-only, and that is load-bearing rather than style. The field list
    changed when authority stopped being a credential, and 22 positional
    constructions in one test file kept building silently: `False` landed in
    `groups` and a credential id in `subject`, so the principal was wrong in
    a way no type checker and no constructor could see. Naming every field
    turns that whole class of drift into an immediate TypeError.
    """

    tenant_id: str
    user_id: str | None
    #: Group ids asserted by an external identity provider (SPEC §5.3). The
    #: IdP is now the only source: membership is re-read from the credential
    #: on every request, so an IdP that stops asserting a group revokes
    #: access immediately, with no row anywhere to go stale.
    groups: frozenset[str] = frozenset()
    #: Stable identity of the *credential*, for rate limiting and audit:
    #: `ext_<hash>`, from `auth.provisioning.credential_id_for`. Every
    #: authenticated caller has one, because every caller is external --
    #: REQUIRED rather than defaulted so that stays true by construction.
    #: Optional, it reached `ratelimit.check` as None and indexed a
    #: `defaultdict`: no error, and every such caller silently sharing one
    #: anonymous bucket where SPEC §20 asks for one per credential.
    credential_id: str
    #: The external identity as its issuer names it -- an email, an opaque
    #: `sub`. This, NOT `user_id`, is what `MEMORY_MASTER_USERS` matches:
    #: `user_id` is minted locally by `link_identity` on first sight, so
    #: naming an operator by it would mean configuring an id that does not
    #: exist until after that operator's first login, and that nobody can
    #: predict. An operator is named by the identity their IdP asserts.
    subject: str | None = None
    #: Who vouched for `subject` -- the JWT issuer, or the platform resolver
    #: URL. `link_identity` already keys identity on (issuer, subject) because
    #: a subject is only unique within its issuer; operator matching has to
    #: qualify by the same thing, or two providers share one namespace.
    issuer: str | None = None
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

    Qualified by issuer when `MEMORY_MASTER_ISSUER` names one. Both providers
    can be enabled at once, and a caller chooses which one authenticates them
    simply by choosing which header to send -- send only the platform header
    and the JWT branch never runs. Without this, `MEMORY_MASTER_USERS=a@b.com`
    naming a JWT subject is also satisfied by anyone holding a platform
    credential whose resolver returns the literal string `a@b.com`, and the
    same for a group id against a `team_id`. A subject is only unique within
    the issuer that minted it.
    """
    if settings.master_issuer_value and principal.issuer != settings.master_issuer_value:
        return False
    return (
        (principal.subject is not None and principal.subject in settings.master_user_ids)
        or bool(principal.groups & settings.master_group_ids)
    )


def resolve_principal(
    authorization: str | None,
    db: Session,
    *,
    platform_token: str | None = None,
) -> Principal:
    """Authenticate the caller. The token's own shape names its provider.

    Every credential is issued elsewhere. This service mints none, stores
    none and verifies none of its own, so there is nothing to discriminate
    between on `Authorization` -- except the token itself, and a JWT says
    what it is: three dot-separated segments whose first decodes to a JOSE
    header. Anything else is opaque, and only the platform resolver can name
    it. Both providers can therefore be enabled at once, reading the same
    header, with no precedence rule to get wrong.

    Fail-closed, and this is the property the shape check buys: once shape
    picks the JWT provider, that decision is FINAL. A token that parses as a
    JWT and then fails validation is refused, never retried against the
    platform resolver -- a fall-through there would authenticate a bad
    credential as whoever the platform header names, which is a confused
    deputy that stays invisible until it matters.
    """
    settings = get_settings()
    token = _bearer_token(authorization)

    if token is not None and settings.auth_jwt_enabled:
        from memory.auth.providers import jwt_provider

        if jwt_provider.looks_like_jwt(token):
            return jwt_provider.authenticate(token, db)

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
    accepted = []
    if settings.auth_jwt_enabled:
        accepted.append("Authorization: Bearer <token>")
    if settings.auth_platform_enabled:
        accepted.extend(f"{header}: <key>" for header in settings.incoming_headers)
    # Names the header(s) THIS deployment reads: a fixed "Authorization:
    # Bearer" hint sent platform-only callers down a path that 401s (QA F-27).
    raise Unauthorized(
        "missing or malformed credential: send "
        + (" or ".join(accepted) or "a configured credential header")
    )


def _bearer_token(authorization: str | None) -> str | None:
    # RFC 7235 makes the auth scheme case-insensitive. `bearer <key>` used to
    # answer "missing or malformed Authorization header", indistinguishable
    # from a bad key.
    if not authorization or not authorization.lower().startswith(BEARER):
        return None
    token = authorization[len(BEARER) :].strip()
    return token or None
