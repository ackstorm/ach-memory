"""Who is calling, derived only from the credential (SPEC §3.2)."""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from memory.config import Settings, get_settings
from memory.errors import Forbidden, Unauthorized
from memory.identifiers import has_control_character

BEARER = "bearer "
MAX_GROUP_ID = 128


@dataclass(frozen=True, kw_only=True)
class Principal:
    """Keyword-only so a wrong field never lands silently in the wrong slot."""

    user_id: str
    subject: str = ""  # IdP-asserted identity; operators are configured by it, never by the minted user_id
    groups: frozenset[str] = frozenset()
    issuer: str | None = None
    is_operator: bool = False
    on_behalf_of: str | None = None


def clean_groups(values: object) -> frozenset[str]:
    """Group ids asserted by an IdP or platform resolver.

    Permissive by design: a scalar is wrapped as a one-element list because
    several providers emit a single group unwrapped, and anything unusable
    (wrong type, too long, a control character -- none of which can ever
    match Group.id) is dropped rather than refusing the whole credential.
    """
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return frozenset()
    return frozenset(
        v
        for v in values
        if isinstance(v, str) and v and len(v) <= MAX_GROUP_ID and not has_control_character(v)
    )


def _is_operator(principal: Principal, settings: Settings) -> bool:
    if settings.master_issuer and principal.issuer != settings.master_issuer:
        return False
    return principal.subject in settings.master_users or bool(
        principal.groups & set(settings.master_groups)
    )


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith(BEARER):
        return None
    token = authorization[len(BEARER) :].strip()
    return token or None


def _platform_token(headers: Mapping[str, str], settings: Settings) -> str | None:
    raw = headers.get(settings.auth_platform_incoming_header.lower())
    if raw is None:
        return None
    value = raw.strip()
    if value.lower().startswith(BEARER):
        value = value[len(BEARER) :].strip()
    return value or None


def authenticate(headers: Mapping[str, str], db: Session) -> Principal:
    """Authenticate the caller. The token's own shape names its provider.

    A JWT-shaped token is routed to the JWT provider and that choice is
    final -- one that then fails verification is refused, never retried
    against the platform resolver, which would authenticate a bad credential
    as whoever the platform header names.
    """
    from memory.auth.providers import jwt as jwt_provider
    from memory.auth.providers import platform as platform_provider

    settings = get_settings()
    headers = {k.lower(): v for k, v in headers.items()}
    token = _bearer_token(headers.get("authorization"))

    if settings.auth_jwt_enabled and token is not None and jwt_provider.looks_like_jwt(token):
        principal = jwt_provider.authenticate(token, db)
    elif settings.auth_platform_enabled:
        platform_token = _platform_token(headers, settings)
        if platform_token is None:
            raise Unauthorized("missing or malformed credential")
        principal = platform_provider.authenticate(platform_token, db)
    else:
        raise Unauthorized("no identity provider accepts this credential")

    is_operator = _is_operator(principal, settings)
    on_behalf_of = headers.get("on-behalf-of")
    if on_behalf_of is not None and not is_operator:
        raise Forbidden("only an operator may set on-behalf-of")
    return replace(principal, is_operator=is_operator, on_behalf_of=on_behalf_of)
