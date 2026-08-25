"""Install ach-memory integrations after validating the public MCP endpoint."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

SUPPORTED = ("codex", "claude", "opencode", "pi")
MARKETPLACE = "ackstorm/ach-memory"


class CLIError(Exception):
    """A concise, user-facing CLI failure."""


def _mcp_url(base: str) -> str:
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or not parts.hostname
        or parts.query
        or parts.fragment
    ):
        raise ValueError("ACH_MEMORY_URL must be an absolute http(s) URL without a query or fragment")

    return urlunsplit((parts.scheme, parts.netloc, f"{parts.path.rstrip('/')}/mcp/", "", ""))


async def _preflight(url: str, api_key: str) -> None:
    if not api_key:
        raise CLIError("ACH_MEMORY_API_KEY is required")

    try:
        client = create_mcp_http_client({"Authorization": f"Bearer {api_key}"})
        async with (
            client,
            streamable_http_client(url, http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
    except Exception as exc:
        raise CLIError("MCP preflight failed") from exc

    if {"recall", "retain"} - names:
        raise CLIError("MCP preflight failed: server is missing required public tools")


def _bundle_root(host: str) -> Path:
    """Return the installed assets for one host, or the source-checkout copy.

    Only opencode and pi read this. claude and codex install from the
    repository marketplace and never touch the packaged copy.
    """
    packaged = resources.files("memory").joinpath(f"integrations/{host}")
    if packaged.is_dir():
        return Path(packaged)
    return Path(__file__).resolve().parents[2] / "plugins" / host


def _is_installed(target: str) -> bool:
    return shutil.which(target) is not None


def _require_executable(target: str) -> None:
    if not _is_installed(target):
        raise CLIError(f"{target} executable was not found")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ACH_MEMORY_API_KEY", None)
    try:
        result = subprocess.run(command, capture_output=True, check=False, text=True, env=env)
    except OSError as exc:
        raise CLIError(f"{command[0]} plugin command failed") from exc
    if result.returncode:
        raise CLIError(f"{command[0]} plugin command failed")
    return result


def _run_json(command: list[str]) -> object:
    result = _run(command)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CLIError(f"{command[0]} returned unsupported plugin JSON") from exc


def _marketplace_locations(target: str, payload: object) -> dict[str, Path]:
    if target == "codex":
        if not isinstance(payload, dict) or set(payload) != {"marketplaces"}:
            raise CLIError("codex returned unsupported marketplace JSON")
        entries = payload["marketplaces"]
        location = "root"
    else:
        entries = payload
        location = "installLocation"
    if not isinstance(entries, list):
        raise CLIError(f"{target} returned unsupported marketplace JSON")
    marketplaces: dict[str, Path] = {}
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        path = entry.get(location) if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(path, str) or not Path(path).is_absolute():
            raise CLIError(f"{target} returned unsupported marketplace JSON")
        if name in marketplaces:
            raise CLIError(f"{target} returned unsupported marketplace JSON")
        marketplaces[name] = Path(path).resolve()
    return marketplaces


def _installed_plugins(target: str, payload: object) -> set[str]:
    if target == "codex":
        if not isinstance(payload, dict) or not isinstance(payload.get("installed"), list):
            raise CLIError("codex returned unsupported plugin JSON")
        entries = payload["installed"]
    else:
        entries = payload
    field = "pluginId" if target == "codex" else "id"
    if not isinstance(entries, list):
        raise CLIError(f"{target} returned unsupported plugin JSON")
    if any(not isinstance(entry, dict) or not isinstance(entry.get(field), str) for entry in entries):
        raise CLIError(f"{target} returned unsupported plugin JSON")
    return {entry[field] for entry in entries}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(value, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with source.open("rb") as input_file, tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as output_file:
            temporary = Path(output_file.name)
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CLIError(f"invalid JSON in {path}") from exc
    if not isinstance(value, dict):
        raise CLIError(f"invalid JSON in {path}")
    return value


def _config_root(target: str) -> Path:
    if target == "opencode":
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode"
    if target == "pi":
        return Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
    raise CLIError(f"config installation is not available for {target}")


def _config_plan(target: str, url: str) -> tuple[Path, dict[str, object], tuple[tuple[Path, Path], ...]]:
    root = _config_root(target)
    config_path = root / ("opencode.json" if target == "opencode" else "mcp.json")
    config = _read_json_object(config_path)
    server_key = "mcp" if target == "opencode" else "mcpServers"
    existing = config.get(server_key, {})
    if not isinstance(existing, dict):
        raise CLIError(f"invalid JSON in {config_path}")

    server: dict[str, object]
    if target == "opencode":
        server = {
            "type": "remote",
            "url": url,
            "headers": {"Authorization": "Bearer {env:ACH_MEMORY_API_KEY}"},
        }
        paths = (
            ("opencode.js", "plugins/ach-memory.js"),
            ("activation.txt", "plugins/ach-memory/activation.txt"),
            ("skills/ach-memory/SKILL.md", "skills/ach-memory/SKILL.md"),
        )
    else:
        server = {
            "url": url,
            "auth": "bearer",
            "bearerTokenEnv": "ACH_MEMORY_API_KEY",
            "lifecycle": "lazy",
            "directTools": False,
        }
        paths = (
            ("pi.js", "extensions/ach-memory.js"),
            ("activation.txt", "extensions/ach-memory/activation.txt"),
            ("skills/ach-memory/SKILL.md", "skills/ach-memory/SKILL.md"),
        )
    config[server_key] = {**existing, "ach-memory": server}

    bundle = _bundle_root(target)
    assets = tuple((bundle / source, root / destination) for source, destination in paths)
    for source, _destination in assets:
        if not source.is_file():
            raise CLIError(f"missing bundled {target} asset")
    return config_path, config, assets


def _install_opencode(
    url: str, plan: tuple[Path, dict[str, object], tuple[tuple[Path, Path], ...]] | None = None
) -> tuple[Path, ...]:
    config_path, config, assets = plan or _config_plan("opencode", url)
    for source, destination in assets:
        _copy_file_atomic(source, destination)
    _write_json_atomic(config_path, config)
    return (config_path, *(destination for _source, destination in assets))


def _install_pi(
    url: str, plan: tuple[Path, dict[str, object], tuple[tuple[Path, Path], ...]] | None = None
) -> tuple[Path, ...]:
    config_path, config, assets = plan or _config_plan("pi", url)
    for source, destination in assets:
        _copy_file_atomic(source, destination)
    _write_json_atomic(config_path, config)
    _run(["pi", "install", "npm:pi-mcp-adapter"])
    return (config_path, *(destination for _source, destination in assets))


def _native_plan(target: str) -> tuple[dict[str, Path], set[str]]:
    marketplaces = _marketplace_locations(
        target, _run_json([target, "plugin", "marketplace", "list", "--json"])
    )
    installed = _installed_plugins(target, _run_json([target, "plugin", "list", "--json"]))
    return marketplaces, installed


def _install_native(target: str, plan: tuple[dict[str, Path], set[str]] | None = None) -> tuple[Path, ...]:
    """Register the repository as a marketplace and let the host install from it.

    Nothing is generated here. The plugin is committed at plugins/<host>/ and
    its .mcp.json resolves both the endpoint and the credential from the
    environment, so one static tree serves every deployment -- which is what
    lets the repository itself be the marketplace, the way engram and codemem
    do it. The previous version rendered a private marketplace into
    ~/.local/share at install time purely to bake a per-install URL into that
    file; with ${ACH_MEMORY_URL} there is nothing left to bake.
    """
    if target not in {"codex", "claude"}:
        raise CLIError(f"native installation is not available for {target}")
    _require_executable(target)

    marketplaces, installed = plan if plan is not None else _native_plan(target)
    plugin = "ach-memory@ach-memory"

    if "ach-memory" not in marketplaces:
        command = [target, "plugin", "marketplace", "add"]
        if target == "claude":
            command.extend(["--scope", "user"])
        command.append(MARKETPLACE)
        if target == "codex":
            command.append("--json")
        _run(command)

    if target == "codex":
        if plugin in installed:
            _run([target, "plugin", "remove", plugin])
        _run([target, "plugin", "add", plugin, "--json"])
    elif plugin in installed:
        _run([target, "plugin", "update", plugin])
    else:
        _run([target, "plugin", "install", "-y", "--scope", "user", plugin])
    return ()


def _targets(target: str) -> tuple[str, ...]:
    """`all` is every supported agent actually present, not all four.

    Requiring all four meant a machine with only one agent installed got
    `codex executable was not found` and nothing installed -- the preflight
    below checks every selected target before any installer runs, so one
    absent agent aborted the whole run.

    An explicit target still fails loudly when it is missing. You named it,
    so skipping it would report success for something never installed;
    only `all` is a request to take what is there.
    """
    if target != "all":
        return (target,)
    found = tuple(name for name in SUPPORTED if _is_installed(name))
    if not found:
        raise CLIError(f"none of {', '.join(SUPPORTED)} were found on PATH")
    # stderr, not silence: `all` quietly doing less than all is the surprise
    # this function exists to remove.
    skipped = [name for name in SUPPORTED if name not in found]
    if skipped:
        print(f"ach-memory: not installed, skipped: {', '.join(skipped)}", file=sys.stderr)
    return found


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-memory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("target", choices=(*SUPPORTED, "all"))
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        url = _mcp_url(os.environ.get("ACH_MEMORY_URL", "http://localhost:8000"))
        targets = _targets(args.target)
        for target in targets:
            _require_executable(target)
        native_plans = {target: _native_plan(target) for target in targets if target in {"codex", "claude"}}
        config_plans = {target: _config_plan(target, url) for target in targets if target in {"opencode", "pi"}}
        asyncio.run(_preflight(url, os.environ.get("ACH_MEMORY_API_KEY", "")))
        changed: list[Path] = []
        for target in targets:
            if target in native_plans:
                changed.extend(_install_native(target, native_plans[target]))
            elif target == "opencode":
                changed.extend(_install_opencode(url, config_plans[target]))
            else:
                changed.extend(_install_pi(url, config_plans[target]))
    except (CLIError, ValueError) as exc:
        print(f"ach-memory: {exc}", file=sys.stderr)
        return 1

    for path in changed:
        print(path)
    print("Restart the selected agent(s) to load ach-memory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
