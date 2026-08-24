from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

VALID_MOCK_RESULT = {
    "text": "mock response",
    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
}


def _load_e2e(monkeypatch, provider: str):
    monkeypatch.setenv("MEMORY_MASTER_KEY", "unit-test-master-key")
    monkeypatch.setenv("HINDSIGHT_LLM_PROVIDER", provider)
    path = Path(__file__).parents[1] / "scripts" / "e2e.py"
    spec = importlib.util.spec_from_file_location("e2e_mock_reflect_target", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rest_mock_reflect_makes_one_call_and_validates_the_result(monkeypatch) -> None:
    e2e = _load_e2e(monkeypatch, "mock")
    calls = []

    async def fake_call(method, path, key, *, json_body=None, params=None, timeout=30.0):
        calls.append((method, path, key, json_body, params, timeout))
        return 200, {"result": VALID_MOCK_RESULT}

    monkeypatch.setattr(e2e, "call", fake_call)

    result = asyncio.run(
        e2e.reflect_with_retry(
            {"scope": "user", "query": "deps?"}, "key", "uv", delay=0
        )
    )

    assert result == {"result": VALID_MOCK_RESULT}
    assert len(calls) == 1


def test_mcp_mock_reflect_makes_one_call_and_validates_the_payload(monkeypatch) -> None:
    e2e = _load_e2e(monkeypatch, "mock")
    calls = []

    async def fake_tool(name, args):
        calls.append((name, args))
        return VALID_MOCK_RESULT

    result = asyncio.run(e2e.mcp_reflect_with_retry(fake_tool, {"scope": "user"}, "uv"))

    assert result == VALID_MOCK_RESULT
    assert calls == [("reflect", {"scope": "user"})]


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"text": ""},
        {"text": 123},
        {"text": "mock response", "usage": "not-an-object"},
        {
            "text": "mock response",
            "usage": {"input_tokens": 10, "output_tokens": -1, "total_tokens": 9},
        },
        {
            "text": "mock response",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 99},
        },
    ],
)
def test_mock_reflect_rejects_malformed_results(monkeypatch, result) -> None:
    e2e = _load_e2e(monkeypatch, "mock")

    with pytest.raises(AssertionError):
        e2e.validate_mock_reflect_result(result, "reflect")


def test_mcp_mock_reflect_does_not_hide_tool_errors(monkeypatch) -> None:
    e2e = _load_e2e(monkeypatch, "mock")

    async def failing_tool(name, args):
        raise AssertionError(f"{name}: tool call failed")

    with pytest.raises(AssertionError, match="reflect: tool call failed"):
        asyncio.run(e2e.mcp_reflect_with_retry(failing_tool, {"scope": "user"}, "uv"))


def test_non_mock_reflect_keeps_keyword_retry_behavior(monkeypatch) -> None:
    e2e = _load_e2e(monkeypatch, "openai")
    responses = iter(
        [
            {"text": "not consolidated yet"},
            {"text": "dependencies use uv"},
        ]
    )
    calls = []

    async def fake_tool(name, args):
        calls.append((name, args))
        return next(responses)

    result = asyncio.run(
        e2e.mcp_reflect_with_retry(fake_tool, {"scope": "user"}, "uv", delay=0)
    )

    assert result == {"text": "dependencies use uv"}
    assert len(calls) == 2
