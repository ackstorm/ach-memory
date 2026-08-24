from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


def _load_e2e(monkeypatch):
    monkeypatch.setenv("MEMORY_MASTER_KEY", "unit-test-master-key")
    path = Path(__file__).parents[1] / "scripts" / "e2e.py"
    spec = importlib.util.spec_from_file_location("e2e_rate_limit_target", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scenario(e2e):
    return next(
        fn
        for name, fn in e2e.SCENARIOS
        if name == "ratelimit.exhaust_write_budget_then_confirm_isolation"
    )


def test_rate_limit_probe_uses_directives_until_429_then_one_isolation_retain(
    monkeypatch,
) -> None:
    e2e = _load_e2e(monkeypatch)
    e2e.S.clear()
    e2e.S.update({"key.ratelimituser": "limited-key", "key.alice": "alice-key"})
    calls = []

    async def fake_call(method, path, key, *, json_body=None, params=None, timeout=30.0):
        calls.append((method, path, key, json_body, params, timeout))
        directive_attempts = sum(call[1] == "/v1/directives" for call in calls)
        if path == "/v1/directives" and directive_attempts < 4:
            return 201, {"result": {"id": f"directive-{directive_attempts}"}}
        if path == "/v1/directives":
            return 429, {"error": {"code": "RATE_LIMITED", "message": "slow down"}}
        return 200, {"result": {"operation_id": "op-isolation"}}

    monkeypatch.setattr(e2e, "call", fake_call)

    asyncio.run(_scenario(e2e)())

    paths = [call[1] for call in calls]
    assert paths == ["/v1/directives"] * 4 + ["/v1/memory/retain"]
    assert [call[2] for call in calls] == ["limited-key"] * 4 + ["alice-key"]
    names = [call[3]["name"] for call in calls[:3]]
    assert len(names) == len(set(names)) == 3


def test_rate_limit_probe_rejects_an_error_before_rate_limit(monkeypatch) -> None:
    e2e = _load_e2e(monkeypatch)
    e2e.S.clear()
    e2e.S.update({"key.ratelimituser": "limited-key", "key.alice": "alice-key"})
    responses = iter(
        [
            (201, {"result": {"id": "directive-1"}}),
            (502, {"error": {"code": "HINDSIGHT_ERROR"}}),
            (429, {"error": {"code": "RATE_LIMITED"}}),
            (200, {"result": {"operation_id": "op-isolation"}}),
        ]
    )
    calls = []

    async def fake_call(method, path, key, *, json_body=None, params=None, timeout=30.0):
        calls.append((method, path, key, json_body, params, timeout))
        return next(responses)

    monkeypatch.setattr(e2e, "call", fake_call)

    with pytest.raises(AssertionError, match="expected HTTP 201"):
        asyncio.run(_scenario(e2e)())
    assert len(calls) == 2
