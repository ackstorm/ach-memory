import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

MCP_URL = "https://host/prefix/mcp/"

from memory import cli


def _files_under(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


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
        "https://user:pass@memory.example.com",
        "https://user@memory.example.com",
        "https://:pass@memory.example.com",
        "https://:443",
        "https:///memory",
    ],
)
def test_mcp_url_rejects_invalid_input(base: str) -> None:
    with pytest.raises(ValueError):
        cli._mcp_url(base)


def test_run_removes_api_key_from_child_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Breaks if a plugin-manager subprocess inherits the MCP API key."""
    captured: dict[str, object] = {}
    secret = "test-user-secret"

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("ACH_MEMORY_API_KEY", secret)
    monkeypatch.setenv("ACH_MEMORY_ORDINARY_TEST_VALUE", "kept")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    cli._run(["codex", "plugin", "list"])

    child_env = captured.get("env")
    assert isinstance(child_env, dict)
    assert "ACH_MEMORY_API_KEY" not in child_env
    assert secret not in child_env.values()
    assert child_env["ACH_MEMORY_ORDINARY_TEST_VALUE"] == "kept"


def test_main_rejects_missing_or_unknown_target() -> None:
    assert cli.main(["init"]) != 0
    assert cli.main(["init", "unknown"]) != 0


@pytest.mark.parametrize("target", ["codex", "claude", "opencode", "pi", "all"])
def test_main_accepts_targets(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    async def preflight(_url: str, _api_key: str) -> None:
        return None

    monkeypatch.setenv("ACH_MEMORY_URL", "https://memory.example.com")
    monkeypatch.setattr(cli, "_preflight", preflight, raising=False)
    installed: list[str] = []
    monkeypatch.setattr(cli, "_require_executable", lambda _target: None, raising=False)
    monkeypatch.setattr(cli, "_is_installed", lambda _target: True)
    monkeypatch.setattr(cli, "_native_plan", lambda _target: ({}, set()))
    monkeypatch.setattr(cli, "_config_plan", lambda _target, _url, _mode: (Path("config"), {}, ()))
    monkeypatch.setattr(
        cli, "_install_native", lambda name, _url, _plan, _mode: installed.append(name) or "plugin installed"
    )
    monkeypatch.setattr(
        cli, "_install_opencode", lambda _url, _plan, _mode: installed.append("opencode") or (Path("/tmp/oc/opencode.json"),)
    )
    monkeypatch.setattr(cli, "_install_pi", lambda _url, _plan, _mode: installed.append("pi") or (Path("/tmp/pi/mcp.json"),))

    assert cli.main(["init", target]) == 0
    assert installed == ([target] if target != "all" else ["codex", "claude", "opencode", "pi"])


def test_main_all_checks_every_executable_and_preflight_before_installers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Breaks if any requested installer can run before all targets are ready."""
    events: list[str] = []

    async def preflight(_url: str, _api_key: str) -> None:
        events.append("preflight")

    monkeypatch.setenv("ACH_MEMORY_API_KEY", "test-user-secret")
    monkeypatch.setenv("ACH_MEMORY_URL", "https://memory.example.com")
    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli, "_require_executable", lambda target: events.append(target))
    monkeypatch.setattr(cli, "_is_installed", lambda _target: True)
    monkeypatch.setattr(cli, "_native_plan", lambda _target: ({}, set()))
    monkeypatch.setattr(cli, "_config_plan", lambda _target, _url, _mode: (Path("config"), {}, ()))
    monkeypatch.setattr(
        cli, "_install_native", lambda target, _url, _plan, _mode: events.append(f"install:{target}") or "plugin installed"
    )
    monkeypatch.setattr(
        cli, "_install_opencode", lambda _url, _plan, _mode: events.append("install:opencode") or (Path("/tmp/oc/opencode.json"),)
    )
    monkeypatch.setattr(cli, "_install_pi", lambda _url, _plan, _mode: events.append("install:pi") or (Path("/tmp/pi/mcp.json"),))

    assert cli.main(["init", "all"]) == 0

    assert events == [
        "codex",
        "claude",
        "opencode",
        "pi",
        "preflight",
        "install:codex",
        "install:claude",
        "install:opencode",
        "install:pi",
    ]
    captured = capsys.readouterr()
    assert "test-user-secret" not in captured.out
    assert "test-user-secret" not in captured.err


