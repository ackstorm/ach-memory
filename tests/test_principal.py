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


def test_api_key_header_resolves_without_authorization(session, tenant):
    user, plaintext = _make_user_key(session, tenant)

    principal = resolve_principal(None, session, api_key=plaintext)

    assert principal.is_master is False
    assert principal.user_id == user.id


def test_api_key_header_tolerates_a_bearer_prefix(session, tenant):
    user, plaintext = _make_user_key(session, tenant)

    principal = resolve_principal(None, session, api_key=f"Bearer {plaintext}")

    assert principal.user_id == user.id


def test_api_key_header_wins_over_authorization(session, tenant):
    """Precedence is the whole point: whatever a proxy leaves in Authorization
    must not override the credential the caller explicitly nominated."""
    user, plaintext = _make_user_key(session, tenant)

    principal = resolve_principal(
        f"Bearer {MASTER_PLAINTEXT}", session, api_key=plaintext
    )

    # The master key sat in Authorization and was ignored.
    assert principal.is_master is False
    assert principal.user_id == user.id


def test_blank_api_key_header_does_not_fall_back_to_authorization(session, tenant):
    """A present-but-empty dedicated header is a caller error, not an absent
    one. Falling through here would authenticate as whoever Authorization
    names -- the confused deputy this precedence exists to prevent."""
    with pytest.raises(Unauthorized):
        resolve_principal(f"Bearer {MASTER_PLAINTEXT}", session, api_key="   ")


def test_unknown_api_key_header_is_unauthorized(session, tenant):
    with pytest.raises(Unauthorized):
        resolve_principal(None, session, api_key=keys.generate_key())


def test_revoked_key_is_unauthorized_via_api_key_header(session, tenant):
    _, plaintext = _make_user_key(session, tenant)
    session.query(ApiKey).update({"status": "revoked"})
    session.flush()

    with pytest.raises(Unauthorized):
        resolve_principal(None, session, api_key=plaintext)


def test_master_key_still_works_over_the_api_key_header(session, tenant):
    principal = resolve_principal(None, session, api_key=MASTER_PLAINTEXT)

    assert principal.is_master is True
    assert principal.user_id is None


def test_principal_defaults_to_no_groups(session, tenant):
    _, plaintext = _make_user_key(session, tenant)
    principal = resolve_principal(f"Bearer {plaintext}", session)
    assert principal.groups == frozenset()


def test_local_key_credential_id_is_its_key_id(session, tenant):
    _, plaintext = _make_user_key(session, tenant)
    principal = resolve_principal(f"Bearer {plaintext}", session)
    assert principal.credential_id == principal.key_id
    assert principal.credential_id is not None


def test_master_has_no_credential_id(session):
    principal = resolve_principal(f"Bearer {MASTER_PLAINTEXT}", session)
    assert principal.is_master
    assert principal.credential_id is None
