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
GIT_SOURCE = "git+https://github.com/ackstorm/ach-memory"


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
    """Where each host keeps the config we are allowed to edit.

    opencode deliberately reads XDG_CONFIG_HOME and not OPENCODE_CONFIG.
    It has no private config-dir variable: its root is computed as
    `(XDG_CONFIG_HOME || ~/.config)/opencode` and nothing else feeds into it.
    OPENCODE_CONFIG, OPENCODE_CONFIG_DIR and OPENCODE_CONFIG_CONTENT only
    *append* to its search path, so writing to one of those would not put our
    server where opencode looks first, and honouring them here would move our
    entry out of the file the user edits by hand. Measured in
    github.com/ackstorm/agent-profile, which had to shim XDG_CONFIG_HOME for
    exactly this reason -- do not "fix" this to OPENCODE_CONFIG without new
    measurements.
    """
    if target == "opencode":
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode"
    if target == "pi":
        return Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
    raise CLIError(f"config installation is not available for {target}")


def _proxy_command(mode: str, url: str) -> list[str]:
    """The spawn line hosts use for the stdio proxy.

    The endpoint travels as `--url`, not as an inherited ACH_MEMORY_URL: a
    config whose every input is invisible is unreadable and undebuggable,
    and a host that does not export the variable failed at the first tool
    call rather than at install. The API KEY deliberately does NOT travel
    here -- argv is world-readable (`ps aux`), so the key stays in the
    config's `env` block, which is where every MCP server in the ecosystem
    puts a credential.

    `uvx --from git+...@vX.Y.Z` is the install source: the repository is
    public, the tag pins an immutable revision, and it needs no package
    index -- so a release is installable the moment CI tags it.

    "local" resolves this environment's own console script to an absolute
    path, so a host launches the code sitting in this checkout instead of
    the published tag -- the only way to try an unreleased proxy end to
    end. Resolved at init time on purpose: the written config then works
    from any cwd, and a broken PATH fails here, loudly, not at the host's
    first tool call.
    """
    if mode == "local":
        script = shutil.which("ach-memory")
        if not script:
            raise CLIError(
                "--local needs the ach-memory script on PATH "
                "(run it as `uv run ach-memory init ... --local`)"
            )
        return [str(Path(script).resolve()), "mcp", "--url", url]
    return ["uvx", "--from", f"{GIT_SOURCE}@v{_version()}", "ach-memory", "mcp", "--url", url]