def test_main_all_leaves_every_target_unchanged_when_later_config_is_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Breaks if an earlier target writes before a later target config is validated."""
    secret = "test-user-secret"
    config_home = tmp_path / "config"
    pi_home = tmp_path / "pi"
    data_home = tmp_path / "data"
    pi_home.mkdir(parents=True)
    (pi_home / "mcp.json").write_text("{invalid json")
    (config_home / "opencode").mkdir(parents=True)
    (config_home / "opencode" / "opencode.json").write_text('{"mcp": {}}')
    before = _files_under(tmp_path)

    async def preflight(_url: str, _api_key: str) -> None:
        return None

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload: object = {"marketplaces": []} if "marketplace" in command else {"installed": []}
        if command[0] == "claude":
            payload = []
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setenv("ACH_MEMORY_API_KEY", secret)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    assert cli.main(["init", "all"]) == 1

    captured = capsys.readouterr()
    assert _files_under(tmp_path) == before
    assert secret not in captured.out
    assert secret not in captured.err


def test_main_all_rejects_an_incomplete_adapter_bundle_before_mcp_or_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Breaks if a missing opencode/pi asset reaches MCP or creates target state.

    Only these two hosts still read a packaged bundle. claude and codex install
    from the repository marketplace, so there is no longer a native bundle that
    can be malformed -- the old unreadable-file case went with it.
    """
    secret = "test-user-secret"
    homes = tmp_path / "homes"
    config_home = homes / "config"
    pi_home = homes / "pi"
    bundle = tmp_path / "bundle"
    # pi.js is deliberately absent: the pi plan must fail on it.
    for relative in ("opencode.js", "activation.txt", "skills/ach-memory/SKILL.md"):
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("asset")
    (config_home / "opencode").mkdir(parents=True)
    (config_home / "opencode" / "opencode.json").write_text('{"mcp": {}}')
    pi_home.mkdir(parents=True)
    (pi_home / "mcp.json").write_text('{"mcpServers": {}}')
    before = _files_under(homes)
    events: list[str] = []

    async def preflight(_url: str, _api_key: str) -> None:
        events.append("mcp")

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload: object = {"marketplaces": []} if "marketplace" in command else {"installed": []}
        if command[0] == "claude":
            payload = []
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setenv("ACH_MEMORY_API_KEY", secret)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(homes / "data"))
    monkeypatch.setattr(cli, "_bundle_root", lambda _host: bundle)
    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)
    assert cli.main(["init", "all"]) == 1

    captured = capsys.readouterr()
    assert events == []
    assert _files_under(homes) == before
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.parametrize("failure", ["missing-executable", "mcp"])
def test_main_all_failure_before_preflight_or_mcp_leaves_every_target_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    """Breaks if an all-target prerequisite failure reaches an installer."""
    secret = "test-user-secret"
    config_home = tmp_path / "config"
    pi_home = tmp_path / "pi"
    (config_home / "opencode").mkdir(parents=True)
    (config_home / "opencode" / "opencode.json").write_text('{"mcp": {}}')
    pi_home.mkdir()
    (pi_home / "mcp.json").write_text('{"mcpServers": {}}')
    before = _files_under(tmp_path)

    async def preflight(_url: str, _api_key: str) -> None:
        if failure == "mcp":
            raise cli.CLIError("MCP preflight failed")

    def require(target: str) -> None:
        if failure == "missing-executable" and target == "pi":
            raise cli.CLIError("pi executable was not found")

    monkeypatch.setenv("ACH_MEMORY_API_KEY", secret)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_home))
    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli, "_require_executable", require)
    monkeypatch.setattr(cli, "_is_installed", lambda _target: True)
    monkeypatch.setattr(cli, "_native_plan", lambda _target: ({}, set()))

    assert cli.main(["init", "all"]) == 1

    captured = capsys.readouterr()
    assert _files_under(tmp_path) == before
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.parametrize("target", ["opencode", "pi"])
def test_main_single_config_target_fails_before_first_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    """Breaks if one malformed host config is replaced before its validation error."""
    secret = "test-user-secret"
    root = tmp_path / target
    config = root / ("opencode.json" if target == "opencode" else "mcp.json")
    asset = root / ("plugins/ach-memory.js" if target == "opencode" else "extensions/ach-memory.js")
    root.mkdir(parents=True)
    config.write_text("{invalid json")
    asset.parent.mkdir(parents=True)
    asset.write_text("keep this")
    before = _files_under(tmp_path)

    async def preflight(_url: str, _api_key: str) -> None:
        return None

    monkeypatch.setenv("ACH_MEMORY_API_KEY", secret)
    if target == "opencode":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    else:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(root))
    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")

    assert cli.main(["init", target]) == 1

    captured = capsys.readouterr()
    assert _files_under(tmp_path) == before
    assert secret not in captured.out
    assert secret not in captured.err


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
                "type": "local",
                "command": [
                    "uvx", "--from", f"{cli.GIT_SOURCE}@v{cli._version()}",
                    "ach-memory", "mcp", "--url", "https://host/next/mcp/",
                ],
                "environment": {"ACH_MEMORY_API_KEY": "{env:ACH_MEMORY_API_KEY}"},
                "enabled": True,
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
                "command": "uvx",
                "args": [
                    "--from", f"{cli.GIT_SOURCE}@v{cli._version()}",
                    "ach-memory", "mcp", "--url", "https://host/next/mcp/",
                ],
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
    assert commands == []


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
            # `add` now receives a repository slug, not a path. Both hosts clone
            # it and report the clone's absolute location on the next `list`,
            # which is what this stands in for -- echoing the slug back would
            # make the fake claim a relative path a real host never returns.
            self.marketplace_location = f"/home/tester/.{self.target}/marketplaces/ach-memory"
        return subprocess.CompletedProcess(command, 0, "", "")




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
        cli._install_native("codex", MCP_URL)

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
        cli._install_native("codex", MCP_URL)

    assert commands == [
        ["codex", "plugin", "marketplace", "list", "--json"],
        ["codex", "plugin", "list", "--json"],
    ]
    assert not (tmp_path / "data" / "ach-memory" / "codex-marketplace").exists()


