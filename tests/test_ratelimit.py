import time
from collections import defaultdict, deque
from typing import ClassVar

import httpx
import pytest
import respx

from memory.errors import RateLimited
from memory.ratelimit import Limiter


def test_it_allows_up_to_the_limit_then_refuses():
    limiter = Limiter(limit=3, window_seconds=60.0, now=iter([1.0, 1.1, 1.2, 1.3]).__next__)

    for _ in range(3):
        limiter.check("key_a")
    with pytest.raises(RateLimited):
        limiter.check("key_a")


def test_credentials_are_counted_separately():
    limiter = Limiter(limit=1, window_seconds=60.0, now=lambda: 1.0)

    limiter.check("key_a")
    limiter.check("key_b")


def test_the_window_slides():
    clock = iter([1.0, 2.0, 100.0])
    limiter = Limiter(limit=1, window_seconds=60.0, now=clock.__next__)

    limiter.check("key_a")
    with pytest.raises(RateLimited):
        limiter.check("key_a")
    limiter.check("key_a")  # the first call has aged out


def test_the_error_says_when_to_retry():
    limiter = Limiter(limit=1, window_seconds=60.0, now=lambda: 1.0)
    limiter.check("key_a")

    with pytest.raises(RateLimited) as caught:
        limiter.check("key_a")

    assert caught.value.details["retry_after_seconds"] > 0


def test_quiet_credentials_are_never_removed_from_the_hits_dict():
    """The comment above `Limiter.check` used to claim a quiet key
    'eventually frees its own list instead of growing it forever' -- it does
    not: `check()` drains a key's own deque back to empty on its NEXT call,
    but nothing ever pops the dict entry itself, and a key that goes quiet
    never calls again to trigger even that. Measured live: 100_000 one-shot
    credentials retained 100_000 entries. This pins the same shape at a
    tractable size."""
    limiter = Limiter(limit=1, window_seconds=0.001, now=lambda: 0.0)

    for i in range(50):
        limiter.check(f"key_{i}")

    assert len(limiter._hits) == 50


class _SlowAppendDeque(deque):
    """Widens check()'s read-check-append window so 40 real OS threads
    reliably land inside it.

    Without this, the GIL alone serializes the few microseconds of pure-Python
    work in `check()` tightly enough that 40 threads racing on a bare
    `Limiter` almost never actually interleave (verified empirically: 20/20
    unlocked runs came back exactly at the limit with no artificial delay).
    Sleeping specifically inside `append()` -- AFTER the length check has
    already made its admit/refuse decision on the real, unmodified length --
    reproduces the real hazard: many threads can decide "admit" from a
    consistent read, then all actually mutate the shared deque only once
    they're all past the decision point.
    """

    def append(self, value):
        time.sleep(0.01)
        super().append(value)


def test_concurrent_checks_never_exceed_the_limit():
    """Sync routes run in Starlette's threadpool and MCP tools in AnyIO's
    40-thread pool, so one credential really does reach check() concurrently
    (review finding I8). Distinct from the documented per-replica multiplier.

    Verified unlocked vs. locked by hand (not part of the assertion, since a
    hand run isn't repeatable in CI): 20/20 runs with no lock in `check()`
    admitted all 40 attempts; 20/20 runs with the lock admitted exactly 10.
    """
    import threading

    limiter = Limiter(limit=10, window_seconds=60.0)
    limiter._hits = defaultdict(_SlowAppendDeque)
    admitted = []
    lock = threading.Lock()
    start = threading.Barrier(40)

    def attempt():
        start.wait()
        try:
            limiter.check("one-key")
        except RateLimited:
            return
        with lock:
            admitted.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a thread hung"

    assert len(admitted) == 10


BASE = "http://hindsight.test"


def _mock_bank() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


def _make_user_key(client, master_headers) -> str:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    return client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]


def _lower_the_limit(monkeypatch) -> None:
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()


@respx.mock
def test_a_write_over_the_limit_gets_429_rate_limited(
    client, master_headers, tenant, monkeypatch
):
    _lower_the_limit(monkeypatch)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    key = _make_user_key(client, master_headers)
    headers = {"Authorization": f"Bearer {key}"}

    ok = client.post(
        "/v1/memory/retain", json={"scope": "user", "content": "x"}, headers=headers
    )
    assert ok.status_code == 200

    refused = client.post(
        "/v1/memory/retain", json={"scope": "user", "content": "y"}, headers=headers
    )
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"