def _config_plan(
    target: str, url: str, mode: str = "stdio"
) -> tuple[Path, dict[str, object], tuple[tuple[Path, Path], ...]]:
    root = _config_root(target)
    config_path = root / ("opencode.json" if target == "opencode" else "mcp.json")
    config = _read_json_object(config_path)
    server_key = "mcp" if target == "opencode" else "mcpServers"
    existing = config.get(server_key, {})
    if not isinstance(existing, dict):
        raise CLIError(f"invalid JSON in {config_path}")

    server: dict[str, object]
    if target == "opencode":
        if mode == "http":
            # Direct remote entry, kept as an opt-in escape hatch: same
            # endpoint the stdio proxy forwards to, minus the client-side
            # project auto-resolution the proxy adds.
            server = {
                "type": "remote",
                "url": url,
                "headers": {"Authorization": "Bearer {env:ACH_MEMORY_API_KEY}"},
            }
        else:
            server = {
                "type": "local",
                "command": _proxy_command(mode, url),
                # The key by NAME through opencode's own {env:...} form: the
                # proxy inherits it as ACH_MEMORY_API_KEY, and no secret is
                # written into a config file or into argv.
                "environment": {"ACH_MEMORY_API_KEY": "{env:ACH_MEMORY_API_KEY}"},
                "enabled": True,
            }
        paths = (
            ("opencode.js", "plugins/ach-memory.js"),
            ("activation.txt", "plugins/ach-memory/activation.txt"),
            ("skills/ach-memory/SKILL.md", "skills/ach-memory/SKILL.md"),
        )
    else:
        if mode == "http":
            # pi cannot send a bearer header natively; the HTTP escape hatch
            # keeps the pi-mcp-adapter bridge it always needed for remote.
            server = {
                "url": url,
                "auth": "bearer",
                "bearerTokenEnv": "ACH_MEMORY_API_KEY",
                "lifecycle": "lazy",
                "directTools": False,
            }
        else:
            command = _proxy_command(mode, url)
            # No env block: pi's stdio servers inherit pi's own environment,
            # which is where ACH_MEMORY_API_KEY already lives. Restating it
            # here would only be a place for a secret to get pasted.
            server = {"command": command[0], "args": command[1:]}
        paths = (
            ("pi.js", "extensions/ach-memory.js"),
            ("activation.txt", "extensions/ach-memory/activation.txt"),
            ("skills/ach-memory/SKILL.md", "skills/ach-memory/SKILL.md"),
        )
    config[server_key] = {**existing, "ach-memory": server}

    if target == "opencode":
        # opencode finds skills only in the directories it is told about, and
        # the default is the project-local `.opencode/skills`. We install ours
        # under the user config dir, which is outside that -- so the skill sat
        # on disk, invisible, and opencode was the one host that never read it.
        # Measured: told in Spanish to remember a fact, it stored the fact in
        # Spanish, while the three hosts that did read the skill stored English.
        #
        # `.opencode/skills` is re-stated when we create the key, because
        # writing `paths` at all replaces the default rather than adding to it,
        # and silently switching a user's project-local skills off is a far
        # worse bug than the one being fixed here.
        skills = config.get("skills", {})
        if not isinstance(skills, dict):
            raise CLIError(f"invalid JSON in {config_path}")
        configured = skills.get("paths", [".opencode/skills"])
        if not isinstance(configured, list):
            raise CLIError(f"invalid JSON in {config_path}")
        ours = str(root / "skills")
        if ours not in configured:
            configured = [*configured, ours]
        config["skills"] = {**skills, "paths": configured}

        # Same story for the adapter that injects activation.txt. opencode
        # auto-loads `.opencode/plugin/` and `.opencode/plugins/` -- both
        # project-local -- and otherwise takes an explicit `plugin` array.
        # Ours sat in the user config dir with nothing pointing at it, so
        # opencode never ran it: asked to quote its ach-memory instructions it
        # produced the MCP server blurb and the skill entry, and no activation
        # text at all. A relative entry resolves against the declaring config,
        # which is this file.
        declared = config.get("plugin", [])
        if not isinstance(declared, list):
            raise CLIError(f"invalid JSON in {config_path}")
        if "./plugins/ach-memory.js" not in declared:
            config["plugin"] = [*declared, "./plugins/ach-memory.js"]

    bundle = _bundle_root(target)
    assets = tuple((bundle / source, root / destination) for source, destination in paths)
    for source, _destination in assets:
        if not source.is_file():
            raise CLIError(f"missing bundled {target} asset")
    return config_path, config, assets


def _install_opencode(
    url: str,
    plan: tuple[Path, dict[str, object], tuple[tuple[Path, Path], ...]] | None = None,
    mode: str = "stdio",
) -> tuple[Path, ...]:
    config_path, config, assets = plan or _config_plan("opencode", url, mode)
    for source, destination in assets:
        _copy_file_atomic(source, destination)
    _write_json_atomic(config_path, config)
    return (config_path, *(destination for _source, destination in assets))


def _install_pi(
    url: str,
    plan: tuple[Path, dict[str, object], tuple[tuple[Path, Path], ...]] | None = None,
    mode: str = "stdio",
) -> tuple[Path, ...]:
    """pi spawns our stdio proxy as a native mcp.json server -- no adapter to
    install. The `pi-mcp-adapter` package was only ever needed to bridge pi
    to a remote HTTP server; a plain `command`/`args` stdio entry is pi's own
    format and needs nothing extra on top -- so the adapter install only
    happens for the --http escape hatch, which is the one mode that still
    talks to the remote endpoint from inside pi."""
    config_path, config, assets = plan or _config_plan("pi", url, mode)
    for source, destination in assets:
        _copy_file_atomic(source, destination)
    _write_json_atomic(config_path, config)
    if mode == "http":
        _run(["pi", "install", "npm:pi-mcp-adapter"])
    return (config_path, *(destination for _source, destination in assets))


