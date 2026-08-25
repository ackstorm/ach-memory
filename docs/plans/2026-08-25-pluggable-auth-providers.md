# Pluggable Authentication Providers Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let ach-memory authenticate a caller from an externally-issued JWT (ACH, Dex) or a platform API key (LiteLLM) in addition to its own `mem_` keys, taking user identity *and* group membership from the external assertion.

**Architecture:** `resolve_principal()` — today a single function reading one credential — becomes a fail-closed chain over three providers in `memory/auth/providers/`: local key, JWT, platform resolver. Each returns the same frozen `Principal`, which gains two fields: `groups` (asserted by the provider) and `credential_id` (stable per-credential identity for rate limiting and audit). Any combination of providers may be enabled at once. An external identity is mapped onto a local `User` row through a new `external_identities` table, because a `bank_id` is what makes memory exist and `(issuer, subject)` is the only globally unique name an IdP gives us.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Alembic, pydantic-settings, PyJWT (new: `pyjwt[crypto]`), httpx, pytest.

---

## Context the executing engineer needs

Read these before starting. They are short and they explain the constraints that make several tasks non-obvious.

- `SPEC-v1.md` §5 (credentials), §7 (authorization rules), §18 (the **closed** error-code list), §20 (rate limiting and audit MUSTs).
- `src/memory/auth/principal.py` — the whole of today's authentication, 97 lines.
- `src/memory/api/app.py:19-27` — the REST entry point; `src/memory/mcp/server.py:35-67` — the MCP entry point. These are the **only two** call sites.
- `TODO.md` → "What each host actually supports" — the agent capability matrix that dictates the header rules in Task 3.

### Five findings that shaped this plan

1. **`Authorization: Bearer` must keep accepting `mem_` keys.** Codex and pi cannot send a custom header at all (`TODO.md` matrix, `cli.py:241`, `cli.py:303`). Codex *silently ignores* a `headers` block, so every call would go out unauthenticated with no error. Nothing in the repo sends `x-ach-memory-key` today. The two credential namespaces are separated by the `mem_` prefix instead (`keys.py:5`), which is a total function with no ambiguous input — a JWT is three dot-separated base64url segments and can never produce that prefix.

2. **`sub` is not a usable `User.id`.** ACH's `sub` is a bare email (`../ach/internal/forwarder/jwt/signer.go:46`); Dex's is an opaque identifier. A master-provisioned user is `usr_<uuid>` (`ids.py:4`). Without a mapping table the same human gets two `User` rows, two `bank_id`s, and their memory silently splits in half. Hence Task 4.

3. **Rate limiting and audit both key off `principal.key_id`,** which is `None` for any external caller. `ratelimit.py:94-99` would then drop every JWT user into the *master* bucket — one shared 60-writes/minute ceiling for the entire fleet — and `audit.py:31` would write `actor_key_id=NULL`, indistinguishable from a master action. Both are SPEC §20 MUSTs. Hence `credential_id`, Task 2 and Task 10.

4. **Reading needs only `Principal.groups`, but *owning* needs a real `Group` row.** `projects.py:107` authorizes against `group_members`, while `projects.py:77` validates ownership against the `groups` table and raises `GroupNotFound`. So group rows are created lazily, at the one place that requires them, instead of on every request (Task 9).

5. **§18 is a closed list with a two-way conformance test.** `tests/test_errors.py` compares `vars(errors)` against SPEC §18's first fenced block in both directions. Adding `AuthBackendUnavailable` therefore *requires* the SPEC edit in the same commit or the suite fails.

### Testing posture

The test blocks below are a **menu, not a quota**. Each task keeps the smallest
set that would actually fail if the logic broke — typically the happy path plus
the one or two failure modes that motivated the design (a fail-closed branch, a
race, a bound). Drop the rest; exhaustive per-branch coverage is not the goal
here and slows every task down for little signal.

Per-task verification is likewise light: run the test file you touched, plus
`ruff`. The full suite, `make verify` and `scripts/smoke.sh` run **once**, at
Task 11 — not after every task.

### Conventions in this codebase

- Settings use the `MEMORY_` prefix (`config.py:10`). `MEMORY_AUTH_JWT_ENABLED`, etc.
- Every `DomainError` lives in `errors.py` — never declare one elsewhere (`errors.py:54-61` records why).
- Provisioning writes use `with db.begin_nested():` and treat `IntegrityError` as "someone else won the race, reload" (`db.py:34-47`, `projects.py`).
- Tests: `uv run pytest -m "not integration" -q`. Full gate: `make verify` (ruff + tests + gitleaks + helm lint).
- Postgres must be up: `make up`. Tests use a separate `memory_test` database, auto-created by `tests/conftest.py`.

---

## Task 1: Authentication settings

**Files:**
- Modify: `src/memory/config.py`
- Modify: `pyproject.toml` (add `pyjwt[crypto]`)
- Test: `tests/test_config.py`

**Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def _base_env(monkeypatch):
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", "abc")
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://localhost:8888")


def test_jwt_disabled_by_default(monkeypatch):
    from memory.config import Settings

    _base_env(monkeypatch)
    assert Settings().auth_jwt_enabled is False


def test_jwt_jwks_uri_derived_from_issuer(monkeypatch):
    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://ach.example.com")
    monkeypatch.setenv("MEMORY_AUTH_JWT_VERIFY_AUDIENCE", "false")
    assert Settings().auth_jwt_jwks_uri == (
        "https://ach.example.com/.well-known/jwks.json"
    )


def test_jwt_requires_issuer(monkeypatch):
    import pytest

    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    with pytest.raises(ValueError, match="MEMORY_AUTH_JWT_ISSUER"):
        Settings()


def test_jwt_requires_audience_unless_verification_is_off(monkeypatch):
    import pytest

    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://ach.example.com")
    with pytest.raises(ValueError, match="MEMORY_AUTH_JWT_AUDIENCE"):
        Settings()


def test_audience_of_separators_only_is_rejected(monkeypatch):
    import pytest

    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://ach.example.com")
    monkeypatch.setenv("MEMORY_AUTH_JWT_AUDIENCE", " , ")
    with pytest.raises(ValueError, match="no audience"):
        Settings()


def test_an_in_cluster_plaintext_issuer_is_accepted(monkeypatch, caplog):
    """Kubernetes service URLs are the normal deployment shape: most of these
    services are reached at http://name.ns.svc and never leave the cluster
    network. Accepted, but logged -- see the WARNING below."""
    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "http://dex.auth.svc")
    monkeypatch.setenv("MEMORY_AUTH_JWT_VERIFY_AUDIENCE", "false")
    settings = Settings()
    assert settings.auth_jwt_jwks_uri == "http://dex.auth.svc/.well-known/jwks.json"


