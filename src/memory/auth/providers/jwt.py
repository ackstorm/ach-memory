"""Identity from an externally-issued, JWKS-verified JWT.

Trust is anchored to the signature and nothing else. No claim grants
authority -- operator status is configuration, read over the resolved
identity in `principal.authenticate` -- so no issuer can mint it by adding a
claim. The token says who the caller is, never what they are allowed to do.
"""

import logging
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, clean_groups
from memory.auth.provisioning import link_identity
from memory.config import get_settings
from memory.errors import Unauthorized

logger = logging.getLogger("memory.auth")

# ACH signs EdDSA, Dex signs RS256. An explicit list, never the token's own
# `alg`: honouring that is the algorithm-confusion attack, and "none" is in it.
ALGORITHMS = ["EdDSA", "RS256"]

_JWKS_CACHE_SECONDS = 300


@lru_cache
def _jwks_client() -> PyJWKClient:
    settings = get_settings()
    return PyJWKClient(
        settings.auth_jwt_jwks_uri, cache_jwk_set=True, lifespan=_JWKS_CACHE_SECONDS
    )


@lru_cache(maxsize=1024)
def _signing_key_for(token: str) -> Any:
    """Resolved separately so tests can substitute a key without a network."""
    return _jwks_client().get_signing_key_from_jwt(token).key


def looks_like_jwt(token: str) -> bool:
    """Whether this token claims to be a JWT, by its own structure.

    Parses only the JOSE header, verifies no signature and trusts no claim,
    so a forged header can only ever route a token to a provider that then
    refuses it -- it never grants anything.
    """
    try:
        jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return False
    return True


def authenticate(token: str, db: Session) -> Principal:
    settings = get_settings()
    options: dict[str, Any] = {
        "verify_aud": settings.auth_jwt_verify_audience,
        # Required, not merely verified-if-present: PyJWT accepts a token
        # with no `exp` at all, which would be valid forever.
        "require": ["exp"],
    }
    try:
        claims = jwt.decode(
            token,
            key=_signing_key_for(token),
            algorithms=ALGORITHMS,
            issuer=settings.auth_jwt_issuer,
            audience=settings.auth_jwt_audience if settings.auth_jwt_verify_audience else None,
            options=options,
        )
    except jwt.ExpiredSignatureError:
        raise Unauthorized("token expired") from None
    except jwt.PyJWTError as exc:
        # Logged with the reason, reported without it: a probe cannot use the
        # error message to discover which check it tripped.
        logger.warning("JWT validation failed: %s", exc)
        raise Unauthorized("token rejected") from None

    subject = claims.get("sub") or claims.get("email")
    if not isinstance(subject, str) or not subject:
        raise Unauthorized("token carries no usable subject")

    issuer = settings.auth_jwt_issuer
    user = link_identity(db, issuer=issuer, subject=subject)
    return Principal(
        user_id=user.id,
        subject=subject,
        groups=clean_groups(claims.get(settings.auth_jwt_groups_claim)),
        issuer=issuer,
    )
