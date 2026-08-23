from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration. All variables use the MEMORY_ prefix."""

    model_config = SettingsConfigDict(env_prefix="MEMORY_", extra="ignore")

    database_url: str
    master_key_hash: str
    hindsight_url: str
    hindsight_api_key: str = ""

    # Mono-tenant in v1. Scopes our own DB rows only -- never reaches
    # Hindsight, whose {tenant} path segment is pinned separately (see
    # hindsight.paths.HINDSIGHT_TENANT, SPEC-v1.md §19.1).
    tenant_id: str = "default"

    max_content_bytes: int = 256_000

    # Hosts the MCP endpoint will answer to. The SDK enables DNS-rebinding
    # protection by default and allows only 127.0.0.1, so a deployed service
    # behind any ingress answers 421 Misdirected Request to every MCP call
    # until its real hostname is listed here. Comma-separated.
    mcp_allowed_hosts: str = "127.0.0.1,localhost"

    # SPEC §20 MUST: rate-limit memory writes per credential (memory.ratelimit).
    # 60 writes per 60-second window -- generous enough that an ordinary
    # interactive coding session (a handful of retain/reflect calls a minute)
    # never sees it, while still turning an unbounded retain/reflect loop from
    # one key into a bounded 1-per-second worst case instead of no ceiling at
    # all.
    # ge=1: MEMORY_WRITE_LIMIT=0 is the natural spelling of "block all
    # writes" and instead made Limiter.check evaluate `len(hits) >= 0` as
    # True on an empty deque, then IndexError on `hits[0]` -- a 500 on every
    # write rather than the 429 the operator asked for.
    write_limit: int = Field(default=60, ge=1)
    # gt=0 for the same reason write_limit has ge=1, and this one fails more
    # quietly: a window of 0 makes `cutoff = now - window` evict every hit
    # immediately, so the limiter never fires again. SPEC §20's MUST is
    # bypassed with no error and no log -- a silently disabled quota.
    write_window_seconds: float = Field(default=60.0, gt=0)

    @field_validator("master_key_hash")
    @classmethod
    def _normalize_hash(cls, value: str) -> str:
        """A hex digest compared verbatim was a whole class of silent outage.

        `echo -n k | sha256sum` appends "  -"; a value read from a mounted
        Secret carries a trailing newline; PowerShell's Get-FileHash is
        uppercase. Each produced a master key that never authenticates,
        indistinguishable from a wrong key -- on the one credential whose
        failure blocks all provisioning. Normalizing once here removes the
        class; `keys.verify_key` still does the constant-time compare.
        """
        return value.strip().split()[0].lower() if value.strip() else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