def test_a_plaintext_jwks_uri_is_logged(monkeypatch, caplog):
    import logging

    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "http://dex.auth.svc")
    monkeypatch.setenv("MEMORY_AUTH_JWT_VERIFY_AUDIENCE", "false")
    with caplog.at_level(logging.WARNING, logger="memory.config"):
        Settings()
    assert "not HTTPS" in caplog.text


def test_an_https_jwks_uri_is_not_logged(monkeypatch, caplog):
    import logging

    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://ach.example.com")
    monkeypatch.setenv("MEMORY_AUTH_JWT_VERIFY_AUDIENCE", "false")
    with caplog.at_level(logging.WARNING, logger="memory.config"):
        Settings()
    assert caplog.text == ""


def test_a_plaintext_resolver_url_is_accepted(monkeypatch):
    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "x-litellm-api-key")
    monkeypatch.setenv(
        "MEMORY_AUTH_PLATFORM_RESOLVER_URL", "http://litellm.genai.svc/v2/user/info"
    )
    assert Settings().auth_platform_enabled is True


def test_platform_requires_all_three_vars(monkeypatch):
    import pytest

    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    with pytest.raises(ValueError, match="RESOLVER_HEADER.*RESOLVER_URL"):
        Settings()


def test_both_providers_may_be_enabled_together(monkeypatch):
    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://ach.example.com")
    monkeypatch.setenv("MEMORY_AUTH_JWT_AUDIENCE", "mcp:memory")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "x-litellm-api-key")
    monkeypatch.setenv(
        "MEMORY_AUTH_PLATFORM_RESOLVER_URL", "https://api.example.com/v2/user/info"
    )
    settings = Settings()
    assert settings.auth_jwt_enabled and settings.auth_platform_enabled
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_config.py -q
```
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'auth_jwt_enabled'`.

**Step 3: Implement**

In `src/memory/config.py`, add the imports and the fields. Place them after `mcp_allowed_hosts`:

```python
import logging

from pydantic import Field, field_validator, model_validator

logger = logging.getLogger("memory.config")
```

```python
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
    # LiteLLM's key metadata carries one team, not a list -- `alitellm-auth`'s
    # /api/oauth/whoami returns `team_id` alongside `user_id`. A scalar here
    # becomes a one-element group set.
    auth_platform_groups_field: str = "team_id"
```

Add the validator after `_normalize_hash`:

```python
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
```

And the module-level helper above `class Settings`:

```python
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
```

In `pyproject.toml`, add to `[project].dependencies`:

```toml
    # Verifies externally-issued identity tokens (memory/auth/providers/jwt.py).
    # [crypto] is not optional for us: ACH signs EdDSA and Dex signs RS256,
    # and PyJWT without the extra supports neither -- it raises
    # "Algorithm 'EdDSA' could not be found" at the first request, not at
    # import, so a missing extra ships as a runtime auth outage.
    "pyjwt[crypto]>=2.10",
```

**Step 4: Run tests to verify they pass**

```bash
uv sync
uv run pytest tests/test_config.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/config.py tests/test_config.py pyproject.toml uv.lock
git commit -m "feat(auth): configure the JWT and platform identity providers"
```

---

## Task 2: `Principal` carries groups and a credential identity

Purely additive. Both fields default, so every existing construction site and test keeps working unchanged.

**Files:**
- Modify: `src/memory/auth/principal.py:21-28`
- Test: `tests/test_principal.py`

**Step 1: Write the failing tests**

```python
def test_principal_defaults_to_no_groups(session, tenant):
    user, plaintext = _make_user_key(session, tenant)
    principal = resolve_principal(f"Bearer {plaintext}", session)
    assert principal.groups == frozenset()


def test_local_key_credential_id_is_its_key_id(session, tenant):
    user, plaintext = _make_user_key(session, tenant)
    principal = resolve_principal(f"Bearer {plaintext}", session)
    assert principal.credential_id == principal.key_id
    assert principal.credential_id is not None


def test_master_has_no_credential_id(session):
    principal = resolve_principal(f"Bearer {MASTER_PLAINTEXT}", session)
    assert principal.is_master
    assert principal.credential_id is None
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/test_principal.py -q
```
Expected: FAIL — `AttributeError: 'Principal' object has no attribute 'groups'`.

**Step 3: Implement**

In `src/memory/auth/principal.py`:

```python
@dataclass(frozen=True)
class Principal:
    """Who is calling, derived only from the credential (SPEC §2.3)."""

    tenant_id: str
    user_id: str | None
    is_master: bool
    key_id: str | None
    #: Group ids asserted by an external identity provider (SPEC §5.3).
    #: Empty for a local key, whose membership lives in `group_members` and is
    #: read from the database instead. Never merged with the database set:
    #: `projects.authorize` consults both independently, so an IdP that stops
    #: asserting a group revokes it immediately without touching a row.
    groups: frozenset[str] = frozenset()
    #: Stable identity of the *credential*, for rate limiting and audit.
    #: `key_id` for a local key, `ext_<hash>` for an external identity (see
    #: `auth.provisioning.credential_id_for`). None only for the master key,
    #: which `ratelimit.check` buckets by On-Behalf-Of instead.
    #:
    #: This exists because `key_id` is None for every external caller, which
    #: silently dropped them all into the master's shared rate-limit bucket
    #: and wrote `actor_key_id=NULL` into every audit row -- both SPEC §20
    #: MUSTs, failing with no error.
    credential_id: str | None = None
```

In the same file, set it on the local-key return (the master branch keeps `credential_id=None`):

```python
    return Principal(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        is_master=False,
        key_id=row.id,
        credential_id=row.id,
    )
```

**Step 4: Verify**

```bash
uv run pytest tests/test_principal.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/auth/principal.py tests/test_principal.py
git commit -m "feat(auth): give Principal asserted groups and a credential identity"
```

---

## Task 3: Extract the local-key provider and split the two credential namespaces

Behaviour-preserving refactor **plus** one new rule: `Authorization: Bearer <non-mem_>` stops being treated as a local key.

**Files:**
- Create: `src/memory/auth/providers/__init__.py` (empty)
- Create: `src/memory/auth/providers/local_key.py`
- Modify: `src/memory/auth/principal.py`
- Test: `tests/test_principal.py`

**Step 1: Write the failing tests**

