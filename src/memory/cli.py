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


def _marketplace_names(target: str, payload: object) -> set[str]:
    entries = payload.get("marketplaces") if target == "codex" and isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise CLIError(f"{target} returned unsupported marketplace JSON")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("name"), str) for entry in entries):
        raise CLIError(f"{target} returned unsupported marketplace JSON")
    return {entry["name"] for entry in entries}


def _installed_plugins(target: str, payload: object) -> set[str]:
    entries = payload.get("installed") if target == "codex" and isinstance(payload, dict) else payload
    field = "pluginId" if target == "codex" else "id"
    if not isinstance(entries, list):
        raise CLIError(f"{target} returned unsupported plugin JSON")
    if any(not isinstance(entry, dict) or not isinstance(entry.get(field), str) for entry in entries):
        raise CLIError(f"{target} returned unsupported plugin JSON")
    return {entry[field] for entry in entries}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _render_marketplace(target: str, url: str) -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    destination = data_home / "ach-memory" / f"{target}-marketplace"
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

    marketplace = _marketplace_names(
        target, _run_json([target, "plugin", "marketplace", "list", "--json"])
    )
    installed = _installed_plugins(target, _run_json([target, "plugin", "list", "--json"]))
    destination = _render_marketplace(target, url)
    plugin = "ach-memory@ach-memory"

    if "ach-memory" not in marketplace:
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
        targets = _native_targets(args.target)
        for target in targets:
            _require_executable(target)
        asyncio.run(_preflight(url, os.environ.get("ACH_MEMORY_API_KEY", "")))
        for target in targets:
            _install_native(target, url)
    except (CLIError, ValueError) as exc:
        print(f"ach-memory: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