def test_installed_plugins_accepts_codex_available_catalog() -> None:
    """Breaks if a real Codex plugin-list catalog blocks installation."""
    assert cli._installed_plugins(
        "codex",
        {
            "installed": [{"pluginId": "ach-memory@ach-memory"}],
            "available": [],
        },
    ) == {"ach-memory@ach-memory"}


@pytest.mark.parametrize(
    ("target", "install_command"),
    [
        ("codex", ["codex", "plugin", "add", "ach-memory@ach-memory", "--json"]),
        ("claude", ["claude", "plugin", "install", "-y", "--scope", "user", "ach-memory@ach-memory"]),
    ],
)
def test_native_install_registers_the_repository_and_writes_nothing_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, install_command: list[str]
) -> None:
    """The architectural guard, from the installer's side.

    This used to assert that a marketplace was rendered under XDG_DATA_HOME
    with the endpoint baked into its .mcp.json. It now asserts the opposite:
    the installer hands the host a repository slug and creates no files at all.
    A native install that touches the filesystem has reintroduced rendering.
    """
    runner = _NativeRunner(target, marketplace_present=False, installed=False)
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    assert cli._install_native(target, MCP_URL) == f"plugin installed from {cli.MARKETPLACE}"

    add = next(c for c in runner.commands if "marketplace" in c and "add" in c)
    assert "ackstorm/ach-memory" in add
    assert not any(part.startswith(str(tmp_path)) for part in add)
    assert install_command in runner.commands
    assert _files_under(home) == {}
    # The credential is the host's problem at run time, never ours at install
    # time: it must not reach a command line or a file.
    assert all("user-secret" not in " ".join(command) for command in runner.commands)

    cli._install_native(target, MCP_URL)

    assert sum("marketplace" in command and "add" in command for command in runner.commands) == 1
    assert _files_under(home) == {}


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

    cli._install_native(target, MCP_URL)

    assert expected_command in runner.commands
    assert all(
        "ach-memory@ach-memory" in command
        or "marketplace" in command
        or command[:2] == ["codex", "mcp"]
        or command[-2:] == ["list", "--json"]
        for command in runner.commands
    )