```python
def test_a_non_mem_bearer_is_not_tried_as_a_local_key(session, tenant):
    """With no external provider configured, a JWT-shaped token is a 401 that
    says so -- never a database lookup that reports 'unknown API key'."""
    with pytest.raises(Unauthorized, match="no identity provider"):
        resolve_principal("Bearer eyJhbGciOiJFZERTQSJ9.e30.sig", session)


def test_mem_prefixed_bearer_still_resolves(session, tenant):
    user, plaintext = _make_user_key(session, tenant)
    assert plaintext.startswith("mem_")
    principal = resolve_principal(f"Bearer {plaintext}", session)
    assert principal.user_id == user.id


def test_dedicated_header_still_wins_over_authorization(session, tenant):
    user, plaintext = _make_user_key(session, tenant)
    principal = resolve_principal("Bearer mem_wrong", session, api_key=plaintext)
    assert principal.user_id == user.id
```

**Step 2: Run to verify failure**

Expected: the first test FAILS with `Unauthorized: unknown or revoked API key` instead of the new message.

**Step 3: Implement**

Create `src/memory/auth/providers/local_key.py` by moving the existing body verbatim:

```python
"""The local credential: a `mem_` key from `api_keys`, or the master key.

Moved out of `principal.py` unchanged -- this is the provider the service
started with, and it stays the default and the only one enabled out of the box.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory.auth import keys
from memory.auth.principal import Principal
from memory.config import get_settings
from memory.errors import Unauthorized
from memory.models import ApiKey


def authenticate(plaintext: str, db: Session) -> Principal:
    settings = get_settings()

    # The bootstrap master key is configuration, never a database row (§5.2).
    if keys.verify_key(plaintext, settings.master_key_hash):
        return Principal(
            tenant_id=settings.tenant_id, user_id=None, is_master=True, key_id=None
        )

    row = db.execute(
        select(ApiKey).where(ApiKey.secret_hash == keys.hash_key(plaintext))
    ).scalar_one_or_none()

    if row is None or row.status != "active":
        raise Unauthorized("unknown or revoked API key")

    # A stored key is always a user key: is_master is a constant here, never
    # derived from a column. Deriving it would mean a single bad row could
    # mint tenant-wide authority.
    return Principal(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        is_master=False,
        key_id=row.id,
        credential_id=row.id,
    )
```

> **Import-cycle warning:** `local_key.py` imports `Principal` from `principal.py`, and `principal.py` will import `local_key`. Import the provider **inside** `resolve_principal`'s body, not at module top level. The same applies to every provider in Tasks 6 and 7.

Rewrite `principal.py`'s `_credential`/`resolve_principal` region:

```python
def resolve_principal(
    authorization: str | None,
    db: Session,
    *,
    api_key: str | None = None,
    platform_token: str | None = None,
) -> Principal:
    """Authenticate the caller against every configured provider, in order.

    Fail-closed at each step: once a credential names a provider, that provider
    is the ONLY one consulted. A `mem_` key that does not verify is never
    retried as a JWT, and a JWT whose signature is bad is never downgraded to a
    key lookup or to the platform resolver. Falling through would mean a bad
    credential silently authenticates as whoever the *next* header names, which
    is a confused deputy that stays invisible until it matters.
    """
    from memory.auth.providers import local_key

    # 1. The dedicated header names the local credential unambiguously and is
    #    the only source considered once present (SPEC §5.1).
    if api_key is not None:
        return local_key.authenticate(_strip_bearer(api_key, API_KEY_HEADER), db)

    token = _bearer_token(authorization)

    # 2. A `mem_` prefix is a total discriminator: keys.generate_key()
    #    guarantees it, and a JWT -- three dot-separated base64url segments --
    #    can never produce it. So `Authorization` still carries local keys,
    #    which is not a convenience: codex and pi cannot send a custom header
    #    at all, and codex ignores a `headers` block silently rather than
    #    erroring (TODO.md, "What each host actually supports"). Reserving
    #    this header for JWTs would leave those two hosts unauthenticated with
    #    no error anywhere.
    if token is not None and token.startswith(keys.KEY_PREFIX):
        return local_key.authenticate(token, db)

    settings = get_settings()

    # 3. Anything else on Authorization is an externally-issued token.
    if token is not None and settings.auth_jwt_enabled:
        from memory.auth.providers import jwt_provider

        return jwt_provider.authenticate(token, db)

    # 4. The platform header is the documented fallback, reached only when
    #    Authorization carried nothing we could use.
    if platform_token and settings.auth_platform_enabled:
        from memory.auth.providers import platform

        return platform.authenticate(platform_token, db)

    if token is not None:
        raise Unauthorized(
            "no identity provider accepts this credential: it is not a "
            f"{keys.KEY_PREFIX} key and no external provider is enabled"
        )
    raise Unauthorized(
        f"missing or malformed credential: send {API_KEY_HEADER} "
        "or Authorization: Bearer"
    )


def _strip_bearer(value: str, header: str) -> str:
    """Tolerated, not documented. The neighbouring platform header
    (`x-litellm-api-key`) *requires* a "Bearer " prefix, so pasting the habit
    across is the likely mistake, and it would otherwise fail as "unknown API
    key" -- indistinguishable from a wrong key."""
    value = value.strip()
    if value.lower().startswith(BEARER):
        value = value[len(BEARER) :].strip()
    if not value:
        raise Unauthorized(f"malformed {header} header")
    return value


def _bearer_token(authorization: str | None) -> str | None:
    # RFC 7235 makes the auth scheme case-insensitive. `bearer <key>` used to
    # answer "missing or malformed Authorization header", indistinguishable
    # from a bad key.
    if not authorization or not authorization.lower().startswith(BEARER):
        return None
    token = authorization[len(BEARER) :].strip()
    return token or None
```

Add `from memory.auth import keys` and `from memory.config import get_settings` to the imports; drop the now-unused `select`/`ApiKey` imports.

**Step 4: Verify**

```bash
uv run pytest tests/test_principal.py tests/test_app.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/auth tests/test_principal.py
git commit -m "refactor(auth): extract the local-key provider behind a fail-closed chain"
```

---

## Task 4: `external_identities` — map an IdP subject onto a local user

**Files:**
- Modify: `src/memory/models.py`
- Create: `migrations/versions/<rev>_external_identities.py`
- Test: `tests/test_models.py`

**Step 1: Write the failing test**

