import logging
from functools import lru_cache

from pydantic import Field, model_validator
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


def _id_set(raw: str) -> frozenset[str]:
    """Comma-separated ids, with every empty entry discarded.

    The discard is the whole point, not tidiness. `"".split(",")` is `[""]`,
    so an unset MEMORY_MASTER_USERS would parse to a set containing the empty
    string -- and any principal whose user id or group id is empty would then
    match it. An unset variable would grant authority instead of withholding
    it, which is the one failure mode this configuration cannot have.
    """
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


class Settings(BaseSettings):
    """Service configuration. All variables use the MEMORY_ prefix."""

    model_config = SettingsConfigDict(env_prefix="MEMORY_", extra="ignore")

    database_url: str
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
    mcp_allowed_hosts: str = "127.0.0.1,localhost,127.0.0.1:*,localhost:*"

    # --- Operator authority ------------------------------------------------
    # Who, among the identities an external provider already resolved, also
    # holds operator authority: the audit log, bank clear and delete, slug
    # release, the fleet view, and On-Behalf-Of delegation. Comma-separated
    # user ids and group ids, matched against the resolved principal.
    #
    # Authority is no longer a credential, so nothing is minted and nothing
    # is stored. Both default to empty, which grants NOBODY -- see
    # `_id_set` for why an empty default is not on its own enough.
    master_users: str = ""
    master_groups: str = ""
    #: Which identity provider may grant operator authority, as its issuer:
    #: the JWT issuer URL, or the platform resolver URL. Required once more
    #: than one provider is enabled -- see `auth.principal.is_operator`.
    master_issuer: str = ""

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
    #: Which request header(s) carry the platform API key, comma-separated and
    #: tried in order -- the first one present on a request wins (see
    #: `incoming_headers`). One deployment serves two callers at once: the ACH
    #: gateway forwards the key as `x-litellm-api-key`, while a local stdio MCP
    #: client sends it as `Authorization: Bearer`. Listing both
    #: (`x-litellm-api-key,authorization`) lets the same service authenticate
    #: each without the caller having to know which header the other uses.
    #: `authorization` is matched only for a NON-JWT bearer: `resolve_principal`
    #: routes on shape first, so a JWT there still goes to the JWT provider.
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

    #: Minimum semantic similarity (cosine, 0-1) a recall hit must clear.
    #:
    #: Hindsight ranks but never abstains: it returns its whole candidate set
    #: however badly it scores, so a nonsense query came back with a page of
    #: confident-looking facts and nothing marking them as noise.
    #:
    #: On `semantic` and NOT on `final`/`reranker`, which was the first
    #: attempt and was wrong. Those two are excellent at ORDERING within one
    #: response and unusable as absolute thresholds: measured, a fact whose
    #: cross-encoder score was 0.000024 for one query scored 0.98 for
    #: another, and `make smoke` caught it -- "pins its Python tooling with
    #: uv, never with pip" was withheld from "how are Python dependencies
    #: managed", which is a reranker false negative a human would not make.
    #: Hindsight's own documentation warns about exactly this and the warning
    #: was under-weighted. `semantic` is a raw cosine similarity, so it means
    #: the same thing on every query and every bank size.
    #:
    #: Measured against benchmarks/corpus.jsonl (34 facts, 25 questions with
    #: their expected answers, plus 10 deliberately absurd queries). Scored
    #: per QUESTION, not per hit -- a question is answered if ANY of its
    #: expected facts clears the floor, so the number that matters is the
    #: BEST expected hit each question got:
    #:
    #: * best expected hit: min 0.6313, median 0.7884, max 0.8407
    #: * nonsense hits (n=680): median 0.4782, max 0.6355
    #:
    #: Those ranges still OVERLAP, by 0.004 -- the best nonsense outscores
    #: the weakest answered question -- so no value here abstains on every
    #: off-topic query without also blinding a real one. A threshold only
    #: picks which of the two errors to make.
    #:
    #: What each candidate floor cost, on that corpus:
    #:
    #:      floor   questions blinded   hits/question   nonsense hits left
    #:       0.00        0/25                    68.0           680/680
    #:       0.55        0/25                    31.0            61/680
    #:       0.60        0/25                    12.7             9/680
    #:       0.65        1/25                     5.4             0/680
    #:       0.70        2/25                     3.2             0/680
    #:
    #: 0.60 buys a 5x cut in answer size and removes 98.7% of the nonsense
    #: while blinding nothing the corpus can detect.
    #:
    #: Know which way this fails before moving it. Too low returns junk the
    #: caller can see and dismiss (every hit carries its `score`); too high
    #: makes a real memory silently unreachable and looks like data loss --
    #: which is why the safe direction is down. Two ceilings sit just above:
    #: `make smoke`'s fact scores 0.6135, so 0.62 breaks it, and 0.65 blinds
    #: "how should Python packages be installed" (its best expected hit
    #: scores 0.6313). Re-measure against the corpus before raising it, with
    #: scripts/../benchmarks. 0 disables the floor.
    recall_min_semantic: float = Field(default=0.60, ge=0, le=1)

    #: Drop hits scoring below this fraction of the BEST hit in the same
    #: response. `recall_min_semantic` answers "is anything here about the
    #: query at all"; this answers "how much of what came back is just tail".
    #:
    #: Both are needed and neither substitutes for the other, and the pairing
    #: of question to score is the point. On an absurd query every candidate
    #: is equally bad, so a ratio against the top one keeps them all and only
    #: the absolute floor abstains. On a good query the floor passes a long
    #: tail -- one measured response ran 1.08, 1.08, 0.75, 0.50 and then fell
    #: off a cliff to 0.013, 0.009, 0.003.
    #:
    #: On `final`, and relative, because `final` is the value hits are
    #: ORDERED by and is only meaningful against the other hits beside it: a
    #: ratio re-calibrates itself on every query and never has to be
    #: comparable across banks. Using it as an absolute threshold is exactly
    #: the mistake `recall_min_semantic` documents.
    #:
    #: 0.01 measured against the same corpus: it takes 5.0 hits per query
    #: down to 3.6 and costs nothing that was not already lost. Ratios from
    #: 0.05 up cost a whole question to save another 0.4 hits.
    #:
    #: Read independently of `recall_min_semantic`, not gated by it. The two
    #: answer different questions about a hit, and no caller ever asked for
    #: one of them to silently switch the other off. 0 disables the cut.
    recall_relative_cut: float = Field(default=0.01, ge=0, le=1)

    #: A hit the KEYWORD arm alone surfaced carries no `semantic` score, so
    #: the floor above cannot judge it. The cross-encoder can: `reranker` is
    #: its normalized 0-1 relevance, comparable across queries and banks.
    #: Measured in production 2026-09-11 (QA F-12): keyword-only noise sat at
    #: 0.003-0.013 while relevant hits scored 0.55-1.10. Applied ONLY when
    #: `semantic` is absent -- a semantically-surfaced hit the reranker
    #: dislikes is a known false negative of the reranker, not of the hit
    #: (see tests/test_recall_relevance_floor.py). Absent `reranker` (RRF
    #: passthrough) leaves the hit unjudged and kept. 0 disables.
    recall_keyword_only_min_reranker: float = Field(default=0.10, ge=0, le=1)

    write_limit: int = Field(default=60, ge=1)
    # gt=0 for the same reason write_limit has ge=1, and this one fails more
    # quietly: a window of 0 makes `cutoff = now - window` evict every hit
    # immediately, so the limiter never fires again. SPEC §20's MUST is
    # bypassed with no error and no log -- a silently disabled quota.
    write_window_seconds: float = Field(default=60.0, gt=0)

    # A creation is far more expensive than an ordinary write -- a real
    # project row, a Hindsight bank, a retain strategy and a built-in model --
    # so it gets its own, much tighter ceiling than write_limit above.
    project_creation_limit: int = Field(default=10, ge=1)
    project_creation_window_seconds: float = Field(default=3600.0, gt=0)

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
    def master_issuer_value(self) -> str:
        return self.master_issuer.strip()

    @property
    def incoming_headers(self) -> tuple[str, ...]:
        """The platform incoming headers, lower-cased and in priority order.

        Not a set: order is priority, so the gateway's `x-litellm-api-key` can
        be listed ahead of `authorization` and win when a request carries both.
        `_id_set` would dedupe but also drop order, so this splits directly and
        only discards empties (an unset var is "", which parses to no headers
        and the enabled-config check below rejects)."""
        seen: list[str] = []
        for part in self.auth_platform_incoming_header.split(","):
            header = part.strip().lower()
            if header and header not in seen:
                seen.append(header)
        return tuple(seen)

    @property
    def master_user_ids(self) -> frozenset[str]:
        return _id_set(self.master_users)

    @property
    def master_group_ids(self) -> frozenset[str]:
        return _id_set(self.master_groups)

    @property
    def jwt_audiences(self) -> list[str]:
        return [a.strip() for a in self.auth_jwt_audience.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