def _stub_installers(monkeypatch: pytest.MonkeyPatch, installed: list[str]) -> None:
    async def preflight(_url: str, _api_key: str) -> None:
        return None

    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli, "_native_plan", lambda _target: ({}, set()))
    monkeypatch.setattr(cli, "_config_plan", lambda _target, _url, _mode: (Path("config"), {}, ()))
    monkeypatch.setattr(
        cli, "_install_native", lambda name, _url, _plan, _mode: installed.append(name) or "plugin installed"
    )
    monkeypatch.setattr(
        cli, "_install_opencode", lambda _url, _plan, _mode: installed.append("opencode") or (Path("/tmp/oc/opencode.json"),)
    )
    monkeypatch.setattr(cli, "_install_pi", lambda _url, _plan, _mode: installed.append("pi") or (Path("/tmp/pi/mcp.json"),))


def test_main_all_installs_what_is_present_and_names_what_it_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`all` must not be all-or-nothing: one absent agent used to abort the run."""
    present = {"claude", "pi"}
    installed: list[str] = []
    monkeypatch.setattr(cli, "_is_installed", lambda target: target in present)
    _stub_installers(monkeypatch, installed)

    assert cli.main(["init", "all"]) == 0
    assert installed == ["claude", "pi"]

    # Named, not silently dropped -- otherwise `all` reports success while
    # having done less than its name says. They belong in the summary on
    # stdout, beside what did install, rather than shouted from stderr.
    captured = capsys.readouterr()
    skips = [line for line in captured.out.splitlines() if "skipped, not on PATH" in line]
    assert sorted(line.split()[1] for line in skips) == ["codex", "opencode"]
    # Progress narration is the only thing allowed on stderr on success --
    # anything else there is an error message that belongs beside a nonzero
    # exit, and the skips above must stay on stdout, not be shouted here.
    assert all(line.startswith("  …") for line in captured.err.splitlines())


def test_main_all_fails_when_no_supported_agent_is_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Installing into nothing is a failure, not a quiet success."""
    installed: list[str] = []
    monkeypatch.setattr(cli, "_is_installed", lambda _target: False)
    _stub_installers(monkeypatch, installed)

    assert cli.main(["init", "all"]) == 1
    assert installed == []
    assert "PATH" in capsys.readouterr().err


@pytest.mark.parametrize("target", ["codex", "claude", "opencode", "pi"])
def test_main_named_target_still_fails_when_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    """Only `all` takes what is there. A named target that is missing must say so
    rather than exit 0 having installed nothing."""
    installed: list[str] = []
    monkeypatch.setattr(cli, "_is_installed", lambda _target: False)
    _stub_installers(monkeypatch, installed)

    assert cli.main(["init", target]) == 1
    assert installed == []
    assert f"{target} executable was not found" in capsys.readouterr().err


