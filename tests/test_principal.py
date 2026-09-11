import pytest

from memory.auth.principal import (
    Principal,
    is_operator,
    resolve_principal,
)
from memory.config import Settings
from memory.errors import Unauthorized

#: A token that is structurally a JWT and nothing more: a real JOSE header,
#: an empty payload, a meaningless signature. `looks_like_jwt` parses only
#: the header, so this is exactly enough to be ROUTED to the JWT provider --
#: which is what these tests are about. Real signature verification is
#: tests/test_auth_jwt.py's.
JWT_SHAPED = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.not-a-real-signature"


@pytest.fixture
def platform(monkeypatch):
    """Spy on the platform provider, for the same reason `jwt_enabled` spies
    on the JWT one: what the dispatcher owns is the choice, not the check."""
    from memory.auth.providers import platform as platform_provider
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "authorization")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_URL", "http://identity.test/whoami")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_RESOLVER_HEADER", "x-resolver-key")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "groups")
    get_settings.cache_clear()

    seen: list[str] = []

    def _authenticate(token, db):
        seen.append(token)
        return Principal(tenant_id="default", user_id="usr_platform", credential_id="ext_2")

    monkeypatch.setattr(platform_provider, "authenticate", _authenticate)
    return seen


@pytest.fixture
def jwt_rejecting(monkeypatch):
    """The JWT provider as it behaves for a token it refuses."""
    from memory.auth.providers import jwt_provider
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_JWT_ENABLED", "true")
    monkeypatch.setenv("MEMORY_AUTH_JWT_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("MEMORY_AUTH_JWT_AUDIENCE", "mcp:ach-memory")
    get_settings.cache_clear()

    def _authenticate(token, db):
        raise Unauthorized("token rejected")

    monkeypatch.setattr(jwt_provider, "authenticate", _authenticate)


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
    return Principal(credential_id="ext_test",
               
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

    operator = Principal(credential_id="ext_test",
                   
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


def test_a_jwt_shaped_token_goes_to_the_jwt_provider(session, jwt_enabled):
    principal = resolve_principal(f"Bearer {JWT_SHAPED}", session)

    assert jwt_enabled == [JWT_SHAPED]
    assert principal.user_id == "usr_jwt"


def test_an_opaque_token_goes_to_the_platform_resolver(session, jwt_enabled, platform):
    """Both providers enabled, one header: the token's own shape decides, so
    there is no precedence rule to get wrong. An opaque string is not a JWT
    and only the resolver can name it."""
    resolve_principal("Bearer mem_looks_like_the_old_thing", session,
                      platform_token="mem_looks_like_the_old_thing")

    assert jwt_enabled == []
    assert platform == ["mem_looks_like_the_old_thing"]


def test_a_rejected_jwt_never_falls_through_to_the_platform_resolver(
    session, jwt_rejecting, platform
):
    """The property the shape check buys. Falling through here would
    authenticate a caller whose JWT was forged, expired or signed by the
    wrong issuer as whoever the platform header names -- a confused deputy
    that stays invisible until it matters."""
    with pytest.raises(Unauthorized):
        resolve_principal(f"Bearer {JWT_SHAPED}", session, platform_token="somebody_else")

    assert platform == []


def test_missing_credential_is_unauthorized(session):
    with pytest.raises(Unauthorized, match="missing or malformed"):
        resolve_principal(None, session)


def test_non_bearer_authorization_is_unauthorized(session):
    with pytest.raises(Unauthorized, match="missing or malformed"):
        resolve_principal("Basic abc", session)


def test_a_missing_credential_names_the_platform_header_this_deployment_reads(
    session, platform, monkeypatch
):
    """QA F-27: production runs platform-only on `x-litellm-api-key`, where
    `Authorization: Bearer` is refused -- so a fixed hint naming it sent the
    caller down the one path that 401s."""
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_INCOMING_HEADER", "x-litellm-api-key")
    get_settings.cache_clear()

    with pytest.raises(Unauthorized) as exc:
        resolve_principal(None, session)

    assert "x-litellm-api-key" in exc.value.message
    assert "Authorization: Bearer" not in exc.value.message


def test_a_missing_credential_names_bearer_when_only_jwt_is_enabled(session, jwt_enabled):
    with pytest.raises(Unauthorized) as exc:
        resolve_principal(None, session)

    assert "Authorization: Bearer" in exc.value.message
    assert "x-litellm-api-key" not in exc.value.message


def test_a_token_with_no_provider_enabled_says_this_service_mints_none(session):
    """The only refusal left, so it has to be the one that explains the new
    model: there is no key to be missing, only a provider to configure."""
    with pytest.raises(Unauthorized, match="mints no credentials of its own"):
        resolve_principal("Bearer eyJhbGciOiJFZERTQSJ9.e30.sig", session)


# --- Operator authority is qualified by who vouched for the subject --------


def test_a_subject_from_another_issuer_is_not_the_operator():
    """The escalation this closes: both providers can be enabled at once, and
    a caller picks which one authenticates them by picking which header to
    send. So `MEMORY_MASTER_USERS=jc@example.com` naming a JWT subject was
    equally satisfied by anyone holding a platform credential whose resolver
    returned that same string -- full admin plane and a cross-tenant project
    read, with no bad credential anywhere in the request.

    A subject is only unique within the issuer that minted it, which is why
    `link_identity` keys on the pair.
    """
    settings = Settings(
        master_users="jc@example.com", master_issuer="https://idp.example.com"
    )

    from_the_named_issuer = Principal(credential_id="ext_test",
                                
        tenant_id="default",
        user_id="usr_1",
        subject="jc@example.com",
        issuer="https://idp.example.com",
    )
    from_somewhere_else = Principal(credential_id="ext_test",
                              
        tenant_id="default",
        user_id="usr_2",
        subject="jc@example.com",
        issuer="http://litellm.internal/whoami",
    )

    assert is_operator(from_the_named_issuer, settings)
    assert not is_operator(from_somewhere_else, settings)


def test_a_group_from_another_issuer_is_not_the_operator():
    settings = Settings(
        master_groups="platform-admins", master_issuer="https://idp.example.com"
    )
    impostor = Principal(credential_id="ext_test",
                   
        tenant_id="default",
        user_id="usr_2",
        groups=frozenset({"platform-admins"}),
        issuer="http://litellm.internal/whoami",
    )

    assert not is_operator(impostor, settings)


def test_an_unnamed_issuer_still_grants_when_only_one_provider_is_enabled():
    """The common deployment. Requiring the issuer unconditionally would make
    every single-provider install configure a value with only one possible
    answer, so the refusal lives in the startup assertion instead, which only
    fires when the ambiguity is real."""
    settings = Settings(master_users="jc@example.com")
    principal = Principal(credential_id="ext_test",
                    
        tenant_id="default",
        user_id="usr_1",
        subject="jc@example.com",
        issuer="https://idp.example.com",
    )

    assert is_operator(principal, settings)


def test_a_principal_cannot_exist_without_a_credential_id():
    """Optional, it reached `ratelimit.check` as None and indexed a
    `defaultdict`: no error, and every such caller silently sharing one
    anonymous bucket where SPEC §20 asks for one per credential. Required,
    that is unrepresentable rather than merely unlikely."""
    with pytest.raises(TypeError):
        Principal(tenant_id="default", user_id="usr_x")
