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