def _init_stubs(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> None:
    async def preflight(_url: str, _api_key: str) -> None:
        return None

    monkeypatch.setattr(cli, "_preflight", preflight)
    monkeypatch.setattr(cli, "_is_installed", lambda target: target in present)
    monkeypatch.setattr(cli, "_require_executable", lambda _target: None)
    monkeypatch.setattr(cli, "_native_plan", lambda _target: ({}, set()))
    monkeypatch.setattr(cli, "_install_native", lambda target, _url, _plan, _mode: f"plugin installed from {cli.MARKETPLACE}")
    monkeypatch.setattr(cli, "_config_plan", lambda _target, _url, _mode: (Path("config"), {}, ()))
    monkeypatch.setattr(
        cli, "_install_opencode",
        lambda _url, _plan, _mode: (Path.home() / ".config/opencode/opencode.json",
                             Path.home() / ".config/opencode/plugins/ach-memory.js"),
    )
    monkeypatch.setattr(cli, "_install_pi", lambda _url, _plan, _mode: (Path.home() / ".pi/agent/mcp.json",))


def test_init_reports_every_agent_including_the_ones_that_write_no_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression this output exists for.

    claude and codex install through their host's marketplace and write nothing
    of ours, so listing changed paths -- which is all init used to print --
    reported two agents out of four and made a successful install
    indistinguishable from a missing one.
    """
    monkeypatch.setenv("ACH_MEMORY_URL", "https://memory.example.com")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    _init_stubs(monkeypatch, {"claude", "codex", "opencode", "pi"})

    assert cli.main(["init", "all"]) == 0

    out = capsys.readouterr().out
    for agent in ("claude", "codex", "opencode", "pi"):
        assert any(agent in line and "\u2714" in line for line in out.splitlines()), agent
    assert "https://memory.example.com" in out
    # Files are summarized, not listed, until -v asks for them.
    assert "2 files" in out
    assert "ach-memory.js" not in out
    assert "user-secret" not in out


def test_init_allows_codex_without_an_explicit_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The proxy reads ACH_MEMORY_URL per spawn, not at install time, so codex
    no longer needs the endpoint pinned into its config at init -- the
    localhost fallback is just a warning for codex now, same as every other
    host."""
    monkeypatch.delenv("ACH_MEMORY_URL", raising=False)
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    _init_stubs(monkeypatch, {"codex"})

    assert cli.main(["init", "codex"]) == 0

    out = capsys.readouterr().out
    assert "ACH_MEMORY_URL is not set" in out


def test_init_still_allows_the_localhost_fallback_without_codex(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal is scoped to the host that cannot recover from it."""
    monkeypatch.delenv("ACH_MEMORY_URL", raising=False)
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    _init_stubs(monkeypatch, {"claude"})

    assert cli.main(["init", "claude"]) == 0
    assert "ACH_MEMORY_URL is not set" in capsys.readouterr().out


def test_init_warns_when_the_endpoint_is_the_localhost_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unexported ACH_MEMORY_URL installs cleanly against localhost and then
    fails at every tool call, far from here."""
    monkeypatch.delenv("ACH_MEMORY_URL", raising=False)
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    _init_stubs(monkeypatch, {"claude"})

    assert cli.main(["init", "claude"]) == 0

    out = capsys.readouterr().out
    assert "ACH_MEMORY_URL is not set" in out
    assert "http://localhost:8000" in out


def test_verbose_lists_the_files_a_host_config_install_wrote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ACH_MEMORY_URL", "https://memory.example.com")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    _init_stubs(monkeypatch, {"opencode"})

    assert cli.main(["init", "opencode", "-v"]) == 0

    out = capsys.readouterr().out
    # The root is named once on the summary line; the listing shows only what
    # differs, so a long relocated root cannot push filenames off the terminal.
    assert "~/.config/opencode" in out
    assert "plugins/ach-memory.js" in out
    assert "~/.config/opencode/plugins/ach-memory.js" not in out
    assert str(Path.home()) not in out


def test_a_failure_without_ach_memory_url_names_the_missing_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure lands before the summary that would have shown the endpoint.

    Without this, an unexported ACH_MEMORY_URL produces "MCP preflight failed"
    against a localhost nobody asked for, which reads as the service being down
    rather than the variable being absent.
    """
    async def preflight(_url: str, _api_key: str) -> None:
        raise cli.CLIError("MCP preflight failed")

    monkeypatch.delenv("ACH_MEMORY_URL", raising=False)
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    _init_stubs(monkeypatch, {"claude"})
    monkeypatch.setattr(cli, "_preflight", preflight)

    assert cli.main(["init", "claude"]) == 1

    err = capsys.readouterr().err
    assert "MCP preflight failed" in err
    assert "ACH_MEMORY_URL is not set" in err
    assert "http://localhost:8000" in err
    assert "user-secret" not in err


def test_a_failure_with_ach_memory_url_set_does_not_blame_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real outage must not be reported as a configuration mistake."""
    async def preflight(_url: str, _api_key: str) -> None:
        raise cli.CLIError("MCP preflight failed")

    monkeypatch.setenv("ACH_MEMORY_URL", "https://memory.example.com")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "user-secret")
    _init_stubs(monkeypatch, {"claude"})
    monkeypatch.setattr(cli, "_preflight", preflight)

    assert cli.main(["init", "claude"]) == 1

    err = capsys.readouterr().err
    assert "MCP preflight failed" in err
    assert "ACH_MEMORY_URL" not in err


def test_config_roots_follow_each_host_own_relocation_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pins which variable relocates each host, and which ones must not.

    opencode's config root is `(XDG_CONFIG_HOME || ~/.config)/opencode` and
    nothing else feeds into it. OPENCODE_CONFIG, OPENCODE_CONFIG_DIR and
    OPENCODE_CONFIG_CONTENT only append to its search path, so an install that
    followed them would write somewhere opencode does not treat as its config
    root -- and somewhere the user is not editing by hand. Setting all three to
    decoys here is the guard against that being "fixed" later.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pihome"))
    for decoy in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"):
        monkeypatch.setenv(decoy, str(tmp_path / "decoy"))

    assert cli._config_root("opencode") == tmp_path / "xdg" / "opencode"
    assert cli._config_root("pi") == tmp_path / "pihome"


def test_config_roots_fall_back_to_each_host_documented_default(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("XDG_CONFIG_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)

    assert cli._config_root("opencode") == Path.home() / ".config" / "opencode"
    assert cli._config_root("pi") == Path.home() / ".pi" / "agent"


def test_codex_install_registers_the_server_from_the_current_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex ends up matching the environment init ran with, whatever it held.

    Remove-then-add unconditionally, with no read of the current config: that
    makes re-running init the update path, and `codex mcp remove` exits 0 when
    the server is absent so the remove needs no guard.
    """
    runner = _NativeRunner(
        "codex",
        marketplace_present=True,
        installed=True,
        marketplace_location="/home/tester/.codex/marketplaces/ach-memory",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(cli.subprocess, "run", runner)

    cli._install_native("codex", MCP_URL)

    mcp = [c for c in runner.commands if c[:2] == ["codex", "mcp"]]
    assert [c[2] for c in mcp] == ["remove", "add"]
    add = mcp[1]
    assert add == [
        "codex", "mcp", "add", "ach-memory", "--",
        "uvx", "--from", f"{cli.GIT_SOURCE}@v{cli._version()}",
        "ach-memory", "mcp", "--url", MCP_URL,
    ]
    # The endpoint travels as the proxy's own --url argument, never as codex's
    # remote-server --url plus a bearer env var name: that is the --http shape.
    assert "--bearer-token-env-var" not in add
    assert not any("mcp" in c and "list" in c for c in runner.commands)


def test_opencode_install_registers_the_skills_directory(tmp_path, monkeypatch):
    """opencode reads skills only from the paths it is configured with.

    The default is the project-local `.opencode/skills`; we install under the
    user config dir, which is not in it. Measured before this: opencode was the
    only host of four that never read the skill, and the only one that stored a
    fact in Spanish when asked in Spanish -- the other three read the skill and
    stored English.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from memory.cli import _config_plan

    _config_path, config, _assets = _config_plan("opencode", "https://memory.example.com/mcp/")
    paths = config["skills"]["paths"]

    assert str(tmp_path / "opencode" / "skills") in paths
    # Naming `paths` at all replaces opencode's default instead of adding to
    # it, so the project-local directory has to survive the write.
    assert ".opencode/skills" in paths


def test_opencode_install_keeps_skill_paths_the_user_already_set(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import json

    from memory.cli import _config_plan

    config_dir = tmp_path / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.json").write_text(
        json.dumps({"skills": {"paths": ["/srv/team-skills"], "urls": ["https://x/"]}})
    )

    _config_path, config, _assets = _config_plan("opencode", "https://memory.example.com/mcp/")

    assert "/srv/team-skills" in config["skills"]["paths"]
    assert str(config_dir / "skills") in config["skills"]["paths"]
    assert config["skills"]["urls"] == ["https://x/"]


def test_mcp_requires_api_key(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("ACH_MEMORY_API_KEY", raising=False)
    assert cli.main(["mcp"]) == 1
    assert "ACH_MEMORY_API_KEY" in capsys.readouterr().err


def test_mcp_builds_proxy_from_env_and_runs_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACH_MEMORY_URL", "https://mem.example.com")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "mem_secret")
    calls: list[tuple[str, str]] = []
    built = []

    class FakeProxy:
        instructions = None

        def run(self) -> None:
            calls.append(("run", "stdio"))

    def fake_build(url: str, key: str) -> FakeProxy:
        calls.append((url, key))
        proxy = FakeProxy()
        built.append(proxy)
        return proxy

    monkeypatch.setattr("memory.mcp.proxy.build_proxy", fake_build)
    monkeypatch.setattr(
        "memory.mcp.proxy.fetch_brief",
        lambda base, key, slug, locator: {"instructions": "POLICY + BRIEF"},
    )
    assert cli.main(["mcp"]) == 0
    # Same /mcp/ derivation init uses -- one _mcp_url, not a second parser.
    assert calls == [("https://mem.example.com/mcp/", "mem_secret"), ("run", "stdio")]
    assert built[0].instructions == "POLICY + BRIEF"


def test_mcp_still_runs_when_there_is_no_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memory service that cannot answer costs the session its brief and
    nothing else -- the proxy then advertises no instructions of its own and
    FastMCP forwards the server's policy text."""
    monkeypatch.setenv("ACH_MEMORY_URL", "https://mem.example.com")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "mem_secret")
    ran = []

    class FakeProxy:
        instructions = None

        def run(self) -> None:
            ran.append(True)

    monkeypatch.setattr("memory.mcp.proxy.build_proxy", lambda _u, _k: FakeProxy())
    monkeypatch.setattr("memory.mcp.proxy.fetch_brief", lambda *_a, **_k: None)

    assert cli.main(["mcp"]) == 0
    assert ran == [True]
    assert FakeProxy.instructions is None


def test_config_plan_modes_pick_the_server_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--http restores the direct-remote entries; --local pins this
    checkout's script. Only the server entry varies -- assets and paths are
    covered by the stdio test above."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    url = "https://memory.example.com/mcp/"

    _, config, _ = cli._config_plan("pi", url, "http")
    assert config["mcpServers"]["ach-memory"] == {
        "url": url,
        "auth": "bearer",
        "bearerTokenEnv": "ACH_MEMORY_API_KEY",
        "lifecycle": "lazy",
        "directTools": False,
    }
    _, config, _ = cli._config_plan("opencode", url, "http")
    assert config["mcp"]["ach-memory"] == {
        "type": "remote",
        "url": url,
        "headers": {"Authorization": "Bearer {env:ACH_MEMORY_API_KEY}"},
    }

    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/checkout/.venv/bin/ach-memory")
    _, config, _ = cli._config_plan("pi", url, "local")
    assert config["mcpServers"]["ach-memory"] == {
        "command": "/checkout/.venv/bin/ach-memory",
        "args": ["mcp", "--url", url],
    }


def test_register_codex_server_http_pins_url_and_env_var_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_run", lambda command: commands.append(command))
    cli._register_codex_server("https://memory.example.com/mcp/", "http")
    assert commands == [
        ["codex", "mcp", "remove", "ach-memory"],
        [
            "codex", "mcp", "add", "ach-memory",
            "--url", "https://memory.example.com/mcp/",
            "--bearer-token-env-var", "ACH_MEMORY_API_KEY",
        ],
    ]
