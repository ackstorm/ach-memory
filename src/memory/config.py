from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    """Service configuration. All variables use the MEMORY_ prefix."""

    model_config = SettingsConfigDict(env_prefix="MEMORY_", extra="ignore")

    database_url: str
    hindsight_url: str
    hindsight_api_key: str = ""
    hindsight_extraction_mode: Literal["verbatim", "concise", "verbose", "chunks"] = "verbatim"
    hindsight_timeout_seconds: float = Field(default=30.0, gt=0)
    hindsight_llm_timeout_seconds: float = Field(default=180.0, gt=0)

    # Which backend adapter serves the Backend ABC (backend/base.py).
    backend: Literal["hindsight", "fake"] = "hindsight"

    max_content_bytes: int = 256_000

    # The SDK's DNS-rebinding protection only allows 127.0.0.1 by default.
    mcp_allowed_hosts: _CsvList = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "127.0.0.1:*", "localhost:*"]
    )

    # Operator authority: identities an external provider already resolved.
    master_users: _CsvList = Field(default_factory=list)
    master_groups: _CsvList = Field(default_factory=list)
    master_issuer: str = ""

    auth_jwt_enabled: bool = False
    auth_jwt_issuer: str = ""
    auth_jwt_jwks_uri: str = ""
    auth_jwt_audience: str = ""
    auth_jwt_verify_audience: bool = True
    auth_jwt_groups_claim: str = "groups"

    auth_platform_enabled: bool = False
    auth_platform_incoming_header: str = ""
    auth_platform_resolver_header: str = ""
    auth_platform_resolver_url: str = ""
    auth_platform_cache_ttl: int = Field(default=300, ge=0)
    auth_platform_user_field: str = ""
    auth_platform_groups_field: str = ""

    # Cosine-similarity floor a recall hit must clear (see old config.py for
    # the corpus measurement this default is pinned to).
    recall_min_semantic: float = Field(default=0.60, ge=0, le=1)
    # Standing-context token budget, approximated as len(text) // 4.
    context_budget_chars: int = 16000

    @field_validator("mcp_allowed_hosts", "master_users", "master_groups", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return list(dict.fromkeys(p.strip() for p in value.split(",") if p.strip()))
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
