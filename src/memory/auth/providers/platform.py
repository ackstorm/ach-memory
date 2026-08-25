"""Identity from a platform-issued API key, resolved over HTTP (SPEC §5.3).

The fallback behind the JWT provider: LiteLLM forwards its own key rather than
a token we can verify offline, so identity comes from asking the platform who
the key belongs to. `alitellm-auth`'s /api/oauth/whoami answers `user_id` plus
`team_id`, which is where the single group comes from.
"""

import logging
import time
from functools import lru_cache

import httpx
from sqlalchemy.orm import Session

from memory.auth.principal import Principal
from memory.auth.provisioning import link_identity
from memory.config import get_settings
from memory.errors import AuthBackendUnavailable, Unauthorized
from memory.identifiers import has_control_character

logger = logging.getLogger("memory.auth")

MAX_GROUP_ID = 128
MAX_CACHE_ENTRIES = 1024
_TIMEOUT_SECONDS = 10.0

# {token: (user_id, groups, expires_at)}. Only successes land here -- caching a
# refusal would keep rejecting a key for the whole TTL after the platform
# re-enabled it.
_cache: dict[str, tuple[str, frozenset[str], float]] = {}


def reset_cache() -> None:
    """Test seam, and the only supported way to clear it."""
    _cache.clear()
    _client.cache_clear()


@lru_cache
def _client() -> httpx.Client:
    # Sync on purpose: `resolve_principal` is sync on both surfaces, FastAPI
    # runs a sync dependency in a threadpool, and the MCP pipeline is a sync
    # context manager. Making this async would tint both call chains for one
    # cached HTTP call.
    return httpx.Client(timeout=_TIMEOUT_SECONDS)


def _prune(now: float) -> None:
    for token in [t for t, (_, _, exp) in _cache.items() if now >= exp]:
        del _cache[token]
    overflow = len(_cache) - MAX_CACHE_ENTRIES
    for token in list(_cache)[:overflow] if overflow > 0 else []:
        del _cache[token]


def _groups(payload: dict, field: str) -> frozenset[str]:
    raw = payload.get(field)
    values = raw if isinstance(raw, list) else [raw]
    return frozenset(
        v
        for v in values
        if isinstance(v, str) and v and len(v) <= MAX_GROUP_ID and not has_control_character(v)
    )


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
        raise AuthBackendUnavailable("could not reach the identity resolver") from None

    if response.status_code in (401, 403, 404):
        raise Unauthorized("unknown platform credential")
    if response.status_code >= 500:
        logger.error("platform resolver returned %s", response.status_code)
        raise AuthBackendUnavailable("the identity resolver is failing")
    if response.status_code != 200:
        logger.error("platform resolver returned %s", response.status_code)
        raise Unauthorized("unknown platform credential")

    try:
        payload = response.json()
    except ValueError:
        raise AuthBackendUnavailable("the identity resolver returned no JSON") from None
    if not isinstance(payload, dict):
        raise AuthBackendUnavailable("the identity resolver returned no object")

    subject = payload.get("user_id")
    if not isinstance(subject, str) or not subject:
        # A 200 with no user_id is the platform telling us the key is valid but
        # anonymous. There is no identity to act as, so it cannot authenticate.
        raise Unauthorized("the identity resolver named no user")

    groups = _groups(payload, settings.auth_platform_groups_field)
    _prune(now)
    _cache[token] = (subject, groups, now + settings.auth_platform_cache_ttl)
    return subject, groups


def authenticate(token: str, db: Session) -> Principal:
    settings = get_settings()
    subject, groups = _resolve(token)
    # The resolver URL is the issuer: two deployments resolving against
    # different platforms must not collapse the same user_id into one person.
    user_id, credential_id = link_identity(
        db,
        issuer=settings.auth_platform_resolver_url,
        subject=subject,
        tenant_id=settings.tenant_id,
    )
    return Principal(
        tenant_id=settings.tenant_id,
        user_id=user_id,
        is_master=False,
        key_id=None,
        groups=groups,
        credential_id=credential_id,
    )