def _register_codex_server(url: str, mode: str = "stdio") -> None:
    """Register the MCP server with Codex, because its plugin cannot.

    Codex now spawns our stdio proxy directly (`uvx ach-memory mcp`), which
    reads ACH_MEMORY_URL and ACH_MEMORY_API_KEY from its own environment on
    every launch. Nothing about the endpoint is written into Codex's config
    at install time any more, so re-running init after changing the URL is
    no longer required for codex. Printing the command for the user to paste
    left the install half done by default: plugin present, server absent,
    every tool call failing while everything looked correct.

    The --http escape hatch is the exception: Codex cannot interpolate
    ${ACH_MEMORY_URL} in a URL, so that mode writes the endpoint literally
    (and main() requires the variable to be set, or localhost would be
    pinned permanently while looking successful). bearer_token_env_var
    stores the variable's NAME and Codex resolves it per call.

    Unconditional remove-then-add, with no read of the current state: whatever
    Codex held before, it ends up matching this command, so re-running init is
    also the update path. `codex mcp remove` exits 0 when the server is
    absent, so the remove needs no guard.
    """
    _run(["codex", "mcp", "remove", "ach-memory"])
    if mode == "http":
        _run([
            "codex", "mcp", "add", "ach-memory",
            "--url", url,
            "--bearer-token-env-var", "ACH_MEMORY_API_KEY",
        ])
    else:
        _run(["codex", "mcp", "add", "ach-memory", "--", *_proxy_command(mode, url)])


def _native_plan(target: str) -> tuple[dict[str, Path], set[str]]:
    marketplaces = _marketplace_locations(
        target, _run_json([target, "plugin", "marketplace", "list", "--json"])
    )
    installed = _installed_plugins(target, _run_json([target, "plugin", "list", "--json"]))
    return marketplaces, installed


def _install_native(
    target: str,
    url: str,
    plan: tuple[dict[str, Path], set[str]] | None = None,
    mode: str = "stdio",
) -> str:
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

    if target == "codex":
        _register_codex_server(url, mode)
    if target == "claude" and mode != "stdio":
        # The claude plugin ships a committed, static .mcp.json (stdio via
        # uvx); a flag at install time cannot rewrite it. README documents
        # the direct-HTTP config to paste by hand.
        return (
            f"plugin installed from {MARKETPLACE} "
            f"(stdio; --{mode} does not apply, see README for direct HTTP)"
        )
    return f"plugin installed from {MARKETPLACE}"


def _version() -> str:
    from importlib import metadata

    try:
        return metadata.version("ach-memory")
    except metadata.PackageNotFoundError:
        return "dev"


