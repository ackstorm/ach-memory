import asyncio
from types import SimpleNamespace

import pytest

from memory import cli


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000/mcp/"),
        ("https://memory.example.com/", "https://memory.example.com/mcp/"),
        ("https://example.com/team/memory", "https://example.com/team/memory/mcp/"),
    ],
)
def test_mcp_url(base: str, expected: str) -> None:
    assert cli._mcp_url(base) == expected


@pytest.mark.parametrize(
    "base",
    [
        "localhost:8000",
        "ftp://example.com",
        "https://example.com/?x=1",
        "https://example.com/#x",
    ],
)
def test_mcp_url_rejects_invalid_input(base: str) -> None:
    with pytest.raises(ValueError):
        cli._mcp_url(base)


def test_main_rejects_missing_or_unknown_target() -> None:
    assert cli.main(["init"]) != 0
    assert cli.main(["init", "unknown"]) != 0


@pytest.mark.parametrize("target", ["codex", "claude", "opencode", "pi", "all"])
def test_main_accepts_targets(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    async def preflight(_url: str, _api_key: str) -> None:
        return None

    monkeypatch.setattr(cli, "_preflight", preflight, raising=False)

    assert cli.main(["init", target]) == 0


def test_preflight_lists_required_tools_without_calling_memory_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers: list[dict[str, str]] = []
    sessions = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeTransport:
        async def __aenter__(self):
            return "read", "write"

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSession:
        def __init__(self, read: str, write: str) -> None:
            assert (read, write) == ("read", "write")
            self.initialized = False
            self.listed = 0
            self.memory_calls = 0
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            self.initialized = True

        async def list_tools(self) -> SimpleNamespace:
            self.listed += 1
            return SimpleNamespace(
                tools=[SimpleNamespace(name="recall"), SimpleNamespace(name="retain")]
            )

        async def call_tool(self, *_args: object) -> None:
            self.memory_calls += 1

    def create_client(value: dict[str, str]) -> FakeClient:
        headers.append(value)
        return FakeClient()

    def streamable_client(_url: str, *, http_client: FakeClient) -> FakeTransport:
        assert isinstance(http_client, FakeClient)
        return FakeTransport()

    monkeypatch.setattr(cli, "create_mcp_http_client", create_client, raising=False)
    monkeypatch.setattr(cli, "streamable_http_client", streamable_client, raising=False)
    monkeypatch.setattr(cli, "ClientSession", FakeSession, raising=False)

    asyncio.run(cli._preflight("https://memory.example.com/mcp/", "user-secret"))

    assert headers == [{"Authorization": "Bearer user-secret"}]
    assert sessions[0].initialized is True
    assert sessions[0].listed == 1
    assert sessions[0].memory_calls == 0


def test_preflight_rejects_empty_key_before_opening_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(_headers: dict[str, str]) -> None:
        pytest.fail("preflight opened a connection without an API key")

    monkeypatch.setattr(cli, "create_mcp_http_client", fail_if_called, raising=False)

    with pytest.raises(cli.CLIError, match="ACH_MEMORY_API_KEY"):
        asyncio.run(cli._preflight("https://memory.example.com/mcp/", ""))
