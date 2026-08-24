import asyncio
import json
import subprocess
from pathlib import Path
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
    installed: list[str] = []
    monkeypatch.setattr(cli, "_require_executable", lambda _target: None, raising=False)
    monkeypatch.setattr(cli, "_install_native", lambda name, _url: installed.append(name), raising=False)
    monkeypatch.setattr(cli, "_install_opencode", lambda _url: installed.append("opencode"), raising=False)
    monkeypatch.setattr(cli, "_install_pi", lambda _url: installed.append("pi"), raising=False)

    assert cli.main(["init", target]) == 0
    assert installed == ([target] if target != "all" else ["codex", "claude", "opencode", "pi"])


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


@pytest.mark.parametrize(
    ("target", "config_name", "server_key", "expected_server", "owned_paths"),
    [
        (
            "opencode",
            "opencode.json",
            "mcp",
            {
                "type": "remote",
                "url": "https://host/next/mcp/",
                "headers": {"Authorization": "Bearer {env:ACH_MEMORY_API_KEY}"},
            },
            [
                "plugins/ach-memory.js",
                "plugins/ach-memory/activation.txt",
                "skills/ach-memory/SKILL.md",
            ],
        ),
        (
            "pi",
            "mcp.json",
            "mcpServers",
            {
                "url": "https://host/next/mcp/",
                "auth": "bearer",
                "bearerTokenEnv": "ACH_MEMORY_API_KEY",
                "lifecycle": "lazy",
                "directTools": False,
            },
            [
                "extensions/ach-memory.js",
                "extensions/ach-memory/activation.txt",
                "skills/ach-memory/SKILL.md",
            ],
        ),
    ],
)
def test_config_install_upserts_only_ach_memory_and_refreshes_owned_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    config_name: str,
    server_key: str,
    expected_server: dict[str, object],
    owned_paths: list[str],
) -> None:
    """Breaks if an install overwrites host settings, persists a key, or leaves stale owned files."""
    root = tmp_path / target
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    if target == "opencode":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        root = tmp_path / "opencode"
    else:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(root))
    root.mkdir(parents=True)
    config = root / config_name
    config.write_text(json.dumps({"unrelated": {"keep": True}, server_key: {"other": {"url": "x"}}}))
    for relative in owned_paths:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("stale")

    commands: list[list[str]] = []
    monkeypatch.setattr(
        cli,
        "_run",
        lambda command: commands.append(command) or subprocess.CompletedProcess(command, 0, "", ""),
    )

    getattr(cli, f"_install_{target}")("https://host/first/mcp/")
    getattr(cli, f"_install_{target}")("https://host/next/mcp/")

    installed = json.loads(config.read_text())
    assert installed["unrelated"] == {"keep": True}
    assert installed[server_key]["other"] == {"url": "x"}
    assert installed[server_key]["ach-memory"] == expected_server
    assert all((root / relative).read_text() != "stale" for relative in owned_paths)
    assert all(b"user-secret" not in (root / relative).read_bytes() for relative in owned_paths)
    assert all("user-secret" not in " ".join(command) for command in commands)
    assert commands == ([] if target == "opencode" else [["pi", "install", "npm:pi-mcp-adapter"], ["pi", "install", "npm:pi-mcp-adapter"]])


@pytest.mark.parametrize(
    ("target", "config_name", "asset"),
    [
        ("opencode", "opencode.json", "plugins/ach-memory.js"),
        ("pi", "mcp.json", "extensions/ach-memory.js"),
    ],
)
def test_config_install_rejects_invalid_json_before_replacing_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, config_name: str, asset: str
) -> None:
    """Breaks if corrupt host JSON is replaced or an owned file changes before parsing completes."""
    root = tmp_path / target
    if target == "opencode":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        root = tmp_path / "opencode"
    else:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(root))
    root.mkdir(parents=True)
    config = root / config_name
    config.write_text("{not json")
    destination = root / asset
    destination.parent.mkdir(parents=True)
    destination.write_text("keep this")
    monkeypatch.setattr(cli, "_run", lambda _command: pytest.fail("Pi ran before config parsing"))

    with pytest.raises(cli.CLIError, match="invalid JSON"):
        getattr(cli, f"_install_{target}")("https://host/prefix/mcp/")

    assert config.read_text() == "{not json"
    assert destination.read_text() == "keep this"


