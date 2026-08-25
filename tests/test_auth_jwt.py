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
    """ACH does not emit a groups claim yet, so this is the common case."""
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
