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
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "team_id")
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


@respx.mock
def test_a_list_valued_groups_field_becomes_every_group(session, tenant, monkeypatch):
    """The deployed LiteLLM shape. `/v2/user/info` answers `teams` as a list of
    ids and emits no `team_id` at all, so the cluster points GROUPS_FIELD at
    `teams`. Every other case here is the flat `team_id` alitellm-auth returns;
    without this one the list path ships to production untested."""
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "teams")
    get_settings.cache_clear()
    respx.get(RESOLVER).mock(
        return_value=httpx.Response(
            200, json={"user_id": "alice@example.com", "teams": ["team-a", "team-b"]}
        )
    )
    principal = platform.authenticate("sk-abc", session)
    assert principal.groups == frozenset({"team-a", "team-b"})


@respx.mock
def test_the_wrong_groups_field_authenticates_with_no_groups(session, tenant):
    """The trap that made both fields required, pinned. Naming `team_id` while
    the resolver answers `teams` is not an error anywhere:
    the caller authenticates and silently carries no group membership, so every
    group-owned project quietly stops authorizing."""
    respx.get(RESOLVER).mock(
        return_value=httpx.Response(
            200, json={"user_id": "alice@example.com", "teams": ["team-a"]}
        )
    )
    principal = platform.authenticate("sk-abc", session)
    assert principal.user_id
    assert principal.groups == frozenset()


@respx.mock
def test_a_dotted_path_reads_a_wrapped_answer(session, tenant, monkeypatch):
    """LiteLLM's /key/info wraps both fields under `info`, so the flat lookup
    that works for /v2/user/info reads nothing there. Addressing the envelope
    is the whole reason these are paths and not plain key names."""
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "info.user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "info.team_id")
    get_settings.cache_clear()
    respx.get(RESOLVER).mock(
        return_value=httpx.Response(
            200,
            json={"key": "sk-abc", "info": {"user_id": "pepe", "team_id": "platform"}},
        )
    )
    principal = platform.authenticate("sk-abc", session)
    assert principal.user_id
    assert principal.groups == frozenset({"platform"})


@respx.mock
def test_a_path_through_a_non_dict_is_unauthorized(session, tenant, monkeypatch):
    """`data.user_id` against {"data": "nope"} must refuse, not raise. A
    resolver answering a shape we did not expect is a 401, never a 500."""
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "data.user_id")
    get_settings.cache_clear()
    respx.get(RESOLVER).mock(return_value=httpx.Response(200, json={"data": "nope"}))
    with pytest.raises(Unauthorized):
        platform.authenticate("sk-abc", session)


@respx.mock
def test_a_path_deeper_than_the_payload_is_unauthorized(session, tenant, monkeypatch):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "a.b.c.d")
    get_settings.cache_clear()
    respx.get(RESOLVER).mock(return_value=httpx.Response(200, json={"a": {"b": {}}}))
    with pytest.raises(Unauthorized):
        platform.authenticate("sk-abc", session)


@pytest.mark.parametrize(
    "unset", ["MEMORY_AUTH_PLATFORM_USER_FIELD", "MEMORY_AUTH_PLATFORM_GROUPS_FIELD"]
)
def test_platform_auth_refuses_to_start_without_both_fields(monkeypatch, unset):
    """No default is right for every resolver, and guessing wrong is silent:
    a groups path that matches nothing still authenticates the caller and just
    leaves them with no membership. Refusing to boot is the loud version."""
    from memory.config import get_settings

    monkeypatch.setenv(unset, "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match=unset):
        get_settings()
    get_settings.cache_clear()
