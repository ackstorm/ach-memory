import time
from collections import defaultdict, deque
from typing import ClassVar

import httpx
import pytest
import respx

from memory.errors import RateLimited
from memory.ratelimit import Limiter
from tests.conftest import OPERATOR_SUBJECT


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


def _operator(credential_id: str = "ext_operator"):
    """A principal with operator authority, built directly.

    `ratelimit.check` reads only `credential_id`, so authority is irrelevant to
    the bucket key -- but these two tests are ABOUT the operator/delegation
    path, so the principal is spelled the way the operator plane produces one:
    an ordinary external identity (`subject`, `credential_id`) that
    configuration would name in MEMORY_MASTER_USERS.
    """
    from memory.auth.principal import Principal

    return Principal(
        tenant_id="default",
        user_id="usr_operator",
        credential_id=credential_id,
        subject=OPERATOR_SUBJECT,
    )


def _retain_body(**overrides) -> dict:
    import uuid

    body = {
        "scope": "user",
        "content": "x",
        "memory_type": "fact",
        "basis": "human_explicit",
        "trigger": "agent_proactive",
        "evidence": [{"kind": "user_quote", "raw": "x"}],
        "operation_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _lower_the_limit(monkeypatch) -> None:
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()


@respx.mock
def test_a_write_over_the_limit_gets_429_rate_limited(
    client, new_user, tenant, monkeypatch
):
    _lower_the_limit(monkeypatch)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    headers = new_user()["headers"]

    ok = client.post(
        "/v1/memory/retain", json=_retain_body(), headers=headers
    )
    assert ok.status_code == 202, ok.text

    refused = client.post(
        "/v1/memory/retain", json=_retain_body(content="y"), headers=headers
    )
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"


@respx.mock
def test_a_read_route_is_not_rate_limited(client, new_user, tenant, monkeypatch):
    """A genuinely non-creating read (`create=False`, `is_write=False`) must
    never be touched by the write limiter. `recall` used to be the subject
    here, but it defaults `create=True` -- it can mint a Project row per call
    (see test_a_recall_loop_is_rate_limited below) and is itself now
    `is_write=True`, so it is the wrong tool to prove "reads are unlimited"
    with. `list` never creates anything and stays unmetered."""
    _lower_the_limit(monkeypatch)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    headers = new_user()["headers"]

    warmup = client.post(
        "/v1/memory/retain", json=_retain_body(), headers=headers
    )
    assert warmup.status_code == 202, warmup.text

    for _ in range(3):
        response = client.post(
            "/v1/memory/list", json={"scope": "user"}, headers=headers
        )
        assert response.status_code == 200


@respx.mock
def test_recall_missing_projects_are_not_created_or_rate_limited(
    client, new_user, tenant, monkeypatch
):
    """Recall is a read and refuses unknown projects before Hindsight."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    headers = new_user()["headers"]

    ok = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "loop-1", "query": "x"},
        headers=headers,
    )
    assert ok.status_code == 404

    refused = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "loop-2", "query": "x"},
        headers=headers,
    )
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "PROJECT_NOT_FOUND"


@respx.mock
def test_the_limit_is_shared_across_rest_and_mcp_for_the_same_credential(
    client, new_user, tenant, monkeypatch
):
    """A caller must not evade the limit by switching surfaces: exhaust it
    over REST, then the MCP twin for the SAME credential must also refuse."""
    from memory.mcp.tools import REGISTRY, MCPToolError

    _lower_the_limit(monkeypatch)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    headers = new_user()["headers"]

    ok = client.post(
        "/v1/memory/retain", json=_retain_body(), headers=headers
    )
    assert ok.status_code == 202, ok.text

    # Bound outside the class body on purpose: `headers` is also the
    # attribute name below, and a class body's LOAD_NAME would then skip the
    # enclosing function scope entirely.
    mcp_headers = dict(headers)

    class _Ctx:
        # The SAME identity the REST call above sent, so the two surfaces
        # really do resolve to one credential_id -- which is the whole claim.
        headers: ClassVar = mcp_headers

    with pytest.raises(MCPToolError) as exc_info:
        REGISTRY["retain"](
            ctx=_Ctx(), scope="user", content="y", memory_type="fact",
            basis="human_explicit", trigger="agent_proactive",
            evidence=[{"kind": "user_quote", "raw": "y"}],
        )

    assert exc_info.value.code == "RATE_LIMITED"


def test_delegated_master_traffic_is_bucketed_per_subject(monkeypatch):
    """SPEC §16.5: ACH calls with operator authority plus On-Behalf-Of when
    acting for a human, so ONE operator credential fronts every ACH-mediated
    user. One bucket for all of it means 20 developers share a 60/min ceiling
    while each direct caller gets its own, making the delegated path 20x
    stricter than the direct one and letting one runaway agent 429 everybody
    (2026-08-23 review, R1-#3).

    Still true under external identity: the shared credential is no longer a
    minted master key but the one `ext_` credential ACH authenticates with,
    and the fairness split it needs is unchanged."""
    from memory import ratelimit

    limiter = ratelimit.Limiter(limit=1, window_seconds=60)
    monkeypatch.setattr(ratelimit, "get_limiter", lambda: limiter)

    operator = _operator()

    ratelimit.check(operator, on_behalf_of="alice")
    with pytest.raises(RateLimited):
        ratelimit.check(operator, on_behalf_of="alice")

    # Bob is a different human behind the same operator credential.
    ratelimit.check(operator, on_behalf_of="bob")


def test_the_limiter_is_keyed_per_credential_through_a_route(
    client, new_user, tenant, monkeypatch
):
    """tests above exercise Limiter directly and never touch check()'s key
    derivation. Mutating `principal.credential_id` to a constant survived the
    whole suite: every credential in the tenant would then share one bucket --
    a trivial tenant-wide DoS contradicting §20's "per credential" MUST
    (2026-08-23 review, R4-I3)."""
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()

    alice, bob = new_user()["headers"], new_user()["headers"]

    with respx.mock:
        respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
            return_value=httpx.Response(200, json={})
        )
        body = _retain_body()
        assert client.post("/v1/memory/retain", json=body, headers=alice).status_code == 202
        assert client.post("/v1/memory/retain", json=body, headers=alice).status_code == 429
        # Bob's own bucket must be untouched.
        assert client.post(
            "/v1/memory/retain", json=_retain_body(), headers=bob
        ).status_code == 202


def test_two_external_identities_do_not_share_a_bucket(monkeypatch):
    """Before credential_id, every external caller had key_id=None and fell
    through to the master bucket -- one 60-writes-per-minute ceiling for the
    entire fleet, SPEC §20's per-credential MUST failing with no error and no
    log."""
    from memory import ratelimit
    from memory.auth.principal import Principal

    limiter = ratelimit.Limiter(limit=1, window_seconds=60)
    monkeypatch.setattr(ratelimit, "get_limiter", lambda: limiter)

    alice = Principal(
        tenant_id="default", user_id="usr_a", credential_id="ext_alice",
        subject="alice@test",
    )
    bob = Principal(
        tenant_id="default", user_id="usr_b", credential_id="ext_bob",
        subject="bob@test",
    )

    ratelimit.check(alice)
    ratelimit.check(bob)  # a different credential, so a different bucket

    with pytest.raises(RateLimited):
        ratelimit.check(alice)
