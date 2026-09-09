import pytest

from memory.auth.principal import (
    API_KEY_HEADER,
    Principal,
    is_operator,
    resolve_principal,
)
from memory.config import Settings
from memory.errors import Unauthorized


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
    """The dispatcher's only job is choosing a provider, so the provider
    itself is a spy here. `tests/test_auth_jwt.py` owns the real signature
    verification; duplicating its key harness would test PyJWT twice and the
    dispatch not at all."""
    from memory.auth.providers import jwt_provider
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("MEMORY_AUTH_JWT_AUDIENCE", "mcp:ach-memory")
    get_settings.cache_clear()

    seen: list[str] = []

    def _authenticate(token, db):
        seen.append(token)
        return Principal(tenant_id="default", user_id="usr_jwt", credential_id="ext_1")

    monkeypatch.setattr(jwt_provider, "authenticate", _authenticate)
    return seen


def _principal(user_id=None, groups=frozenset(), subject=None) -> Principal:
    return Principal(
        tenant_id="default", user_id=user_id, groups=groups, subject=subject
    )


# --- Operator authority is configuration, not a credential -----------------


def test_an_unset_master_config_grants_nobody():
    """The failure mode that matters is not "the wrong person is an
    operator", it is "an unset variable made everyone one". An empty default
    is not enough on its own: "".split(",") is [""], which would match a
    principal whose user id or group id is the empty string."""
    settings = Settings(master_users="", master_groups="")

    assert not is_operator(
        _principal(user_id="", groups=frozenset({""}), subject=""), settings
    )
    assert not is_operator(_principal(user_id="usr_1", subject="a@b.test"), settings)


def test_a_separator_only_master_config_grants_nobody():
    settings = Settings(master_users=" , ", master_groups=",,")

    assert not is_operator(
        _principal(user_id="", groups=frozenset({""}), subject=""), settings
    )


def test_a_configured_user_grants_operator():
    settings = Settings(master_users="juancarlos@example.com", master_groups="")

    assert is_operator(_principal(subject="juancarlos@example.com"), settings)
    assert not is_operator(_principal(subject="someone@example.com"), settings)


def test_the_configured_user_is_the_subject_not_the_local_user_id():
    """`user_id` is minted by `link_identity` the first time an identity is
    seen, so naming an operator by it would mean configuring a value that
    does not exist until after that operator's first login, and that nobody
    can predict. Configuration names the identity the IdP asserts."""
    settings = Settings(master_users="usr_1", master_groups="")

    assert not is_operator(_principal(user_id="usr_1", subject="a@b.test"), settings)


def test_a_principal_without_a_subject_never_matches_the_user_branch():
    settings = Settings(master_users="", master_groups="")

    assert not is_operator(_principal(user_id="usr_1", subject=None), settings)


def test_a_surface_can_withhold_authority_from_an_operator(monkeypatch):
    """MCP yields every principal with `authority_allowed=False`, so the same
    person is an operator over REST and an ordinary user over MCP. The gate is
    on `is_master`, not on `is_operator`: configuration still says this person
    holds authority, and the surface says it is not exercisable here."""
    import dataclasses

    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_MASTER_USERS", "juancarlos@example.com")
    get_settings.cache_clear()

    operator = Principal(
        tenant_id="default", user_id="usr_1", subject="juancarlos@example.com"
    )
    assert operator.is_master

    assert not dataclasses.replace(operator, authority_allowed=False).is_master


def test_a_configured_group_grants_operator():
    settings = Settings(master_users="", master_groups="sre,platform")

    assert is_operator(
        _principal(subject="a@b.test", groups=frozenset({"platform"})), settings
    )
    assert not is_operator(
        _principal(subject="a@b.test", groups=frozenset({"devs"})), settings
    )


def test_the_service_refuses_to_start_when_master_config_could_over_grant(monkeypatch):
    """`config._id_set` cannot produce an empty entry today, so the only way
    to reach this is that parsing loosening later. The assertion is here
    because that failure is otherwise entirely silent -- an unset variable
    handing every bank to everyone, reported nowhere -- so forcing the state
    is the only way to pin that the refusal is real and loud."""
    from memory.api.app import create_app
    from memory.config import Settings, get_settings

    monkeypatch.setattr(
        Settings, "master_group_ids", property(lambda self: frozenset({""}))
    )
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="MEMORY_MASTER_GROUPS"):
        create_app()


# --- Dispatch: every credential is externally issued -----------------------


def test_a_bearer_token_goes_to_the_jwt_provider(session, jwt_enabled):
    principal = resolve_principal("Bearer some.jwt.token", session)

    assert jwt_enabled == ["some.jwt.token"]
    assert principal.user_id == "usr_jwt"


def test_a_mem_prefixed_token_is_no_longer_special(session, jwt_enabled):
    """The `mem_` prefix existed to discriminate a local key from a JWT on
    Authorization. With no local keys there is nothing to discriminate, so a
    token that happens to start with it is simply a token."""
    resolve_principal("Bearer mem_looks_like_the_old_thing", session)

    assert jwt_enabled == ["mem_looks_like_the_old_thing"]


def test_the_dedicated_header_wins_over_authorization(session, jwt_enabled):
    """Precedence is the whole point: whatever a proxy leaves in
    Authorization must not override the credential the caller explicitly
    nominated for this service."""
    resolve_principal("Bearer from_a_proxy", session, api_key="mine")

    assert jwt_enabled == ["mine"]


def test_the_dedicated_header_tolerates_a_bearer_prefix(session, jwt_enabled):
    resolve_principal(None, session, api_key="Bearer mine")

    assert jwt_enabled == ["mine"]


def test_a_blank_dedicated_header_does_not_fall_back_to_authorization(
    session, jwt_enabled
):
    """A present-but-empty dedicated header is a caller error, not an absent
    one. Falling through would authenticate as whoever Authorization names --
    the confused deputy this precedence exists to prevent."""
    with pytest.raises(Unauthorized, match=API_KEY_HEADER):
        resolve_principal("Bearer from_a_proxy", session, api_key="   ")

    assert jwt_enabled == []


def test_missing_credential_is_unauthorized(session):
    with pytest.raises(Unauthorized, match="missing or malformed"):
        resolve_principal(None, session)


def test_non_bearer_authorization_is_unauthorized(session):
    with pytest.raises(Unauthorized, match="missing or malformed"):
        resolve_principal("Basic abc", session)


def test_a_token_with_no_provider_enabled_says_this_service_mints_none(session):
    """The only refusal left, so it has to be the one that explains the new
    model: there is no key to be missing, only a provider to configure."""
    with pytest.raises(Unauthorized, match="mints no credentials of its own"):
        resolve_principal("Bearer eyJhbGciOiJFZERTQSJ9.e30.sig", session)