def _tilde(path: Path) -> str:
    """~/... beats a 60-character absolute path in a status line."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _report(
    results: "list[tuple[str, str, tuple[Path, ...]]]",
    skipped: "list[str]",
    url: str,
    url_from_env: bool,
    verbose: bool,
) -> None:
    """Say what happened to every requested agent, including the ones that got
    no files.

    claude and codex install through their host's marketplace and write nothing
    of ours, so a listing of changed paths -- which is all this used to print --
    showed nothing for them at all. `init all` on a machine with four agents
    reported two, and the silence was indistinguishable from not being
    installed.
    """
    # The base URL, not the derived /mcp/ endpoint: this is the value the
    # reader exported and the one they will compare against.
    print(f"ach-memory {_version()}  \u2192  {url}", end="")
    print("" if url_from_env else "  (ACH_MEMORY_URL unset)")
    print()
    if not url_from_env:
        print(f"  ! ACH_MEMORY_URL is not set, using {url}")
        print("    export it in ~/.zshrc for a remote deployment")
        print()

    width = max((len(name) for name, _, _ in results), default=0)
    width = max(width, *(len(name) for name in skipped)) if skipped else width
    for name, summary, paths in results:
        print(f"  \u2714 {name.ljust(width)}  {summary}")
        if verbose:
            # Relative to the root the summary line just named. Repeating an
            # absolute path on every line buries the part that differs, and a
            # relocated root (XDG_CONFIG_HOME, PI_CODING_AGENT_DIR) can be long
            # enough to push the filename off the terminal entirely.
            root = paths[0].parent if paths else None
            for path in paths:
                try:
                    shown = path.relative_to(root) if root else path
                except ValueError:
                    shown = path
                print(f"    {' ' * width}  {shown}")
    for name in skipped:
        print(f"  \u2013 {name.ljust(width)}  skipped, not on PATH")

    installed = [name for name, _, _ in results]
    if installed:
        print()
        names = installed[0] if len(installed) == 1 else (
            " and ".join([", ".join(installed[:-1]), installed[-1]])
        )
        print(f"Restart {names} to load ach-memory.")


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
    return found


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-memory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("target", choices=(*SUPPORTED, "all"))
    init.add_argument(
        "-v", "--verbose", action="store_true", help="list every file written"
    )
    transport = init.add_mutually_exclusive_group()
    transport.add_argument(
        "--http",
        action="store_true",
        help="configure hosts against the remote HTTP endpoint directly "
        "instead of the stdio proxy (no client-side project resolution)",
    )
    transport.add_argument(
        "--local",
        action="store_true",
        help="stdio proxy from this checkout's ach-memory script instead of "
        "uvx, to test unreleased code",
    )
    mcp = commands.add_parser(
        "mcp", help="run the local stdio MCP proxy"
    )
    mcp.add_argument(
        "--url",
        default=None,
        help="memory service base URL (default: $ACH_MEMORY_URL). The API key "
        "is read from $ACH_MEMORY_API_KEY and never taken as an argument, "
        "because argv is world-readable",
    )
    return parser


def _serve_mcp(url_argument: str | None = None) -> int:
    """Run the stdio proxy until the host closes stdin.

    Imported lazily: `init` must keep working on an interpreter where
    fastmcp failed to install, and a plain `ach-memory --help` should not
    pay the fastmcp import.

    The endpoint comes from --url when the host config states it (what
    `init` writes, so the config is self-describing) and falls back to
    ACH_MEMORY_URL for a hand-written config or a shell run. The key is
    environment-only on purpose: `ps aux` shows every argument of every
    process on the machine, so a credential must never be one.
    """
    from memory.mcp import proxy

    key = os.environ.get("ACH_MEMORY_API_KEY", "")
    if not key:
        print(
            "ach-memory: ACH_MEMORY_API_KEY must be set to run the MCP proxy",
            file=sys.stderr,
        )
        return 1
    url = _mcp_url(
        url_argument or os.environ.get("ACH_MEMORY_URL") or "http://localhost:8000"
    )
    proxy.build_proxy(url, key).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.command == "mcp":
        return _serve_mcp(args.url)

    base = os.environ.get("ACH_MEMORY_URL")
    mode = "http" if args.http else ("local" if args.local else "stdio")
    try:
        url = _mcp_url(base or "http://localhost:8000")
        targets = _targets(args.target)
        for target in targets:
            _require_executable(target)
        # The stdio proxy reads ACH_MEMORY_URL per launch, so the localhost
        # fallback is just a warning -- except for --http codex, which writes
        # the endpoint literally and would pin localhost permanently while
        # looking successful.
        if mode == "http" and "codex" in targets and base is None:
            raise CLIError(
                "ACH_MEMORY_URL must be set for --http codex: it stores the "
                "endpoint literally and cannot read the variable later"
            )
        # Progress goes to stderr: init spends seconds inside the host CLIs
        # (`claude plugin list` alone takes a few) and one network round-trip,
        # and silence reads as a hang. stderr keeps the final stdout report
        # clean and pipeable.
        native_plans = {}
        for target in targets:
            if target in {"codex", "claude"}:
                print(f"  … querying {target} plugin state", file=sys.stderr)
                native_plans[target] = _native_plan(target)
        config_plans = {target: _config_plan(target, url, mode) for target in targets if target in {"opencode", "pi"}}
        print(f"  … verifying MCP endpoint {url}", file=sys.stderr)
        asyncio.run(_preflight(url, os.environ.get("ACH_MEMORY_API_KEY", "")))
        results: list[tuple[str, str, tuple[Path, ...]]] = []
        for target in targets:
            print(f"  … installing {target}", file=sys.stderr)
            if target in native_plans:
                results.append((target, _install_native(target, url, native_plans[target], mode), ()))
            else:
                install = _install_opencode if target == "opencode" else _install_pi
                paths = install(url, config_plans[target], mode)
                summary = (
                    f"{len(paths)} files \u2192 {_tilde(paths[0].parent)}" if paths else "configured"
                )
                results.append((target, summary, paths))
    except (CLIError, ValueError) as exc:
        print(f"ach-memory: {exc}", file=sys.stderr)
        # The failure lands here before the summary that would have said which
        # endpoint was used, and an unexported ACH_MEMORY_URL silently means
        # localhost -- so "MCP preflight failed" on its own points at the
        # service rather than at the missing variable that caused it.
        if base is None:
            print(
                f"ach-memory: ACH_MEMORY_URL is not set, so this tried {url}",
                file=sys.stderr,
            )
            print(
                "ach-memory: export it in ~/.zshrc for a remote deployment",
                file=sys.stderr,
            )
        return 1

    # Reported here rather than inside _targets: a skip is part of the summary,
    # not a warning to shout from stderr while the real output goes to stdout.
    skipped = [name for name in SUPPORTED if name not in targets] if args.target == "all" else []
    _report(results, skipped, base or "http://localhost:8000", base is not None, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