```python
def test_external_identity_is_unique_per_issuer_and_subject(session, tenant):
    from sqlalchemy.exc import IntegrityError

    from memory import ids
    from memory.models import ExternalIdentity, User

    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()

    def _row(credential_id):
        return ExternalIdentity(
            issuer="https://ach.example.com",
            subject="alice@example.com",
            tenant_id=tenant,
            user_id=user.id,
            credential_id=credential_id,
        )

    session.add(_row("ext_aaa"))
    session.flush()
    session.add(_row("ext_bbb"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_same_subject_from_two_issuers_stays_two_identities(session, tenant):
    from memory import ids
    from memory.models import ExternalIdentity, User

    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    for issuer, credential in (
        ("https://ach.example.com", "ext_a"),
        ("https://auth.example.com", "ext_b"),
    ):
        session.add(
            ExternalIdentity(
                issuer=issuer,
                subject="alice@example.com",
                tenant_id=tenant,
                user_id=user.id,
                credential_id=credential,
            )
        )
    session.flush()  # no constraint violation
```

**Step 2: Run to verify failure**

Expected: `ImportError: cannot import name 'ExternalIdentity'`.

**Step 3: Implement**

Add to `src/memory/models.py`, after `ApiKey`:

```python
class ExternalIdentity(Base):
    __tablename__ = "external_identities"

    # (issuer, subject) is the only globally unique name an IdP gives us, and
    # neither half works alone as a User.id. ACH's `sub` is a bare owner email
    # (ach/internal/forwarder/jwt/signer.go); Dex's is an opaque identifier;
    # a master-provisioned user is `usr_<uuid>` from ids.py. Keying on the
    # subject alone would collapse the same string from two issuers into one
    # person, and keying on nothing would mint a second User -- and a second
    # bank_id -- on every request, silently splitting one human's memory in
    # half with no error anywhere.
    issuer: Mapped[str] = mapped_column(String(256), primary_key=True)
    subject: Mapped[str] = mapped_column(String(256), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # Width matches AuditEvent.actor_key_id, which stores this value for an
    # external caller: that column now holds either an `key_`-prefixed
    # api_keys.id or an `ext_`-prefixed credential from here, and this row is
    # what resolves the latter back to a human. The prefixes are disjoint by
    # construction (ids.py), so a reader can always tell which namespace it
    # is looking at.
    credential_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
```

Generate the migration:

```bash
make up
uv run alembic revision --autogenerate -m "external identities"
```

Review the generated file. `down_revision` must be `'ba9ea9cf7347'` (the current head). It should contain only `op.create_table("external_identities", ...)` plus its indexes — if autogenerate proposes anything else, delete those lines; they are drift, not part of this change.

**Step 4: Verify**

```bash
uv run alembic upgrade head
uv run pytest tests/test_models.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/models.py migrations/versions/ tests/test_models.py
git commit -m "feat(auth): map external identities onto local users"
```

---

## Task 5: Provisioning — create or link a user on first sight

**Files:**
- Create: `src/memory/auth/provisioning.py`
- Test: `tests/test_provisioning.py`

**Step 1: Write the failing tests**

```python
import pytest

from memory.auth import provisioning
from memory.models import ExternalIdentity, User


def test_credential_id_is_stable_bounded_and_prefixed():
    a = provisioning.credential_id_for("https://ach.example.com", "alice@example.com")
    b = provisioning.credential_id_for("https://ach.example.com", "alice@example.com")
    assert a == b
    assert a.startswith("ext_")
    assert len(a) <= 64


def test_credential_id_cannot_be_confused_across_the_separator():
    """Concatenating issuer+subject without a separator made ("ab", "c") and
    ("a", "bc") the same credential -- one identity's rate-limit bucket and
    audit rows silently shared with another's."""
    assert provisioning.credential_id_for("ab", "c") != provisioning.credential_id_for(
        "a", "bc"
    )


def test_first_sight_creates_a_user_with_its_own_bank(session, tenant):
    user_id, credential_id = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    user = session.get(User, user_id)
    assert user is not None and user.bank_id
    assert credential_id.startswith("ext_")


def test_second_sight_returns_the_same_user(session, tenant):
    first = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    second = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    assert first == second
    assert session.query(User).count() == 1


def test_the_same_subject_from_another_issuer_is_another_user(session, tenant):
    alice_ach, _ = provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    alice_dex, _ = provisioning.link_identity(
        session, issuer="https://auth.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    assert alice_ach != alice_dex


def test_an_identity_from_another_tenant_is_refused(session, tenant):
    from memory.errors import Unauthorized

    provisioning.link_identity(
        session, issuer="https://ach.example.com", subject="alice@example.com",
        tenant_id=tenant,
    )
    with pytest.raises(Unauthorized):
        provisioning.link_identity(
            session, issuer="https://ach.example.com", subject="alice@example.com",
            tenant_id="other",
        )
```

**Step 2: Run to verify failure**

Expected: `ModuleNotFoundError: No module named 'memory.auth.provisioning'`.

**Step 3: Implement**

```python
"""Turn a verified external identity into a local user.

An IdP tells us who someone is; it cannot tell us where their memory lives.
`User.bank_id` is what makes memory exist (SPEC §19.2), so an externally
authenticated caller still needs a row here -- created once, on first sight,
and reused forever after through `external_identities`.
"""

import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory import ids
from memory.db import ensure_tenant
from memory.errors import Unauthorized
from memory.models import ExternalIdentity, User

CREDENTIAL_PREFIX = "ext_"


def credential_id_for(issuer: str, subject: str) -> str:
    """A stable, opaque credential identity for audit and rate limiting.

    Hashed rather than composed, because `AuditEvent.actor_key_id` is
    String(64) and an issuer URL plus a subject routinely exceeds it -- a
    truncated actor is a wrong actor. The newline separator is not decoration:
    plain concatenation makes ("ab", "c") and ("a", "bc") the same credential,
    which would silently merge two identities' rate-limit buckets and audit
    trails. An issuer is a URL and a subject is an email or an opaque id, so
    neither can contain a newline to forge a collision with.
    """
    digest = hashlib.sha256(f"{issuer}\n{subject}".encode()).hexdigest()
    return f"{CREDENTIAL_PREFIX}{digest[:32]}"


def link_identity(
    db: Session, *, issuer: str, subject: str, tenant_id: str
) -> tuple[str, str]:
    """Return `(user_id, credential_id)` for an external identity.

    Creates the user and the link on first sight. Idempotent, and safe under
    concurrent first requests for the same identity: the loser of the race
    reloads the winner's row rather than surfacing an IntegrityError as a 500.
    """
    row = db.get(ExternalIdentity, (issuer, subject))
    if row is not None:
        if row.tenant_id != tenant_id:
            # Mono-tenant in v1, so this is unreachable today. It stays a hard
            # refusal rather than a silent re-link because re-pointing an
            # existing identity at another tenant would hand that tenant an
            # existing user's whole bank.
            raise Unauthorized("identity belongs to another tenant")
        return row.user_id, row.credential_id

    ensure_tenant(db, tenant_id)
    user_id = ids.new_user_id()
    credential_id = credential_id_for(issuer, subject)
    try:
        with db.begin_nested():
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    bank_id=ids.new_user_bank_id(),
                )
            )
            db.add(
                ExternalIdentity(
                    issuer=issuer,
                    subject=subject,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    credential_id=credential_id,
                )
            )
    except IntegrityError:
        # Same shape as ensure_tenant and projects.create: losing the race is
        # success, the row exists either way.
        row = db.get(ExternalIdentity, (issuer, subject))
        if row is None:
            raise
        return row.user_id, row.credential_id

    return user_id, credential_id
```

