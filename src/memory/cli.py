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
from uuid import uuid4

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


class CLIError(Exception):
    """A concise, user-facing CLI failure."""


def _mcp_url(base: str) -> str:
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
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


def _bundle_root() -> Path:
    """Return the installed bundle, or the source-checkout copy for ``uv run``."""
    packaged = resources.files("memory").joinpath("integrations/plugin")
    if packaged.is_dir():
        return Path(packaged)
    return Path(__file__).resolve().parents[2] / "plugins" / "ach-memory"


def _require_executable(target: str) -> None:
    if not shutil.which(target):
        raise CLIError(f"{target} executable was not found")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, check=False, text=True)
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
        if not isinstance(payload, dict) or set(payload) != {"installed"}:
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


def _install_opencode(url: str) -> None:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode"
    config_path = root / "opencode.json"
    config = _read_json_object(config_path)
    existing = config.get("mcp", {})
    if not isinstance(existing, dict):
        raise CLIError(f"invalid JSON in {config_path}")
    mcp = dict(existing)
    mcp["ach-memory"] = {
        "type": "remote",
        "url": url,
        "headers": {"Authorization": "Bearer {env:ACH_MEMORY_API_KEY}"},
    }
    config["mcp"] = mcp
    bundle = _bundle_root()
    for source, destination in (
        (bundle / "adapters" / "opencode.js", root / "plugins" / "ach-memory.js"),
        (bundle / "activation.txt", root / "plugins" / "ach-memory" / "activation.txt"),
        (bundle / "skills" / "ach-memory" / "SKILL.md", root / "skills" / "ach-memory" / "SKILL.md"),
    ):
        _copy_file_atomic(source, destination)
    _write_json_atomic(config_path, config)


def _install_pi(url: str) -> None:
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
    config_path = root / "mcp.json"
    config = _read_json_object(config_path)
    existing = config.get("mcpServers", {})
    if not isinstance(existing, dict):
        raise CLIError(f"invalid JSON in {config_path}")
    servers = dict(existing)
    servers["ach-memory"] = {
        "url": url,
        "auth": "bearer",
        "bearerTokenEnv": "ACH_MEMORY_API_KEY",
        "lifecycle": "lazy",
        "directTools": False,
    }
    config["mcpServers"] = servers
    bundle = _bundle_root()
    for source, destination in (
        (bundle / "adapters" / "pi.js", root / "extensions" / "ach-memory.js"),
        (bundle / "activation.txt", root / "extensions" / "ach-memory" / "activation.txt"),
        (bundle / "skills" / "ach-memory" / "SKILL.md", root / "skills" / "ach-memory" / "SKILL.md"),
    ):
        _copy_file_atomic(source, destination)
    _write_json_atomic(config_path, config)
    _run(["pi", "install", "npm:pi-mcp-adapter"])


def _marketplace_destination(target: str) -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "ach-memory" / f"{target}-marketplace"


def _render_marketplace(target: str, url: str) -> Path:
    destination = _marketplace_destination(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target}-marketplace-", dir=destination.parent))
    shutil.rmtree(staging)
    backup: Path | None = None

    try:
        plugin = staging / "plugins" / "ach-memory"
        shutil.copytree(_bundle_root(), plugin)
        server: dict[str, object] = {"type": "http", "url": url}
        if target == "codex":
            server["bearer_token_env_var"] = "ACH_MEMORY_API_KEY"
            marketplace: object = {
                "name": "ach-memory",
                "interface": {"displayName": "ach-memory"},
                "plugins": [
                    {
                        "name": "ach-memory",
                        "source": {"source": "local", "path": "./plugins/ach-memory"},
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                        "category": "Productivity",
                    }
                ],
            }
            _write_json(staging / ".agents" / "plugins" / "marketplace.json", marketplace)
        else:
            server["headers"] = {"Authorization": "Bearer ${ACH_MEMORY_API_KEY}"}
            marketplace = {
                "name": "ach-memory",
                "description": "Durable memory for coding agents.",
                "owner": {"name": "ackstorm"},
                "plugins": [
                    {
                        "name": "ach-memory",
                        "version": "0.1.0",
                        "description": "Durable memory for Claude.",
                        "source": "./plugins/ach-memory",
                        "category": "productivity",
                    }
                ],
            }
            _write_json(staging / ".claude-plugin" / "marketplace.json", marketplace)
        _write_json(plugin / ".mcp.json", {"mcpServers": {"ach-memory": server}})

        if destination.exists():
            backup = destination.with_name(f".{destination.name}.backup-{uuid4().hex}")
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
        except OSError:
            if backup is not None:
                os.replace(backup, destination)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    except OSError as exc:
        raise CLIError(f"could not write {target} marketplace") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return destination


def _install_native(target: str, url: str) -> None:
    if target not in {"codex", "claude"}:
        raise CLIError(f"native installation is not available for {target}")
    _require_executable(target)

    destination = _marketplace_destination(target)
    marketplaces = _marketplace_locations(
        target, _run_json([target, "plugin", "marketplace", "list", "--json"])
    )
    if "ach-memory" in marketplaces and marketplaces["ach-memory"] != destination.resolve():
        raise CLIError("ach-memory marketplace is registered at a different location")
    installed = _installed_plugins(target, _run_json([target, "plugin", "list", "--json"]))
    destination = _render_marketplace(target, url)
    plugin = "ach-memory@ach-memory"

    if "ach-memory" not in marketplaces:
        command = [target, "plugin", "marketplace", "add"]
        if target == "claude":
            command.extend(["--scope", "user"])
        command.append(str(destination))
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


def _native_targets(target: str) -> tuple[str, ...]:
    if target == "all":
        return "codex", "claude"
    return (target,) if target in {"codex", "claude"} else ()


def _config_targets(target: str) -> tuple[str, ...]:
    if target == "all":
        return "opencode", "pi"
    return (target,) if target in {"opencode", "pi"} else ()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-memory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("target", choices=("codex", "claude", "opencode", "pi", "all"))
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        url = _mcp_url(os.environ.get("ACH_MEMORY_URL", "http://localhost:8000"))
        native_targets = _native_targets(args.target)
        config_targets = _config_targets(args.target)
        for target in native_targets:
            _require_executable(target)
        if "pi" in config_targets:
            _require_executable("pi")
        asyncio.run(_preflight(url, os.environ.get("ACH_MEMORY_API_KEY", "")))
        for target in native_targets:
            _install_native(target, url)
        for target in config_targets:
            if target == "opencode":
                _install_opencode(url)
            else:
                _install_pi(url)
    except (CLIError, ValueError) as exc:
        print(f"ach-memory: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
