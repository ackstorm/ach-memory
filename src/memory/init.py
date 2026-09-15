"""`ach-memory init <host>`: install the plugin into a coding-agent host."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from memory import __version__

MARKETPLACE = "ackstorm/ach-memory"
PLUGIN = "ach-memory@ach-memory"
DEFAULT_URL = "http://localhost:8000/mcp/"
_SOURCE = f"git+https://github.com/ackstorm/ach-memory@v{__version__}"


def _plugins_root() -> Path:
    """Wheel ships `plugins/` as `memory/plugins/`; a checkout keeps it at the repo root."""
    packaged = Path(__file__).parent / "plugins"
    return packaged if packaged.exists() else Path(__file__).parents[2] / "plugins"


def _run(*argv: str) -> bool:
    return subprocess.run(list(argv), check=False).returncode == 0


def _init_claude(local: bool) -> None:
    """Native marketplace install; a re-run updates. `local` has no meaning here."""
    if not _run("claude", "plugin", "marketplace", "add", MARKETPLACE):
        _run("claude", "plugin", "marketplace", "update", "ach-memory")
    if not _run("claude", "plugin", "install", PLUGIN):
        _run("claude", "plugin", "update", PLUGIN)


def _init_codex(local: bool) -> None:
    """Codex has `marketplace upgrade` and no `plugin update`: re-add is the update path."""
    if not _run("codex", "plugin", "marketplace", "add", MARKETPLACE):
        _run("codex", "plugin", "marketplace", "upgrade", "ach-memory")
    if not _run("codex", "plugin", "add", PLUGIN) and _run("codex", "plugin", "remove", PLUGIN):
        _run("codex", "plugin", "add", PLUGIN)


def _mcp_command(local: bool) -> list[str]:
    """URL resolved now so the written config is self-describing; the key stays in env."""
    url = os.environ.get("ACH_MEMORY_URL") or DEFAULT_URL
    entry = [str(Path(sys.argv[0]).resolve())] if local else ["uvx", "--from", _SOURCE, "ach-memory"]
    return [*entry, "mcp", "--url", url]


def _edit_json(path: Path, mutate) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    mutate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _append_once(items: list, value) -> None:
    if value not in items:
        items.append(value)


def _copy_plugin(host: str, dest: Path, skills: Path) -> None:
    """`<dest>/ach-memory.js` + `<dest>/ach-memory/{activation*,scripts/}` + `<skills>/ach-memory/`."""
    src = _plugins_root() / host
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(src / "ach-memory.js", dest / "ach-memory.js")
    shutil.copytree(
        src, dest / "ach-memory", dirs_exist_ok=True, ignore=shutil.ignore_patterns("ach-memory.js", "skills")
    )
    shutil.copytree(src / "skills", skills, dirs_exist_ok=True)


def _init_opencode(local: bool) -> None:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode"
    _copy_plugin("opencode", root / "plugins", root / "skills")

    def mutate(cfg: dict) -> None:
        cfg.setdefault("mcp", {})["ach-memory"] = {
            "type": "local",
            "command": _mcp_command(local),
            "environment": {
                "ACH_MEMORY_API_KEY": "{env:ACH_MEMORY_API_KEY}",
                "ACH_MEMORY_HEADER": "{env:ACH_MEMORY_HEADER}",
            },
            "enabled": True,
        }
        _append_once(cfg.setdefault("plugin", []), "./plugins/ach-memory.js")
        # Setting `skills.paths` replaces opencode's default, so restate it.
        _append_once(cfg.setdefault("skills", {}).setdefault("paths", [".opencode/skills"]), str(root / "skills"))

    _edit_json(root / "opencode.json", mutate)


def _init_pi(local: bool) -> None:
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
    _copy_plugin("pi", root / "extensions", root / "skills")
    command = _mcp_command(local)

    def mutate(cfg: dict) -> None:
        cfg.setdefault("mcpServers", {})["ach-memory"] = {"command": command[0], "args": command[1:]}

    _edit_json(root / "mcp.json", mutate)
    settings = root / "settings.json"
    packages = json.loads(settings.read_text()).get("packages", []) if settings.exists() else []
    if "npm:pi-mcp-adapter" not in packages:
        _run("pi", "install", "npm:pi-mcp-adapter")


HOSTS = ("claude", "codex", "opencode", "pi")
_INSTALLERS = {"claude": _init_claude, "codex": _init_codex, "opencode": _init_opencode, "pi": _init_pi}


def init(target: str, local: bool) -> int:
    """Install for one host, or for every host whose CLI is on PATH when `target` is `all`."""
    hosts = [h for h in HOSTS if shutil.which(h)] if target == "all" else [target]
    if not hosts:
        print("ach-memory init: no supported host CLI on PATH", file=sys.stderr)
        return 1
    for host in hosts:
        _INSTALLERS[host](local)
        print(f"ach-memory: installed for {host}")
    return 0