**Step 4: Verify**

```bash
uv run pytest tests/test_provisioning.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/auth/provisioning.py tests/test_provisioning.py
git commit -m "feat(auth): provision a local user for an external identity"
```

---

## Task 6: The JWT provider

**Files:**
- Create: `src/memory/auth/providers/jwt_provider.py`
- Test: `tests/test_auth_jwt.py`

> Named `jwt_provider.py`, not `jwt.py`: a module called `jwt` inside a package that also does `import jwt` shadows PyJWT for anything using implicit-relative habits and produces an `AttributeError: module 'jwt' has no attribute 'decode'` that reads like a broken install.

**Step 1: Write the failing tests**

Use a locally generated Ed25519 key and stub the JWKS client, so the suite needs no network.

```python
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

import jwt as pyjwt
from memory.auth.providers import jwt_provider
from memory.errors import Unauthorized

ISSUER = "https://ach.example.com"
AUDIENCE = "mcp:ach-memory"


@pytest.fixture
def signing_key():
    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture(autouse=True)
def _jwt_env(monkeypatch, signing_key):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", "abc")
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("MEMORY_AUTH_JWT_AUDIENCE", AUDIENCE)
    get_settings.cache_clear()

    # The signing key is generated per test; the provider must not cache a
    # JWKS client across them.
    jwt_provider._signing_key_for.cache_clear()
    monkeypatch.setattr(
        jwt_provider,
        "_signing_key_for",
        lambda token: signing_key.public_key(),
    )
    yield
    get_settings.cache_clear()


def _token(signing_key, **claims):
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "alice@example.com",
        "exp": 4102444800,  # 2100-01-01
        **claims,
    }
    return pyjwt.encode(payload, signing_key, algorithm="EdDSA")


def test_a_valid_token_resolves_to_a_provisioned_user(session, tenant, signing_key):
    principal = jwt_provider.authenticate(_token(signing_key), session)
    assert principal.user_id
    assert principal.is_master is False
    assert principal.credential_id.startswith("ext_")


def test_groups_come_from_the_token(session, tenant, signing_key):
    principal = jwt_provider.authenticate(
        _token(signing_key, groups=["platform", "sre"]), session
    )
    assert principal.groups == frozenset({"platform", "sre"})


def test_a_missing_groups_claim_is_no_groups_not_an_error(session, tenant, signing_key):
    principal = jwt_provider.authenticate(_token(signing_key), session)
    assert principal.groups == frozenset()


def test_a_scalar_groups_claim_is_accepted(session, tenant, signing_key):
    principal = jwt_provider.authenticate(
        _token(signing_key, groups="platform"), session
    )
    assert principal.groups == frozenset({"platform"})


def test_unusable_group_values_are_dropped_not_fatal(session, tenant, signing_key):
    """An oversize or control-charactered group can never match Group.id
    (String(128)) and would 500 the lazy-create path with a DataError."""
    principal = jwt_provider.authenticate(
        _token(signing_key, groups=["ok", "x" * 200, "bad\x00", 7, ""]), session
    )
    assert principal.groups == frozenset({"ok"})


def test_a_token_without_exp_is_refused(session, tenant, signing_key):
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice@example.com"}
    token = pyjwt.encode(payload, signing_key, algorithm="EdDSA")
    with pytest.raises(Unauthorized):
        jwt_provider.authenticate(token, session)


def test_an_expired_token_says_so(session, tenant, signing_key):
    with pytest.raises(Unauthorized, match="expired"):
        jwt_provider.authenticate(_token(signing_key, exp=1), session)


def test_a_wrong_audience_is_refused(session, tenant, signing_key):
    with pytest.raises(Unauthorized):
        jwt_provider.authenticate(_token(signing_key, aud="mcp:something-else"), session)


def test_a_wrong_issuer_is_refused(session, tenant, signing_key):
    with pytest.raises(Unauthorized):
        jwt_provider.authenticate(_token(signing_key, iss="https://evil.example"), session)


def test_a_token_is_never_master(session, tenant, signing_key):
    """No claim may mint tenant-wide authority. is_master is a constant on
    this path, exactly as it is for a stored key."""
    principal = jwt_provider.authenticate(
        _token(signing_key, is_master=True, master=True), session
    )
    assert principal.is_master is False


def test_the_same_subject_twice_is_one_user(session, tenant, signing_key):
    a = jwt_provider.authenticate(_token(signing_key), session)
    b = jwt_provider.authenticate(_token(signing_key), session)
    assert a.user_id == b.user_id
```

**Step 2: Run to verify failure**

Expected: `ModuleNotFoundError: No module named 'memory.auth.providers.jwt_provider'`.

**Step 3: Implement**

```python
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
```

Add `cryptography` to the dev group in `pyproject.toml` if the tests cannot import it (it arrives with `pyjwt[crypto]`, so check first with `uv run python -c "import cryptography"`).

**Step 4: Verify**

```bash
uv run pytest tests/test_auth_jwt.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/auth/providers/jwt_provider.py tests/test_auth_jwt.py
git commit -m "feat(auth): authenticate externally-issued JWTs against a JWKS"
```

---

## Task 7: The platform resolver provider

**Files:**
- Modify: `src/memory/errors.py` (add `AuthBackendUnavailable`)
- Modify: `SPEC-v1.md` §18 (required — `tests/test_errors.py` enforces both directions)
- Create: `src/memory/auth/providers/platform.py`
- Test: `tests/test_auth_platform.py`

**Step 1: Write the failing tests**

