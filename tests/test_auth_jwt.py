import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from memory.auth.providers import jwt as jwt_provider
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
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://hindsight.test")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("MEMORY_AUTH_JWT_AUDIENCE", AUDIENCE)
    get_settings.cache_clear()

    # A fresh signing key per test, so the provider must not cache a JWKS
    # client across them.
    jwt_provider._signing_key_for.cache_clear()
    monkeypatch.setattr(
        jwt_provider, "_signing_key_for", lambda token: signing_key.public_key()
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


def test_a_valid_token_resolves_to_a_provisioned_user(db, signing_key):
    principal = jwt_provider.authenticate(_token(signing_key), db)
    assert principal.user_id.startswith("usr_")
    assert principal.issuer == ISSUER


def test_groups_come_from_the_token(db, signing_key):
    principal = jwt_provider.authenticate(
        _token(signing_key, groups=["platform", "sre"]), db
    )
    assert principal.groups == frozenset({"platform", "sre"})


def test_a_missing_groups_claim_is_no_groups_not_an_error(db, signing_key):
    principal = jwt_provider.authenticate(_token(signing_key), db)
    assert principal.groups == frozenset()


def test_a_second_sight_of_the_same_subject_reuses_the_user(db, signing_key):
    first = jwt_provider.authenticate(_token(signing_key), db)
    second = jwt_provider.authenticate(_token(signing_key), db)
    assert first.user_id == second.user_id


def test_a_token_without_exp_is_refused(db, signing_key):
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice@example.com"}
    token = pyjwt.encode(payload, signing_key, algorithm="EdDSA")
    with pytest.raises(Unauthorized):
        jwt_provider.authenticate(token, db)


def test_an_expired_token_says_so(db, signing_key):
    with pytest.raises(Unauthorized, match="expired"):
        jwt_provider.authenticate(_token(signing_key, exp=1), db)


def test_a_wrong_audience_is_refused(db, signing_key):
    with pytest.raises(Unauthorized):
        jwt_provider.authenticate(_token(signing_key, aud="mcp:something-else"), db)


def test_a_wrong_issuer_is_refused(db, signing_key):
    with pytest.raises(Unauthorized):
        jwt_provider.authenticate(_token(signing_key, iss="https://evil.example"), db)


def test_a_bad_signature_is_refused(db, signing_key):
    other_key = ed25519.Ed25519PrivateKey.generate()
    forged = _token(other_key)  # signed by a key the provider never returns
    with pytest.raises(Unauthorized):
        jwt_provider.authenticate(forged, db)
