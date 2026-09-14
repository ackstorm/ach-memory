"""Identity from a platform-issued API key, resolved over HTTP.

The fallback behind the JWT provider: a gateway forwards its own key rather
than a token we can verify offline, so identity comes from asking the
platform who the key belongs to.
"""

import logging
import time
from functools import lru_cache

import httpx
from sqlalchemy.orm import Session

from memory.auth.principal import Principal, clean_groups
from memory.auth.provisioning import link_identity
from memory.config import get_settings
from memory.errors import Unauthorized, UpstreamError

logger = logging.getLogger("memory.auth")

MAX_CACHE_ENTRIES = 1024
_TIMEOUT_SECONDS = 10.0

# {token: (user_id, groups, expires_at)}. Only successes land here -- caching
# a refusal would keep rejecting a key for its whole TTL after the platform
# re-enabled it.
_cache: dict[str, tuple[str, frozenset[str], float]] = {}


def reset_cache() -> None:
    """Test seam, and the only supported way to clear it."""
    _cache.clear()
    _client.cache_clear()


@lru_cache
def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT_SECONDS)


def _prune(now: float) -> None:
    for token in [t for t, (_, _, exp) in _cache.items() if now >= exp]:
        del _cache[token]
    overflow = len(_cache) - MAX_CACHE_ENTRIES
    for token in list(_cache)[:overflow] if overflow > 0 else []:
        del _cache[token]


def _dig(payload: dict, path: str):
    """Read a dotted path out of the resolver's JSON, failing closed to None."""
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _resolve(token: str) -> tuple[str, frozenset[str]]:
    settings = get_settings()
    now = time.time()
    cached = _cache.get(token)
    if cached is not None and now < cached[2]:
        return cached[0], cached[1]

    try:
        response = _client().get(
            settings.auth_platform_resolver_url,
            headers={settings.auth_platform_resolver_header: token},
        )
    except httpx.HTTPError as exc:
        logger.error("platform resolver unreachable: %s", exc)
        raise UpstreamError("could not reach the identity resolver") from None

    if response.status_code in (401, 403, 404):
        raise Unauthorized("unknown platform credential")
    if response.status_code != 200:
        logger.error("platform resolver returned %s", response.status_code)
        raise UpstreamError("the identity resolver is failing")

    try:
        payload = response.json()
    except ValueError:
        raise UpstreamError("the identity resolver returned no JSON") from None
    if not isinstance(payload, dict):
        raise UpstreamError("the identity resolver returned no object")

    subject = _dig(payload, settings.auth_platform_user_field)
    if not isinstance(subject, str) or not subject:
        # A 200 with nothing at the configured path means the key is valid
        # but anonymous -- refusing every such caller is the safe way to be
        # wrong about which field carries the identity.
        raise Unauthorized("the identity resolver named no user")

    groups = clean_groups(_dig(payload, settings.auth_platform_groups_field))
    _prune(now)
    _cache[token] = (subject, groups, now + settings.auth_platform_cache_ttl)
    return subject, groups


def authenticate(token: str, db: Session) -> Principal:
    settings = get_settings()
    subject, groups = _resolve(token)
    # The resolver URL is the issuer: two deployments resolving against
    # different platforms must not collapse the same subject into one user.
    issuer = settings.auth_platform_resolver_url
    user = link_identity(db, issuer=issuer, subject=subject)
    return Principal(user_id=user.id, subject=subject, groups=groups, issuer=issuer)