Use `respx` (already a dev dependency) to stub the resolver.

```python
import httpx
import pytest
import respx

from memory.auth.providers import platform
from memory.errors import AuthBackendUnavailable, Unauthorized

RESOLVER = "https://api.example.com/v2/user/info"


@pytest.fixture(autouse=True)
def _platform_env(monkeypatch):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", "abc")
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "x-litellm-api-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_URL", RESOLVER)
    get_settings.cache_clear()
    platform.reset_cache()
    yield
    get_settings.cache_clear()
    platform.reset_cache()


@respx.mock
def test_resolves_user_and_team(session, tenant):
    respx.get(RESOLVER).mock(
        return_value=httpx.Response(
            200, json={"user_id": "alice@example.com", "team_id": "platform"}
        )
    )
    principal = platform.authenticate("sk-abc", session)
    assert principal.user_id
    assert principal.groups == frozenset({"platform"})
    assert principal.credential_id.startswith("ext_")


@respx.mock
def test_a_missing_team_is_no_groups(session, tenant):
    respx.get(RESOLVER).mock(
        return_value=httpx.Response(200, json={"user_id": "alice@example.com"})
    )
    assert platform.authenticate("sk-abc", session).groups == frozenset()


@respx.mock
def test_the_second_call_is_served_from_cache(session, tenant):
    route = respx.get(RESOLVER).mock(
        return_value=httpx.Response(
            200, json={"user_id": "alice@example.com", "team_id": "platform"}
        )
    )
    platform.authenticate("sk-abc", session)
    platform.authenticate("sk-abc", session)
    assert route.call_count == 1


@respx.mock
def test_a_rejected_key_is_unauthorized(session, tenant):
    respx.get(RESOLVER).mock(return_value=httpx.Response(401))
    with pytest.raises(Unauthorized):
        platform.authenticate("sk-bad", session)


@respx.mock
def test_a_rejection_is_not_cached(session, tenant):
    """Caching a 401 would keep refusing a key for the whole TTL after the
    platform re-enabled it."""
    route = respx.get(RESOLVER).mock(return_value=httpx.Response(401))
    for _ in range(2):
        with pytest.raises(Unauthorized):
            platform.authenticate("sk-bad", session)
    assert route.call_count == 2


@respx.mock
def test_an_unreachable_resolver_is_not_reported_as_a_bad_key(session, tenant):
    """503, not 401: an agent that retries a 401 forever gets nowhere, and an
    operator told 'bad credential' hunts the wrong problem during an outage."""
    respx.get(RESOLVER).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AuthBackendUnavailable):
        platform.authenticate("sk-abc", session)


@respx.mock
def test_a_response_without_user_id_is_unauthorized(session, tenant):
    respx.get(RESOLVER).mock(return_value=httpx.Response(200, json={"team_id": "x"}))
    with pytest.raises(Unauthorized):
        platform.authenticate("sk-abc", session)
```

**Step 2: Run to verify failure**

Expected: `ImportError: cannot import name 'AuthBackendUnavailable'`.

**Step 3: Implement**

Add to `src/memory/errors.py`:

```python
class AuthBackendUnavailable(DomainError):
    """The credential could not be checked, which is not the same as bad.

    Reporting a resolver outage as UNAUTHORIZED tells an agent its key is
    wrong -- so it stops retrying and a human starts rotating credentials --
    when the truth is that the check never ran. 503 says "ask again later",
    which is the only accurate thing we know.
    """

    code = "AUTH_BACKEND_UNAVAILABLE"
    status = 503
```

Add `AUTH_BACKEND_UNAVAILABLE` to SPEC §18's first fenced `text` block, in the same alphabetical position the block already uses, and mention it in §18's prose alongside the other 5xx codes. `tests/test_errors.py` compares both directions, so a missing entry fails the suite.

Create `src/memory/auth/providers/platform.py`:

```python
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
```

**Step 4: Verify**

```bash
uv run pytest tests/test_auth_platform.py tests/test_errors.py -q
```
Expected: PASS. If `test_errors.py` fails, SPEC §18's fenced list and `errors.py` disagree — fix the SPEC, not the test.

**Step 5: Commit**

```bash
git add src/memory/errors.py src/memory/auth/providers/platform.py SPEC-v1.md tests/
git commit -m "feat(auth): resolve platform API keys to an identity over HTTP"
```

---

## Task 8: Wire the chain into both surfaces

**Files:**
- Modify: `src/memory/api/app.py:19-27`
- Modify: `src/memory/mcp/server.py:44-52`
- Test: `tests/test_app.py`, `tests/test_mcp_server.py`

The platform header name is configurable, so FastAPI cannot map it to a named parameter. Take `Request` and read it by name.

**Step 1: Write the failing tests**

```python
def test_the_platform_header_reaches_the_resolver(client, monkeypatch, tenant):
    """The header name is configuration, so it cannot be a named parameter."""
    from memory.auth.providers import platform

    seen = {}

    def _fake(token, db):
        seen["token"] = token
        raise Unauthorized("stop here")

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "x-litellm-api-key")
    monkeypatch.setenv(
        "MEMORY_AUTH_PLATFORM_RESOLVER_URL", "https://api.example.com/v2/user/info"
    )
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(platform, "authenticate", _fake)

    client.get("/v1/projects", headers={"x-litellm-api-key": "sk-abc"})
    assert seen["token"] == "sk-abc"


def test_a_bearer_prefixed_platform_token_is_stripped(client, monkeypatch, tenant):
    """LiteLLM's own header requires the prefix; the resolver must not see it."""
    ...  # same shape, headers={"x-litellm-api-key": "Bearer sk-abc"}, assert "sk-abc"
```

**Step 2: Run to verify failure**

Expected: FAIL — `seen` is empty; the header is never read.

**Step 3: Implement**

`src/memory/api/app.py`:

```python
from fastapi import Depends, FastAPI, Header, Request


def _platform_token(request: Request) -> str | None:
    """The platform credential, read by a name that comes from configuration.

    FastAPI maps a parameter name to a fixed header, so a configurable header
    has to be read off the Request. Returns None when the provider is off, so
    a stray header on an unconfigured deployment is simply not a credential.
    """
    settings = get_settings()
    if not settings.auth_platform_enabled:
        return None
    raw = request.headers.get(settings.auth_platform_incoming_header)
    if raw is None:
        return None
    value = raw.strip()
    # LiteLLM's own header carries the prefix; the resolver must receive the
    # bare key.
    if value.lower().startswith("bearer "):
        value = value[len("bearer ") :].strip()
    return value or None


def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    # FastAPI maps this parameter name to the `x-ach-memory-key` header. It
    # takes precedence over Authorization when present -- see
    # memory.auth.principal.API_KEY_HEADER for why the dedicated header exists.
    x_ach_memory_key: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_session),
) -> Principal:
    return resolve_principal(
        authorization,
        db,
        api_key=x_ach_memory_key,
        platform_token=_platform_token(request),
    )
```

