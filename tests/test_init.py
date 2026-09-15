import json
import subprocess

from memory import init


def test_plugins_root_points_at_a_tree_with_every_host():
    root = init._plugins_root()
    assert (root / "shared" / "activation.txt").is_file()
    assert (root / "claude-code" / ".claude-plugin" / "plugin.json").is_file()


class _Runner:
    """Records argv of every subprocess.run call; fails the argv listed in `failing`."""

    def __init__(self, failing=()):
        self.calls = []
        self.failing = [list(f) for f in failing]

    def __call__(self, argv, check=False):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1 if list(argv) in self.failing else 0)


def test_init_claude_always_refreshes_marketplace_and_plugin(monkeypatch):
    runner = _Runner(failing=[["claude", "plugin", "marketplace", "add", "ackstorm/ach-memory"]])
    monkeypatch.setattr(init.subprocess, "run", runner)

    init._init_claude(local=False)

    assert runner.calls == [
        ["claude", "plugin", "marketplace", "add", "ackstorm/ach-memory"],
        ["claude", "plugin", "marketplace", "update", "ach-memory"],
        ["claude", "plugin", "install", "ach-memory@ach-memory"],
        ["claude", "plugin", "update", "ach-memory@ach-memory"],
    ]


def test_init_codex_upgrades_marketplace_then_readds(monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(init.subprocess, "run", runner)

    init._init_codex(local=False)

    assert runner.calls == [
        ["codex", "plugin", "marketplace", "add", "ackstorm/ach-memory"],
        ["codex", "plugin", "marketplace", "upgrade", "ach-memory"],
        ["codex", "plugin", "add", "ach-memory@ach-memory"],
    ]


def _opencode_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("ACH_MEMORY_URL", "https://memory.example/mcp/")
    return tmp_path / "opencode"


def test_init_opencode_writes_plugin_skill_and_config(tmp_path, monkeypatch):
    home = _opencode_home(tmp_path, monkeypatch)
    home.mkdir()
    (home / "opencode.json").write_text(json.dumps({"provider": {"x": 1}, "plugin": ["./plugins/other.js"]}))

    init._init_opencode(local=False)

    assert (home / "plugins" / "ach-memory.js").is_file()
    assert (home / "plugins" / "ach-memory" / "scripts" / "session-start.sh").is_file()
    assert (home / "plugins" / "ach-memory" / "activation.txt").is_file()
    assert (home / "skills" / "ach-memory" / "SKILL.md").is_file()
    cfg = json.loads((home / "opencode.json").read_text())
    assert cfg["provider"] == {"x": 1}
    assert cfg["plugin"] == ["./plugins/other.js", "./plugins/ach-memory.js"]
    assert cfg["skills"]["paths"] == [".opencode/skills", str(home / "skills")]
    server = cfg["mcp"]["ach-memory"]
    assert server["type"] == "local"
    assert server["enabled"] is True
    assert server["command"][:3] == ["uvx", "--from", f"git+https://github.com/ackstorm/ach-memory@v{init.__version__}"]
    assert server["command"][3:] == ["ach-memory", "mcp", "--url", "https://memory.example/mcp/"]
    assert server["environment"] == {
        "ACH_MEMORY_API_KEY": "{env:ACH_MEMORY_API_KEY}",
        "ACH_MEMORY_HEADER": "{env:ACH_MEMORY_HEADER}",
    }
    assert "ACH_MEMORY_API_KEY" not in json.dumps(server["command"])


def test_init_opencode_is_idempotent_and_creates_the_config(tmp_path, monkeypatch):
    home = _opencode_home(tmp_path, monkeypatch)

    init._init_opencode(local=False)
    first = (home / "opencode.json").read_text()
    init._init_opencode(local=False)

    assert (home / "opencode.json").read_text() == first
    cfg = json.loads(first)
    assert cfg["plugin"] == ["./plugins/ach-memory.js"]
    assert cfg["skills"]["paths"].count(str(home / "skills")) == 1


def test_init_opencode_local_uses_this_checkouts_script(tmp_path, monkeypatch):
    home = _opencode_home(tmp_path, monkeypatch)
    monkeypatch.setattr(init.sys, "argv", ["/venv/bin/ach-memory"])

    init._init_opencode(local=True)

    cfg = json.loads((home / "opencode.json").read_text())
    assert cfg["mcp"]["ach-memory"]["command"] == ["/venv/bin/ach-memory", "mcp", "--url", "https://memory.example/mcp/"]


def _pi_home(tmp_path, monkeypatch):
    home = tmp_path / "pi-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
    monkeypatch.setenv("ACH_MEMORY_URL", "https://memory.example/mcp/")
    return home


def test_init_pi_writes_extension_skill_and_mcp(tmp_path, monkeypatch):
    home = _pi_home(tmp_path, monkeypatch)
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"packages": ["npm:pi-mcp-adapter"]}))
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    runner = _Runner()
    monkeypatch.setattr(init.subprocess, "run", runner)

    init._init_pi(local=False)

    assert (home / "extensions" / "ach-memory.js").is_file()
    assert (home / "extensions" / "ach-memory" / "scripts" / "session-start.sh").is_file()
    assert (home / "skills" / "ach-memory" / "SKILL.md").is_file()
    servers = json.loads((home / "mcp.json").read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert servers["ach-memory"] == {
        "command": "uvx",
        "args": ["--from", f"git+https://github.com/ackstorm/ach-memory@v{init.__version__}", "ach-memory", "mcp", "--url", "https://memory.example/mcp/"],
    }
    assert "env" not in servers["ach-memory"]
    assert runner.calls == []


def test_init_pi_installs_the_mcp_adapter_when_missing(tmp_path, monkeypatch):
    home = _pi_home(tmp_path, monkeypatch)
    runner = _Runner()
    monkeypatch.setattr(init.subprocess, "run", runner)

    init._init_pi(local=False)
    init._init_pi(local=False)

    assert runner.calls == [["pi", "install", "npm:pi-mcp-adapter"]] * 2
    assert "ach-memory" in json.loads((home / "mcp.json").read_text())["mcpServers"]


def test_init_all_runs_only_hosts_on_path(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(init, "_INSTALLERS", {h: (lambda local, h=h: ran.append((h, local))) for h in init.HOSTS})
    monkeypatch.setattr(init.shutil, "which", lambda name: "/bin/" + name if name in ("codex", "pi") else None)

    assert init.init("all", local=True) == 0

    assert ran == [("codex", True), ("pi", True)]
    assert capsys.readouterr().out == "ach-memory: installed for codex\nach-memory: installed for pi\n"


def test_init_all_with_no_host_fails(monkeypatch, capsys):
    monkeypatch.setattr(init.shutil, "which", lambda name: None)

    assert init.init("all", local=False) == 1
    assert "no supported host" in capsys.readouterr().err


def test_init_explicit_host_skips_detection(monkeypatch):
    ran = []
    monkeypatch.setattr(init, "_INSTALLERS", {**init._INSTALLERS, "opencode": lambda local: ran.append(local)})
    monkeypatch.setattr(init.shutil, "which", lambda name: None)

    assert init.init("opencode", local=False) == 0
    assert ran == [False]
