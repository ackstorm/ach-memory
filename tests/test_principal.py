import pytest

from memory.auth import principal
from memory.auth.principal import Principal, authenticate, clean_groups
from memory.config import Settings
from memory.errors import Forbidden, Unauthorized

#: Structurally a JWT and nothing more -- enough to be ROUTED to the JWT
#: provider, which is what dispatch tests are about. Real signature
#: verification lives in test_auth_jwt.py.
JWT_SHAPED = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.not-a-real-signature"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", "http://localhost:8888")
    from memory.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def jwt_enabled(monkeypatch):
    """The dispatcher's only job is choosing a provider, so the provider is a
    spy here; test_auth_jwt.py owns real verification."""
    from memory.auth.providers import jwt as jwt_provider
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    get_settings.cache_clear()

    seen: list[str] = []

    def _authenticate(token, db):
        seen.append(token)
        return Principal(user_id="usr_jwt", subject="usr_jwt")

    monkeypatch.setattr(jwt_provider, "authenticate", _authenticate)
    return seen


@pytest.fixture
def jwt_rejecting(monkeypatch):
    from memory.auth.providers import jwt as jwt_provider
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    get_settings.cache_clear()

    def _authenticate(token, db):
        raise Unauthorized("token rejected")

    monkeypatch.setattr(jwt_provider, "authenticate", _authenticate)


@pytest.fixture
def platform_enabled(monkeypatch):
    from memory.auth.providers import platform as platform_provider
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    get_settings.cache_clear()

    seen: list[str] = []

    def _authenticate(token, db):
        seen.append(token)
        return Principal(user_id="usr_platform", subject="usr_platform")

    monkeypatch.setattr(platform_provider, "authenticate", _authenticate)
    return seen


# --- clean_groups ------------------------------------------------------


def test_a_scalar_is_wrapped_as_one_group():
    assert clean_groups("platform") == frozenset({"platform"})


def test_unusable_values_are_dropped_not_fatal():
    assert clean_groups(["ok", "x" * 200, "bad\x00", 7, ""]) == frozenset({"ok"})


def test_a_non_list_non_str_value_is_no_groups():
    assert clean_groups(None) == frozenset()
    assert clean_groups(42) == frozenset()


# --- dispatch: shape decides the provider, and the choice is final ----


def test_a_jwt_shaped_token_goes_to_the_jwt_provider(jwt_enabled):
    p = authenticate({"authorization": f"Bearer {JWT_SHAPED}"}, None)
    assert jwt_enabled == [JWT_SHAPED]
    assert p.user_id == "usr_jwt"


def test_an_opaque_token_goes_to_the_platform_resolver(jwt_enabled, platform_enabled):
    authenticate(
        {"authorization": "Bearer mem_opaque", "x-litellm-api-key": "mem_opaque"}, None
    )
    assert jwt_enabled == []
    assert platform_enabled == ["mem_opaque"]


def test_a_rejected_jwt_never_falls_through_to_platform(jwt_rejecting, platform_enabled):
    with pytest.raises(Unauthorized):
        authenticate(
            {"authorization": f"Bearer {JWT_SHAPED}", "x-litellm-api-key": "x"}, None
        )
    assert platform_enabled == []


def test_headers_are_matched_case_insensitively(jwt_enabled):
    p = authenticate({"Authorization": f"Bearer {JWT_SHAPED}"}, None)
    assert p.user_id == "usr_jwt"


def test_no_provider_enabled_is_unauthorized():
    with pytest.raises(Unauthorized):
        authenticate({"authorization": f"Bearer {JWT_SHAPED}"}, None)


def test_a_missing_platform_header_is_unauthorized(platform_enabled):
    with pytest.raises(Unauthorized):
        authenticate({}, None)
    assert platform_enabled == []


# --- operator resolution is configuration, not a credential -----------


def test_an_unset_master_config_grants_nobody():
    settings = Settings(master_users="", master_groups="")
    assert not principal._is_operator(Principal(user_id="", groups=frozenset({""})), settings)


def test_a_configured_user_grants_operator():
    settings = Settings(master_users="usr_1", master_groups="")
    assert principal._is_operator(Principal(user_id="usr_h", subject="usr_1"), settings)
    assert not principal._is_operator(Principal(user_id="usr_h2", subject="usr_2"), settings)


def test_a_configured_group_grants_operator():
    settings = Settings(master_users="", master_groups="sre,platform")
    assert principal._is_operator(
        Principal(user_id="x", groups=frozenset({"platform"})), settings
    )
    assert not principal._is_operator(
        Principal(user_id="x", groups=frozenset({"devs"})), settings
    )


def test_master_issuer_gates_operator_matching_when_set():
    settings = Settings(master_users="usr_1", master_issuer="https://idp.example.com")
    assert principal._is_operator(
        Principal(user_id="usr_h", subject="usr_1", issuer="https://idp.example.com"), settings
    )
    assert not principal._is_operator(
        Principal(user_id="usr_h", subject="usr_1", issuer="http://other.example.com"), settings
    )


def test_an_unset_master_issuer_does_not_gate():
    settings = Settings(master_users="usr_1")
    assert principal._is_operator(Principal(user_id="usr_h", subject="usr_1", issuer="anything"), settings)


# --- on_behalf_of is an operator-only claim, recorded verbatim --------


def test_an_operator_can_set_on_behalf_of(monkeypatch, jwt_enabled):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MASTER_USERS", "usr_jwt")
    get_settings.cache_clear()

    p = authenticate(
        {"authorization": f"Bearer {JWT_SHAPED}", "on-behalf-of": "usr_other"}, None
    )
    assert p.is_operator
    assert p.on_behalf_of == "usr_other"


def test_a_non_operator_setting_on_behalf_of_is_forbidden(jwt_enabled):
    with pytest.raises(Forbidden):
        authenticate(
            {"authorization": f"Bearer {JWT_SHAPED}", "on-behalf-of": "usr_other"}, None
        )


def test_on_behalf_of_absent_is_none_for_an_operator(monkeypatch, jwt_enabled):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MASTER_USERS", "usr_jwt")
    get_settings.cache_clear()

    p = authenticate({"authorization": f"Bearer {JWT_SHAPED}"}, None)
    assert p.on_behalf_of is None


def test_a_principal_requires_a_user_id():
    with pytest.raises(TypeError):
        Principal()
