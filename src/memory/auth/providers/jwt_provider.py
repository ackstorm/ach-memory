"""Identity from an externally-issued, JWKS-verified JWT (SPEC §5.3).

Trust is anchored to the signature and nothing else. Every claim that could
grant authority -- tenant, master status -- is ignored: the tenant comes from
configuration and `is_master` is a constant here, exactly as it is for a stored
key, so no issuer can mint tenant-wide authority by adding a claim.
"""

import logging
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.auth.provisioning import link_identity
from memory.config import get_settings
from memory.errors import Unauthorized
from memory.identifiers import has_control_character

logger = logging.getLogger("memory.auth")

# Matches Group.id's column width. A longer value can never name a real group,
# and reaches the lazy-create path in projects._validate_owner as a psycopg
# DataError -- a 500, not a denial.
MAX_GROUP_ID = 128

# ACH signs EdDSA (ach/internal/forwarder/jwt/signer.go); Dex signs RS256.
# An explicit list, never the token's own `alg`: honouring that is the
# algorithm-confusion attack, and "none" is in it.
ALGORITHMS = ["EdDSA", "RS256"]

_JWKS_CACHE_SECONDS = 300


@lru_cache
def _jwks_client() -> PyJWKClient:
    settings = get_settings()
    return PyJWKClient(
        settings.auth_jwt_jwks_uri,
        cache_jwk_set=True,
        lifespan=_JWKS_CACHE_SECONDS,
    )


@lru_cache(maxsize=1024)
def _signing_key_for(token: str) -> Any:
    """Resolved separately so tests can substitute a key without a network.

    Cached by token: the JWKS fetch is the only I/O on this path, and a busy
    agent replays the same token for its whole lifetime.
    """
    return _jwks_client().get_signing_key_from_jwt(token).key


def _groups(claims: Mapping[str, object], claim_name: str) -> frozenset[str]:
    """Group ids asserted by the issuer.

    Permissive by design. A token that verifies must not be refused because
    its groups claim has an unexpected shape -- that would turn an IdP's
    schema change into a total outage for an otherwise valid identity. A
    scalar is accepted as a one-element list because several IdPs emit a
    single group unwrapped; anything unusable is dropped.
    """
    raw = claims.get(claim_name)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        value
        for value in raw
        if isinstance(value, str)
        and value
        and len(value) <= MAX_GROUP_ID
        and not has_control_character(value)
    )


def authenticate(token: str, db: Session) -> Principal:
    settings = get_settings()
    options: dict[str, Any] = {
        "verify_aud": settings.auth_jwt_verify_audience,
        # `exp` is required, not merely verified-if-present: PyJWT accepts a
        # token without one, which would make it valid forever.
        "require": ["exp"],
    }

    try:
        claims = jwt.decode(
            token,
            key=_signing_key_for(token),
            algorithms=ALGORITHMS,
            issuer=settings.auth_jwt_issuer,
            audience=settings.jwt_audiences if settings.auth_jwt_verify_audience else None,
            options=options,
        )
    except jwt.ExpiredSignatureError:
        raise Unauthorized("token expired") from None
    except jwt.PyJWTError as exc:
        # Logged with the reason, reported without it: the caller learns only
        # that authentication failed, so a probe cannot use the error message
        # to discover which check it tripped.
        logger.warning("JWT validation failed: %s", exc)
        raise Unauthorized("token rejected") from None

    subject = claims.get("sub") or claims.get("email")
    if not isinstance(subject, str) or not subject:
        raise Unauthorized("token carries no usable subject")

    issuer = settings.auth_jwt_issuer
    user_id, credential_id = link_identity(
        db, issuer=issuer, subject=subject, tenant_id=settings.tenant_id
    )
    return Principal(
        tenant_id=settings.tenant_id,
        user_id=user_id,
        is_master=False,
        key_id=None,
        groups=_groups(claims, settings.auth_jwt_groups_claim),
        credential_id=credential_id,
    )
