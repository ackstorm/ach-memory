import pytest
from pydantic import ValidationError

from memory.config import Settings

REQUIRED = {
    "MEMORY_DATABASE_URL": "postgresql+psycopg://memory:memory@localhost:5432/memory",
    "MEMORY_HINDSIGHT_URL": "http://localhost:8888",
}


def _clear(monkeypatch):
    for key in list(REQUIRED) + [
        "MEMORY_TENANT_ID",
        "MEMORY_MAX_CONTENT_BYTES",
        # Operator authority replaced the master key hash, and it is the one
        # setting whose ambient value would silently change what a test means.
        "MEMORY_MASTER_USERS",
        "MEMORY_MASTER_GROUPS",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_settings_read_from_environment(monkeypatch):
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    settings = Settings()

    assert settings.database_url == REQUIRED["MEMORY_DATABASE_URL"]
    assert settings.hindsight_url == "http://localhost:8888"


def test_tenant_id_defaults_to_hindsight_default_segment(monkeypatch):
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    assert Settings().tenant_id == "default"


def test_the_default_allowed_hosts_cover_any_loopback_port(monkeypatch):
    """A portless entry never matches a Host carrying a port (the SDK matches
    the whole header), and the service listens on 8000. Without the wildcard
    forms the shipped default answers 421 to every direct MCP call."""
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    allowed = [h.strip() for h in Settings().mcp_allowed_hosts.split(",")]
    assert "127.0.0.1:*" in allowed
    assert "localhost:*" in allowed
    # portless forms stay: an ingress on 80/443 sends a Host with no port
    assert "127.0.0.1" in allowed
    assert "localhost" in allowed


def test_missing_required_setting_fails_loudly(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("MEMORY_DATABASE_URL", REQUIRED["MEMORY_DATABASE_URL"])

    with pytest.raises(ValidationError):
        Settings()


def test_a_zero_write_limit_is_refused(monkeypatch):
    """MEMORY_WRITE_LIMIT=0 is the natural spelling of "block all writes" and
    made Limiter.check evaluate len(hits) >= 0 -> True on an empty deque, then
    IndexError on hits[0] -> 500 on every write instead of 429."""
    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "0")
    with pytest.raises(ValidationError):
        Settings()


def _operator_env(monkeypatch, users: str | None = None, groups: str | None = None):
    _clear(monkeypatch)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    if users is not None:
        monkeypatch.setenv("MEMORY_MASTER_USERS", users)
    if groups is not None:
        monkeypatch.setenv("MEMORY_MASTER_GROUPS", groups)
    return Settings()


def test_operator_authority_defaults_to_nobody(monkeypatch):
    """Unset must grant nobody, and it is the raw string that has to stay
    empty as well: `"".split(",")` is `[""]`, so a naive parse would put the
    empty string in the granted set on every deployment that never set the
    variable -- and there it would match a principal with an empty subject,
    silently."""
    settings = _operator_env(monkeypatch)

    assert settings.master_users == ""
    assert settings.master_groups == ""
    assert settings.master_user_ids == frozenset()
    assert settings.master_group_ids == frozenset()


@pytest.mark.parametrize("raw", ["", " ", ",", " , ,, ", "\n"])
def test_an_empty_ish_master_list_grants_nobody(monkeypatch, raw):
    """A variable set to nothing in particular -- a blank Helm value, a
    trailing comma, a newline from a mounted file -- is the same "nobody" as
    unset. Anything else grants authority to whoever happens to resolve to a
    blank id."""
    settings = _operator_env(monkeypatch, users=raw, groups=raw)

    assert settings.master_user_ids == frozenset()
    assert settings.master_group_ids == frozenset()


def test_an_empty_configured_entry_never_matches_a_blank_principal(monkeypatch):
    """The failure this parsing exists to prevent, asserted at the place it
    would actually be felt: `is_operator`."""
    from memory.auth.principal import Principal, is_operator

    settings = _operator_env(monkeypatch, users="", groups="")
    blank = Principal(credential_id="ext_test",
                
        tenant_id="default", user_id="usr_x", subject="", groups=frozenset({""})
    )

    assert not is_operator(blank, settings)


def test_master_lists_are_comma_separated_and_whitespace_stripped(monkeypatch):
    """The documented shape: a comma-separated list an administrator writes by
    hand, so spaces around the separators are ordinary, not an error."""
    settings = _operator_env(
        monkeypatch,
        users=" operator@test , ops@test ,",
        groups="platform-admins , sre",
    )

    assert settings.master_user_ids == frozenset({"operator@test", "ops@test"})
    assert settings.master_group_ids == frozenset({"platform-admins", "sre"})


def test_a_zero_or_negative_write_window_is_refused(monkeypatch):
    """A window of 0 makes `cutoff = now - window` evict every hit immediately,
    so the limiter never fires again -- SPEC §20's MUST silently bypassed with
    no error and no log. Quieter than the write_limit=0 crash beside it, which
    at least announced itself as a 500."""
    import pytest
    from pydantic import ValidationError

    from memory.config import Settings

    for value in ("0", "-5"):
        monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings()


def test_the_readme_documents_every_setting():
    """The README's Configuration table is the only place an operator
    deploying outside Helm/Compose learns which variables exist and which are
    required. Nothing kept it in step with `Settings`, and five of these were
    undocumented before it was written.

    Same guard as tests/test_slugs.py's SPEC §8.2 check, for the same reason:
    a prose rule with no executable counterpart drifts, and this project has
    already shipped one that did.
    """
    import re
    from pathlib import Path

    from memory.config import Settings

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    # Anchored on the next heading, not on a sentence inside the prose. The
    # previous anchor was the phrase "The three required variables", which the
    # README stopped containing when it was reworded -- so this guard failed
    # open-ish (hard AssertionError on every run, unrelated to any setting)
    # and MEMORY_MAX_CONTENT_BYTES slipped out of the docs undetected.
    section = re.search(r"## Configuration\n(.*?)\n## Development", readme, re.DOTALL)
    assert section, "README's Configuration section moved -- update this guard"

    # Bare names in a fenced block (required) or inline code (optional): the
    # section documents settings in both shapes, so match the identifier
    # itself rather than one presentation of it.
    documented = set(re.findall(r"(MEMORY_[A-Z_]+)", section.group(1)))
    actual = {f"MEMORY_{name.upper()}" for name in Settings.model_fields}
    assert documented == actual, (
        f"undocumented: {sorted(actual - documented)}; "
        f"documented but not real: {sorted(documented - actual)}"
    )


def _base_env(monkeypatch):
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
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
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "teams")
    assert Settings().auth_platform_enabled is True


def test_incoming_headers_splits_lowercases_and_keeps_order(monkeypatch):
    """Order is priority (gateway header first), duplicates and blanks dropped,
    lower-cased because HTTP header lookup is case-insensitive."""
    monkeypatch.setenv(
        "MEMORY_AUTH_PLATFORM_INCOMING_HEADER", " X-LiteLLM-Api-Key , authorization , ,x-litellm-api-key"
    )
    assert Settings().incoming_headers == ("x-litellm-api-key", "authorization")


def test_incoming_headers_is_empty_when_unset(monkeypatch):
    """Unset parses to no headers, which the enabled-config check rejects --
    never a tuple with an empty string that would match a missing header."""
    monkeypatch.delenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", raising=False)
    assert Settings().incoming_headers == ()


def test_platform_requires_every_var(monkeypatch):
    import pytest

    from memory.config import Settings

    _base_env(monkeypatch)
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    with pytest.raises(
        ValueError,
        match="RESOLVER_HEADER.*RESOLVER_URL.*USER_FIELD.*GROUPS_FIELD",
    ):
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
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "teams")
    settings = Settings()
    assert settings.auth_jwt_enabled and settings.auth_platform_enabled




