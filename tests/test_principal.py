import pytest

from memory import ids
from memory.auth import keys
from memory.auth.principal import resolve_principal
from memory.errors import Unauthorized
from memory.models import ApiKey, User

MASTER_PLAINTEXT = "mem_master_secret_for_tests"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("MEMORY_MASTER_KEY_HASH", keys.hash_key(MASTER_PLAINTEXT))
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://localhost:8888")
    from memory.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_user_key(session, tenant) -> tuple[User, str]:
    # Do not simplify `secret_hash=keys.hash_key(plaintext)` below to storing
    # `plaintext` directly. This exact call is the only thing that kills a
    # self-consistent cleartext-storage mutant (storage writes plaintext AND
    # resolve_principal compares plaintext -- functionally invisible to every
    # assertion in this file: test_user_key_resolves_to_its_user only checks
    # that resolution succeeds, never that storage is hashed). Routing this
    # fixture's row through the real keys.hash_key is what makes that mutant
    # fail here instead of passing silently.
    user = User(id=ids.new_user_id(), tenant_id=tenant, bank_id=ids.new_user_bank_id())
    session.add(user)
    session.flush()
    plaintext = keys.generate_key()
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=tenant,
            user_id=user.id,
            secret_hash=keys.hash_key(plaintext),
        )
    )
    session.flush()
    return user, plaintext


def test_master_key_resolves_to_a_tenant_only_principal(session, tenant):
    principal = resolve_principal(f"Bearer {MASTER_PLAINTEXT}", session)

    assert principal.is_master is True
    assert principal.user_id is None
    assert principal.tenant_id == tenant


def test_user_key_resolves_to_its_user(session, tenant):
    user, plaintext = _make_user_key(session, tenant)

    principal = resolve_principal(f"Bearer {plaintext}", session)

    assert principal.is_master is False
    assert principal.user_id == user.id


def test_unknown_key_is_unauthorized(session, tenant):
    with pytest.raises(Unauthorized):
        resolve_principal(f"Bearer {keys.generate_key()}", session)


def test_revoked_key_is_unauthorized(session, tenant):
    _, plaintext = _make_user_key(session, tenant)
    session.query(ApiKey).update({"status": "revoked"})
    session.flush()

    with pytest.raises(Unauthorized):
        resolve_principal(f"Bearer {plaintext}", session)


def test_missing_header_is_unauthorized(session, tenant):
    with pytest.raises(Unauthorized):
        resolve_principal(None, session)


def test_non_bearer_header_is_unauthorized(session, tenant):
    with pytest.raises(Unauthorized):
        resolve_principal(f"Basic {MASTER_PLAINTEXT}", session)