`src/memory/mcp/server.py` — headers arrive as a plain mapping, so read the configured name directly. Keep the master-key refusal exactly as it is:

```python
    headers = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    authorization = headers.get("authorization")
    api_key = headers.get(API_KEY_HEADER)

    settings = get_settings()
    platform_token = None
    if settings.auth_platform_enabled and settings.auth_platform_incoming_header:
        raw = headers.get(settings.auth_platform_incoming_header.lower())
        if raw:
            value = raw.strip()
            if value.lower().startswith("bearer "):
                value = value[len("bearer ") :].strip()
            platform_token = value or None

    with session_scope() as db:
        principal = resolve_principal(
            authorization, db, api_key=api_key, platform_token=platform_token
        )
        if principal.is_master:
            ...  # unchanged
```

> Lower-casing the whole mapping replaces the existing `headers.get("authorization") or headers.get("Authorization")` pair, which only covered two of the spellings HTTP permits.

**Step 4: Verify**

```bash
uv run pytest tests/test_app.py tests/test_mcp_server.py tests/test_mcp_tools.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/api/app.py src/memory/mcp/server.py tests/
git commit -m "feat(auth): read the configured platform header on REST and MCP"
```

---

## Task 9: Asserted groups authorize, and materialize only when owning

**Files:**
- Modify: `src/memory/projects.py:87-115` (`authorize`)
- Modify: `src/memory/projects.py:64-84` (`_validate_owner`)
- Test: `tests/test_projects.py`

**Step 1: Write the failing tests**

```python
def _external_principal(tenant, groups):
    from memory.auth.principal import Principal

    return Principal(
        tenant_id=tenant, user_id="usr_ext", is_master=False, key_id=None,
        groups=frozenset(groups), credential_id="ext_abc",
    )


def test_an_asserted_group_authorizes_without_a_membership_row(session, tenant):
    """The whole point of the JWT path: the IdP asserts membership, so no
    group_members row has to exist for the caller to reach the project."""
    project = _group_owned_project(session, tenant, owner_id="platform")
    projects.authorize(session, _external_principal(tenant, {"platform"}), project)


def test_a_group_the_token_does_not_assert_is_denied(session, tenant):
    project = _group_owned_project(session, tenant, owner_id="platform")
    with pytest.raises(ProjectAccessDenied):
        projects.authorize(session, _external_principal(tenant, {"sre"}), project)


def test_database_membership_still_authorizes_a_local_key(session, tenant):
    """The two sources are independent, not merged: a local key keeps working
    exactly as before."""
    ...


def test_owning_a_project_materializes_the_asserted_group(session, tenant):
    """authorize() needs no row, but _validate_owner does -- and the group
    only has to exist at the moment someone hands it a project."""
    principal = _external_principal(tenant, {"platform"})
    project = projects.create(
        session, principal, "acme-api", "group", "platform", None
    )
    assert session.get(Group, "platform") is not None
    assert project.owner_id == "platform"


def test_a_group_the_token_does_not_assert_is_not_materialized(session, tenant):
    principal = _external_principal(tenant, {"platform"})
    with pytest.raises(GroupNotFound):
        projects.create(session, principal, "acme-api", "group", "sre", None)
    assert session.get(Group, "sre") is None
```

**Step 2: Run to verify failure**

Expected: `test_an_asserted_group_authorizes_without_a_membership_row` FAILS with `ProjectAccessDenied`.

**Step 3: Implement**

In `authorize`:

```python
    if project.owner_type == "group" and (
        # Asserted by the caller's identity provider, or recorded locally.
        # Checked independently and never merged: an IdP that stops asserting
        # a group revokes access on the next request without any row changing,
        # while a local key's membership stays a database fact.
        project.owner_id in principal.groups
        or db.get(GroupMember, (project.owner_id, principal.user_id))
    ):
        return
```

In `_validate_owner`, add the principal's asserted groups so an external caller can own a project. Change the signature to take the `Principal` (both call sites, `create` and `transfer`, already have one):

```python
def _validate_owner(
    db: Session, principal: Principal, owner_type: str, owner_id: str
) -> None:
    """The owner must exist in this tenant. An unchecked id silently orphans
    the project: authorize() then denies everyone and only a master key can
    undo it. Shared by create() and transfer()."""
    if owner_type == "user":
        reject_control_characters(owner_id, UserNotFound)
        owner = db.get(User, owner_id)
        if owner is None or owner.tenant_id != principal.tenant_id:
            raise UserNotFound(user_id=owner_id)
    elif owner_type == "group":
        reject_control_characters(owner_id, GroupNotFound)
        owner = db.get(Group, owner_id)
        if owner is None:
            # A group asserted by the caller's identity provider is real, it
            # just has no row yet -- nothing provisions one, because groups
            # arrive in a token rather than through POST /v1/groups. Created
            # here and only here: `authorize` needs no row, so this is the one
            # path that actually requires the group to exist, and creating it
            # on every authenticated request instead would write rows for
            # groups nobody ever uses.
            if owner_id not in principal.groups:
                raise GroupNotFound(group_id=owner_id)
            try:
                with db.begin_nested():
                    db.add(Group(id=owner_id, tenant_id=principal.tenant_id))
            except IntegrityError:
                # Lost the race; the row exists, which is all we needed.
                pass
        elif owner.tenant_id != principal.tenant_id:
            raise GroupNotFound(group_id=owner_id)
    else:
        # Guarded here and not only at the API edge: a bad owner_type would
        # make authorize() fall through to a denial for everyone, silently
        # orphaning the project.
        raise InvalidOwnerType("owner type must be user or group")
```

Update both call sites to pass `principal` instead of `principal.tenant_id`. Add `from sqlalchemy.exc import IntegrityError` if not already imported.

Then check `api/projects.py:110-113`: a user key may only create a project it owns. An external caller creating a *group*-owned project must be allowed when the token asserts that group:

```python
    elif not principal.is_master and not (
        (owner.type == "user" and owner.id == principal.user_id)
        or (owner.type == "group" and owner.id in principal.groups)
    ):
        raise Forbidden("a user key may only create a project it owns")
```

