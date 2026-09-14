from functools import lru_cache

import httpx
import pytest

from memory.auth.providers import platform
from memory.errors import Unauthorized, UpstreamError

RESOLVER = "https://api.example.com/v2/user/info"


@pytest.fixture(autouse=True)
def _platform_env(monkeypatch):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_DATABASE_URL", "postgresql+psycopg://x/y")
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


def _mock(handler):
    """Route the provider's cached client through a MockTransport (respx is
    not installed here). `lru_cache`d like the real `_client`, so the
    provider's own `reset_cache()` can still call `.cache_clear()` on it."""

    @lru_cache
    def client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return client


def _respond(status_code=200, json=None):
    def handler(request):
        return httpx.Response(status_code, json=json)

    return handler


def test_resolves_user_and_team(db, monkeypatch):
    monkeypatch.setattr(
        platform,
        "_client",
        _mock(_respond(json={"user_id": "alice@example.com", "team_id": "platform"})),
    )
    principal = platform.authenticate("sk-abc", db)
    assert principal.user_id.startswith("usr_")
    assert principal.groups == frozenset({"platform"})


def test_a_missing_team_is_no_groups(db, monkeypatch):
    monkeypatch.setattr(
        platform, "_client", _mock(_respond(json={"user_id": "alice@example.com"}))
    )
    assert platform.authenticate("sk-abc", db).groups == frozenset()


def test_the_second_call_is_served_from_cache(db, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"user_id": "alice@example.com", "team_id": "platform"})

    monkeypatch.setattr(platform, "_client", _mock(handler))
    platform.authenticate("sk-abc", db)
    platform.authenticate("sk-abc", db)
    assert len(calls) == 1


def test_a_rejected_key_is_unauthorized(db, monkeypatch):
    monkeypatch.setattr(platform, "_client", _mock(_respond(401)))
    with pytest.raises(Unauthorized):
        platform.authenticate("sk-bad", db)


def test_a_rejection_is_not_cached(db, monkeypatch):
    """Caching a 401 would keep refusing a key for its whole TTL after the
    platform re-enabled it."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401)

    monkeypatch.setattr(platform, "_client", _mock(handler))
    for _ in range(2):
        with pytest.raises(Unauthorized):
            platform.authenticate("sk-bad", db)
    assert len(calls) == 2


def test_an_unreachable_resolver_is_not_reported_as_a_bad_key(db, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(platform, "_client", _mock(handler))
    with pytest.raises(UpstreamError):
        platform.authenticate("sk-abc", db)


def test_a_response_without_user_id_is_unauthorized(db, monkeypatch):
    monkeypatch.setattr(platform, "_client", _mock(_respond(json={"team_id": "x"})))
    with pytest.raises(Unauthorized):
        platform.authenticate("sk-abc", db)


def test_a_dotted_path_reads_a_wrapped_answer(db, monkeypatch):
    """LiteLLM's /key/info wraps both fields under `info` -- the whole reason
    these are paths and not plain key names."""
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_USER_FIELD", "info.user_id")
    monkeypatch.setenv("MEMORY_AUTH_PLATFORM_GROUPS_FIELD", "info.team_id")
    get_settings.cache_clear()
    monkeypatch.setattr(
        platform,
        "_client",
        _mock(_respond(json={"info": {"user_id": "pepe", "team_id": "platform"}})),
    )
    principal = platform.authenticate("sk-abc", db)
    assert principal.groups == frozenset({"platform"})


def test_a_path_through_a_non_dict_is_unauthorized(db, monkeypatch):
    """`data.user_id` against {"data": "nope"} refuses, never raises: a
    resolver answering an unexpected shape is a 401, never a 500."""
    monkeypatch.setattr(platform, "_client", _mock(_respond(json={"data": "nope"})))
    with pytest.raises(Unauthorized):
        platform.authenticate("sk-abc", db)
