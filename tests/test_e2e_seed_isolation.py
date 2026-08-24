"""Contract tests for the E2E user-memory isolation prerequisite."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_e2e(monkeypatch):
    """Load the scenario definitions without running the E2E runner."""
    monkeypatch.setenv("MEMORY_MASTER_KEY", "unit-test-master-key")
    path = Path(__file__).parents[1] / "scripts" / "e2e.py"
    spec = importlib.util.spec_from_file_location("e2e_contract_target", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_user_isolation_requires_a_successful_seed_write(monkeypatch):
    e2e = _load_e2e(monkeypatch)
    e2e.S.clear()
    e2e.S.update({"key.alice": "alice-key", "key.bob": "bob-key"})

    async def fake_call(*args, **kwargs):
        return 200, {"result": {"memories": []}}

    monkeypatch.setattr(e2e, "call", fake_call)
    scenario = next(
        fn
        for name, fn in e2e.SCENARIOS
        if name == "memory.second_user_cannot_see_first_users_memory"
    )

    with pytest.raises(AssertionError, match="memory.user_seed_written"):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "data"),
    [
        (200, {"result": {"status": "cancelled"}}),
        (409, {"error": {"code": "OPERATION_NOT_CANCELLABLE"}}),
    ],
)
def test_cancel_accepts_only_success_or_typed_not_cancellable(monkeypatch, status, data):
    e2e = _load_e2e(monkeypatch)

    assert e2e.acceptable_race_outcome(status, data)


@pytest.mark.parametrize(
    ("status", "data"),
    [
        (401, {"error": {"code": "UNAUTHORIZED"}}),
        (403, {"error": {"code": "FORBIDDEN"}}),
        (404, {"error": {"code": "OPERATION_NOT_FOUND"}}),
        (409, {"error": {"code": "OPERATION_NOT_FOUND"}}),
        (409, {"error": {"code": "RATE_LIMITED"}}),
        (409, {"error": {"code": "HINDSIGHT_ERROR"}}),
        (409, {"error": {"code": "INTERNAL_ERROR"}}),
        (502, {"error": {"code": "HINDSIGHT_ERROR"}}),
        (409, {}),
        (409, {"error": {}}),
        (409, {"error": None}),
        (409, None),
        (409, "not-json"),
        (409, ["not", "an", "object"]),
    ],
)
def test_cancel_rejects_other_statuses_codes_and_bodies(monkeypatch, status, data):
    e2e = _load_e2e(monkeypatch)

    assert not e2e.acceptable_race_outcome(status, data)
