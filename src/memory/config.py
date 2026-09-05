import logging
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("memory.config")


def _warn_if_plaintext(name: str, url: str) -> None:
    """Log, never refuse.

    These services run in Kubernetes and reach each other at
    http://name.ns.svc, which is the normal shape and never leaves the cluster
    network -- refusing it would block the actual deployment to defend against
    a threat the cluster already handles.

    It is still worth one line at startup, because these two URLs are not
    ordinary config. The JWKS endpoint decides which signatures this service
    trusts: anyone able to answer it can publish their own keys and mint
    tokens we accept. The resolver receives users' platform keys in a request
    header. Over plaintext on a network that is NOT trusted -- a public
    hostname reached by http because of a typo -- both are handed to whoever
    is on the path, silently. The warning is what makes that visible.
    """
    if not url.startswith("https://"):
        logger.warning("%s is not HTTPS: %s", name, url)


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

    # --- External identity (SPEC §5.3) ------------------------------------
    # Both providers may be enabled at once, and the deployed configuration
    # does exactly that: the JWT is primary and the platform header is the
    # fallback, which is how the same service serves ACH (which mints a JWT)
    # and LiteLLM (which forwards its own key).
    auth_jwt_enabled: bool = False
    auth_jwt_issuer: str = ""
    # Derived from the issuer when empty, which is what every IdP we target
    # publishes anyway. Kept overridable because Dex's discovery document
    # points at /keys, not /.well-known/jwks.json.
    auth_jwt_jwks_uri: str = ""
    # Comma-separated: one token may name the service reached directly and
    # the same service reached through a vmcp aggregator under another `aud`.
    auth_jwt_audience: str = ""
    # ON by default. Off means any token from the trusted issuer is accepted
    # regardless of who it was minted for, which permits cross-service token
    # replay between services that share an issuer.
    auth_jwt_verify_audience: bool = True
    # Which claim carries group membership. Dex emits `groups`; ACH does not
    # emit one yet, and an absent claim is simply no groups (never an error).
    auth_jwt_groups_claim: str = "groups"

    auth_platform_enabled: bool = False
    auth_platform_incoming_header: str = ""
    auth_platform_resolver_header: str = ""
    auth_platform_resolver_url: str = ""
    auth_platform_cache_ttl: int = Field(default=300, ge=0)
    # Where the identity and the groups live in the resolver's JSON. Both are
    # dotted paths, so a resolver that wraps its answer is addressable:
    # `data.user_id` reads {"data": {"user_id": ...}}. A key containing a
    # literal dot cannot be named -- the path splits on it.
    #
    # Neither has a default ON PURPOSE. There is no cross-platform standard
    # here: `alitellm-auth`'s /api/oauth/whoami answers a flat `team_id`,
    # LiteLLM's /v2/user/info answers `teams` as a list and no `team_id` at
    # all, and its /key/info wraps both under `info`. A default would be right
    # for one of them and silently wrong for the rest -- and wrong in the worst
    # direction, since a groups path that matches nothing authenticates the
    # caller anyway and just grants them no membership, reporting no error.
    # Requiring both turns that into a refusal to boot.
    auth_platform_user_field: str = ""
    auth_platform_groups_field: str = ""

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
    # Two upstream timeouts, not one. A cheap GET and `sync_retain` are not
    # the same call: sync_retain blocks until Hindsight has run fact
    # extraction through an LLM, and `reflect` is a full synthesis.
    # docs/PROJECT-STATE.md records one model as "works, slower" and another
    # as timing out outright, so a shared 30s ceiling turned a slow-but-
    # succeeding write into HINDSIGHT_ERROR (502) -- a code that means "retry"
    # to an agent, while the upstream worker finished the original write
    # anyway and the retry duplicated it.
    hindsight_timeout_seconds: float = Field(default=30.0, gt=0)
    hindsight_llm_timeout_seconds: float = Field(default=180.0, gt=0)

    write_limit: int = Field(default=60, ge=1)
    # gt=0 for the same reason write_limit has ge=1, and this one fails more
    # quietly: a window of 0 makes `cutoff = now - window` evict every hit
    # immediately, so the limiter never fires again. SPEC §20's MUST is
    # bypassed with no error and no log -- a silently disabled quota.
    write_window_seconds: float = Field(default=60.0, gt=0)

    # Observability. Metrics carry no identities, no project names and no
    # content -- only counts by action, scope, surface, outcome and error
    # code -- so the endpoint is unauthenticated, which is what a Prometheus
    # scrape config expects. The flag exists so a deployment can withdraw it
    # without a code change.
    metrics_enabled: bool = True
    admin_ui_enabled: bool = True
    # Activity rows are operational telemetry, not the audit trail: they age
    # out. 0 disables pruning entirely.
    activity_retention_days: int = Field(default=30, ge=0)

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

    @model_validator(mode="after")
    def _validate_auth_providers(self) -> "Settings":
        """Fail at startup, never at the first request.

        Every branch here turns a misconfiguration that would otherwise
        authenticate nobody -- or, worse, everybody -- into a container that
        refuses to start with the variable's name in the message.
        """
        if self.auth_jwt_enabled:
            if not self.auth_jwt_issuer:
                raise ValueError(
                    "MEMORY_AUTH_JWT_ISSUER is required when "
                    "MEMORY_AUTH_JWT_ENABLED=true"
                )
            if not self.auth_jwt_jwks_uri:
                self.auth_jwt_jwks_uri = (
                    self.auth_jwt_issuer.rstrip("/") + "/.well-known/jwks.json"
                )
            _warn_if_plaintext("MEMORY_AUTH_JWT_JWKS_URI", self.auth_jwt_jwks_uri)
            if self.auth_jwt_verify_audience:
                if not self.auth_jwt_audience:
                    raise ValueError(
                        "MEMORY_AUTH_JWT_AUDIENCE is required when "
                        "MEMORY_AUTH_JWT_ENABLED=true. To accept tokens "
                        "without checking the audience claim, set "
                        "MEMORY_AUTH_JWT_VERIFY_AUDIENCE=false (insecure: "
                        "permits cross-service token reuse)."
                    )
                # " , " is truthy but parses to no audience at all, and PyJWT
                # rejects every token against an empty list -- a total auth
                # outage from a typo, reported only as "expected []".
                if not self.jwt_audiences:
                    raise ValueError(
                        f"MEMORY_AUTH_JWT_AUDIENCE={self.auth_jwt_audience!r} "
                        "contains separators but no audience. Every token "
                        "would be rejected."
                    )

        if self.auth_platform_enabled:
            missing = [
                name
                for name, value in (
                    ("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", self.auth_platform_incoming_header),
                    ("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", self.auth_platform_resolver_header),
                    ("MEMORY_AUTH_PLATFORM_RESOLVER_URL", self.auth_platform_resolver_url),
                    ("MEMORY_AUTH_PLATFORM_USER_FIELD", self.auth_platform_user_field),
                    ("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", self.auth_platform_groups_field),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "Missing required vars when "
                    f"MEMORY_AUTH_PLATFORM_ENABLED=true: {', '.join(missing)}"
                )
            _warn_if_plaintext(
                "MEMORY_AUTH_PLATFORM_RESOLVER_URL", self.auth_platform_resolver_url
            )
        return self

    @property
    def jwt_audiences(self) -> list[str]:
        return [a.strip() for a in self.auth_jwt_audience.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