class _NativeRunner:
    def __init__(
        self,
        target: str,
        *,
        marketplace_present: bool,
        installed: bool,
        marketplace_location: str | None = None,
    ) -> None:
        self.target = target
        self.marketplace_present = marketplace_present
        self.installed = installed
        self.marketplace_location = marketplace_location
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[-3:] == ["marketplace", "list", "--json"]:
            payload: object = (
                {"marketplaces": [{"name": "ach-memory", "root": self.marketplace_location}]}
                if self.target == "codex"
                else [{"name": "ach-memory", "installLocation": self.marketplace_location}]
            ) if self.marketplace_present else ({"marketplaces": []} if self.target == "codex" else [])
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[-2:] == ["list", "--json"]:
            payload = (
                {"installed": [{"pluginId": "ach-memory@ach-memory"}]}
                if self.target == "codex"
                else [{"id": "ach-memory@ach-memory"}]
            ) if self.installed else ({"installed": []} if self.target == "codex" else [])
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if "marketplace" in command and "add" in command:
            self.marketplace_present = True
            self.marketplace_location = command[-2] if self.target == "codex" else command[-1]
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.mark.parametrize("target", ["codex", "claude"])
def test_native_install_rejects_an_ach_memory_marketplace_at_a_different_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    """Breaks if a stale same-named marketplace can receive plugin mutations."""
    runner = _NativeRunner(
        target,
        marketplace_present=True,
        installed=False,
        marketplace_location=str(tmp_path / "foreign-marketplace"),
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    with pytest.raises(cli.CLIError, match="different location"):
        cli._install_native(target, "https://host/prefix/mcp/")

    assert not (tmp_path / "data" / "ach-memory" / f"{target}-marketplace").exists()
    assert all(command[-2:] == ["list", "--json"] for command in runner.commands)


def test_native_install_rejects_bare_list_codex_marketplace_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Breaks if Codex's object-shaped marketplace response is treated as a bare list."""
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = [] if "marketplace" in command else {"installed": []}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    with pytest.raises(cli.CLIError, match="unsupported marketplace JSON"):
        cli._install_native("codex", "https://host/prefix/mcp/")

    assert commands == [["codex", "plugin", "marketplace", "list", "--json"]]


def test_native_install_rejects_bare_list_codex_plugin_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Breaks if Codex's object-shaped plugin response is treated as a bare list."""
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = {"marketplaces": []} if "marketplace" in command else []
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    with pytest.raises(cli.CLIError, match="unsupported plugin JSON"):
        cli._install_native("codex", "https://host/prefix/mcp/")

    assert commands == [
        ["codex", "plugin", "marketplace", "list", "--json"],
        ["codex", "plugin", "list", "--json"],
    ]
    assert not (tmp_path / "data" / "ach-memory" / "codex-marketplace").exists()


@pytest.mark.parametrize(
    ("target", "expected_mcp", "install_command"),
    [
        (
            "codex",
            {
                "mcpServers": {
                    "ach-memory": {
                        "type": "http",
                        "url": "https://host/prefix/mcp/",
                        "bearer_token_env_var": "ACH_MEMORY_API_KEY",
                    }
                }
            },
            ["codex", "plugin", "add", "ach-memory@ach-memory", "--json"],
        ),
        (
            "claude",
            {
                "mcpServers": {
                    "ach-memory": {
                        "type": "http",
                        "url": "https://host/prefix/mcp/",
                        "headers": {"Authorization": "Bearer ${ACH_MEMORY_API_KEY}"},
                    }
                }
            },
            ["claude", "plugin", "install", "-y", "--scope", "user", "ach-memory@ach-memory"],
        ),
    ],
)
def test_native_install_renders_an_idempotent_secret_free_marketplace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    expected_mcp: dict[str, object],
    install_command: list[str],
) -> None:
    """Breaks if a native install writes a key, wrong MCP payload, or skips its first install."""
    events: list[str] = []
    runner = _NativeRunner(target, marketplace_present=False, installed=False)
    real_copytree = cli.shutil.copytree

    def which(command: str) -> str:
        events.append(f"which:{command}")
        return f"/bin/{command}"

    def copytree(source: Path, destination: Path, *_args: object, **_kwargs: object) -> Path:
        events.append("copytree")
        return real_copytree(source, destination, *_args, **_kwargs)

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli.shutil, "copytree", copytree)
    monkeypatch.setattr(cli.subprocess, "run", runner)

    cli._install_native(target, "https://host/prefix/mcp/")

    marketplace = tmp_path / "data" / "ach-memory" / f"{target}-marketplace"
    mcp = marketplace / "plugins" / "ach-memory" / ".mcp.json"
    first = {path.relative_to(marketplace): path.read_bytes() for path in marketplace.rglob("*") if path.is_file()}
    contents = b"".join(first.values())

    assert events[0] == f"which:{target}"
    assert events.index("copytree") > 0
    assert json.loads(mcp.read_text()) == expected_mcp
    assert install_command in runner.commands
    assert sum("marketplace" in command and "add" in command for command in runner.commands) == 1
    assert b"user-secret" not in contents
    assert all("user-secret" not in " ".join(command) for command in runner.commands)

    cli._install_native(target, "https://host/prefix/mcp/")

    second = {path.relative_to(marketplace): path.read_bytes() for path in marketplace.rglob("*") if path.is_file()}
    assert second == first
    assert sum("marketplace" in command and "add" in command for command in runner.commands) == 1


@pytest.mark.parametrize(
    ("target", "expected_command"),
    [
        ("codex", ["codex", "plugin", "remove", "ach-memory@ach-memory"]),
        ("claude", ["claude", "plugin", "update", "ach-memory@ach-memory"]),
    ],
)
def test_native_install_refreshes_only_an_existing_ach_memory_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    expected_command: list[str],
) -> None:
    """Breaks if an existing plugin is reinstalled or a different plugin is refreshed."""
    runner = _NativeRunner(target, marketplace_present=False, installed=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    cli._install_native(target, "https://host/prefix/mcp/")

    assert expected_command in runner.commands
    assert all(
        "ach-memory@ach-memory" in command or "marketplace" in command or command[-2:] == ["list", "--json"]
        for command in runner.commands
    )
