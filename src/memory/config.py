from functools import lru_cache

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
    write_limit: int = 60
    write_window_seconds: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