**Step 4: Verify**

```bash
uv run pytest tests/test_projects.py tests/test_projects_api.py tests/test_groups_api.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/projects.py src/memory/api/projects.py tests/
git commit -m "feat(authz): authorize against groups asserted by the identity provider"
```

---

## Task 10: Rate limiting and audit follow the credential, not the key row

**Files:**
- Modify: `src/memory/ratelimit.py:94-99`
- Modify: `src/memory/audit.py:27-36`
- Test: `tests/test_ratelimit.py`, `tests/test_audit.py`

**Step 1: Write the failing tests**

```python
def test_two_external_identities_do_not_share_a_bucket(monkeypatch):
    """Before credential_id, every JWT caller fell into the master bucket --
    one 60-writes-per-minute ceiling for the entire fleet (SPEC §20)."""
    from memory import ratelimit
    from memory.auth.principal import Principal

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    ...
    alice = Principal("default", "usr_a", False, None, frozenset(), "ext_alice")
    bob = Principal("default", "usr_b", False, None, frozenset(), "ext_bob")
    ratelimit.check(alice)
    ratelimit.check(bob)          # must not raise
    with pytest.raises(RateLimited):
        ratelimit.check(alice)    # alice's own bucket is spent


def test_an_external_actor_is_recorded_in_the_audit_trail(session, tenant):
    from memory import audit
    from memory.auth.principal import Principal
    from memory.models import AuditEvent

    principal = Principal("default", "usr_a", False, None, frozenset(), "ext_alice")
    audit.record(session, principal, "project.rename", "acme-api")
    session.flush()
    event = session.query(AuditEvent).one()
    assert event.actor_key_id == "ext_alice"
```

**Step 2: Run to verify failure**

Expected: the first test FAILS (both principals share the master bucket); the second FAILS with `actor_key_id is None`.

**Step 3: Implement**

`ratelimit.py`:

```python
    if principal.credential_id:
        get_limiter().check(principal.credential_id)
```

Extend the docstring:

```python
    An external identity gets its own bucket too, keyed by the `ext_` id that
    `auth.provisioning.credential_id_for` derives from (issuer, subject).
    Without it every JWT caller fell through to the master bucket below and
    the whole fleet shared one ceiling -- SPEC §20's per-credential MUST,
    failing with no error and no log.
```

`audit.py`:

```python
            actor_key_id=principal.credential_id,
```

Extend the docstring:

```python
    `actor_key_id` holds whichever credential acted: an `key_`-prefixed
    api_keys.id, or an `ext_`-prefixed identity that `external_identities`
    resolves back to a human. The two namespaces are disjoint by construction
    (ids.py, provisioning.CREDENTIAL_PREFIX). NULL still means the master key,
    which is configuration and never a row.
```

**Step 4: Verify**

```bash
uv run pytest tests/test_ratelimit.py tests/test_audit.py tests/test_governance_ratelimit.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add src/memory/ratelimit.py src/memory/audit.py tests/
git commit -m "fix(auth): give every external identity its own bucket and audit actor"
```

---

## Task 11: Documentation, deployment, and the full gate

**Files:**
- Modify: `SPEC-v1.md` §5 (add §5.3), §7, §11.1
- Modify: `README.md`, `.env.example`
- Modify: `deploy/helm/ach-memory/values.yaml`, `templates/deployment.yaml`

**Step 1: SPEC §5.3**

Add after §5.2, and update §7's pseudocode so the authorization rules match the code:

```text
user key:
    if project.owner_type == "group":
        allow iff the caller is a member of project.owner_id, either
        recorded in group_members or asserted by their identity provider
```

Update §11.1's pipeline to name authentication as a provider chain, and add a paragraph to §5.1 recording why `Authorization` still carries `mem_` keys (the codex/pi constraint) and how the `mem_` prefix separates the two namespaces.

**Step 2: `.env.example`**

```bash
# External identity (optional; both providers may be enabled together).
# The issuer must match the `iss` claim exactly, so it is the public URL even
# when JWKS is fetched in-cluster -- override the JWKS URI for that.
# MEMORY_AUTH_JWT_ENABLED=true
# MEMORY_AUTH_JWT_ISSUER=https://ach.example.com
# MEMORY_AUTH_JWT_JWKS_URI=http://ach.ach.svc/.well-known/jwks.json
# MEMORY_AUTH_JWT_AUDIENCE=mcp:ach-memory
# MEMORY_AUTH_JWT_VERIFY_AUDIENCE=true
# MEMORY_AUTH_PLATFORM_ENABLED=true
# MEMORY_AUTH_PLATFORM_INCOMING_HEADER=x-litellm-api-key
# MEMORY_AUTH_PLATFORM_RESOLVER_HEADER=x-litellm-api-key
# MEMORY_AUTH_PLATFORM_RESOLVER_URL=http://litellm.genai.svc/v2/user/info
# MEMORY_AUTH_PLATFORM_CACHE_TTL=300
```

**Step 3: Helm**

Add an `auth:` block to `values.yaml` with every variable defaulting to off/empty, and render each into `deployment.yaml`'s env list only when set. Keep the existing `helm lint` clean — `make chart` renders the must-refuse-without-a-master-key case and must still pass.

**Step 4: Run the full gate**

```bash
make verify
```
Expected: ruff clean, all tests pass, gitleaks clean, helm lint clean.

Then confirm the untouched paths still work end to end against the compose stack:

```bash
make up
./scripts/smoke.sh
```
Expected: PASS — this is the regression check that `Authorization: Bearer mem_...` still authenticates, which is the one thing every existing client depends on.

**Step 5: Commit**

```bash
git add SPEC-v1.md README.md .env.example deploy/
git commit -m "docs(auth): specify the identity providers and their configuration"
```

---

## Out of scope

Recorded so they are decisions, not oversights:

- **No JWT for the master key.** Provisioning stays a local credential. Invariant 22 keeps the master key out of MCP entirely, and nothing in this plan changes that.
- **No group *names* from tokens.** A materialized `Group` row gets an id and a null name; naming it is `PUT /v1/groups/{id}` with the master key.
- **No revocation of a stale link.** Deleting an `external_identities` row orphans a `User` that only the master key can reach. A `DELETE /v1/users/{id}/identities/{...}` endpoint is a follow-up if it is ever needed.
- **ACH does not emit a `groups` claim yet** (`../ach/internal/forwarder/jwt/signer.go:174`). Everything here works when it starts to; until then an ACH caller has no asserted groups and falls back to `group_members`.