@respx.mock
def test_a_read_route_is_not_rate_limited(client, master_headers, tenant, monkeypatch):
    """A genuinely non-creating read (`create=False`, `is_write=False`) must
    never be touched by the write limiter. `recall` used to be the subject
    here, but it defaults `create=True` -- it can mint a Project row per call
    (see test_a_recall_loop_is_rate_limited below) and is itself now
    `is_write=True`, so it is the wrong tool to prove "reads are unlimited"
    with. `list` never creates anything and stays unmetered."""
    _lower_the_limit(monkeypatch)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    key = _make_user_key(client, master_headers)
    headers = {"Authorization": f"Bearer {key}"}

    client.post(
        "/v1/memory/retain", json={"scope": "user", "content": "x"}, headers=headers
    )

    for _ in range(3):
        response = client.post(
            "/v1/memory/list", json={"scope": "user"}, headers=headers
        )
        assert response.status_code == 200


@respx.mock
def test_a_recall_loop_is_rate_limited(client, master_headers, tenant, monkeypatch):
    """recall defaults create=True: unmetered, it mints one Project row per
    call against a random project_slug, each one permanently squatting a
    tenant-unique slug (invariant 8 -- unique across live AND retired names,
    never recoverable). Measured live at 80 projects in 5.1s against one key
    with no limiter on this route. recall is now is_write=True for exactly
    that reason, the same rationale reflect already used for spending model
    tokens on an unattributed server-level key."""
    _lower_the_limit(monkeypatch)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    key = _make_user_key(client, master_headers)
    headers = {"Authorization": f"Bearer {key}"}

    ok = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "loop-1", "query": "x"},
        headers=headers,
    )
    assert ok.status_code == 200

    refused = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "loop-2", "query": "x"},
        headers=headers,
    )
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"


@respx.mock
def test_the_limit_is_shared_across_rest_and_mcp_for_the_same_credential(
    client, master_headers, tenant, monkeypatch
):
    """A caller must not evade the limit by switching surfaces: exhaust it
    over REST, then the MCP twin for the SAME credential must also refuse."""
    from memory.mcp.tools import REGISTRY, MCPToolError

    _lower_the_limit(monkeypatch)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    key = _make_user_key(client, master_headers)
    headers = {"Authorization": f"Bearer {key}"}

    ok = client.post(
        "/v1/memory/retain", json={"scope": "user", "content": "x"}, headers=headers
    )
    assert ok.status_code == 200

    class _Ctx:
        headers: ClassVar = {"authorization": f"Bearer {key}"}

    with pytest.raises(MCPToolError) as exc_info:
        REGISTRY["retain"](ctx=_Ctx(), scope="user", content="y")

    assert exc_info.value.code == "RATE_LIMITED"


def test_delegated_master_traffic_is_bucketed_per_subject(monkeypatch):
    """SPEC §16.5: ACH calls with the master key plus On-Behalf-Of when acting
    for a human, so the master key is the SHARED credential for every
    ACH-mediated user -- not one operator's key. One bucket for all of it means
    20 developers share a 60/min ceiling while each direct user key gets its
    own, making the delegated path 20x stricter than the direct one and letting
    one runaway agent 429 everybody (2026-08-23 review, R1-#3)."""
    from memory import ratelimit
    from memory.auth.principal import Principal

    limiter = ratelimit.Limiter(limit=1, window_seconds=60)
    monkeypatch.setattr(ratelimit, "get_limiter", lambda: limiter)

    master = Principal(tenant_id="default", user_id=None, is_master=True, key_id=None)

    ratelimit.check(master, on_behalf_of="alice")
    with pytest.raises(RateLimited):
        ratelimit.check(master, on_behalf_of="alice")

    # Bob is a different human behind the same master key.
    ratelimit.check(master, on_behalf_of="bob")


def test_the_limiter_is_keyed_per_credential_through_a_route(
    client, master_headers, tenant, monkeypatch
):
    """tests above exercise Limiter directly and never touch check()'s key
    derivation. Mutating `principal.key_id or MASTER_KEY_ID` to a constant
    survived the whole suite: every credential in the tenant would then share
    one bucket -- a trivial tenant-wide DoS contradicting §20's "per
    credential" MUST (2026-08-23 review, R4-I3)."""
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()

    def _key() -> dict[str, str]:
        uid = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
        secret = client.post(
            f"/v1/users/{uid}/keys", json={}, headers=master_headers
        ).json()["key"]
        return {"Authorization": f"Bearer {secret}"}

    alice, bob = _key(), _key()

    with respx.mock:
        respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
            return_value=httpx.Response(200, json={})
        )
        body = {"scope": "user", "content": "x"}
        assert client.post("/v1/memory/retain", json=body, headers=alice).status_code == 200
        assert client.post("/v1/memory/retain", json=body, headers=alice).status_code == 429
        # Bob's own bucket must be untouched.
        assert client.post("/v1/memory/retain", json=body, headers=bob).status_code == 200
