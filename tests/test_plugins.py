"""Every host this repo claims to support must actually be installable.

The plugin bundle is the repository root: `claude plugin install`,
`codex plugin add`, `opencode plugin` and `pi install` all take this tree as-is,
so a broken manifest is a host that silently gets no memory. Before 0.5.0 the
marketplace pointed at one host's subdirectory and the others shipped copies
nobody installed; nothing failed, because nothing asserted the wiring.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "src" / "memory" / "__init__.py").read_text().split('"')[1]

MANIFESTS = (
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "mcp.json",
    "codex-mcp.json",
    "hooks/hooks.json",
    "hooks/codex-hooks.json",
    "hooks/activation.subagent.json",
    "package.json",
)

HOOK_SCRIPTS = ("session-start.sh", "subagent-start.sh", "pre-compact.sh", "retain-nudge.sh", "stop.sh", "idle-nudge.sh")

# Claude Code and Codex both expand their own plugin-root variable and nothing
# else. A hook that names the wrong one resolves to an empty path and dies.
HOOK_FILES = {"hooks/hooks.json": "CLAUDE_PLUGIN_ROOT", "hooks/codex-hooks.json": "PLUGIN_ROOT"}


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text())


@pytest.mark.parametrize("relative", MANIFESTS)
def test_manifest_is_valid_json(relative):
    assert isinstance(load(relative), dict)


def test_marketplace_source_is_the_repository_root():
    """`source: "./"` is what lets one tree serve every host."""
    (entry,) = load(".claude-plugin/marketplace.json")["plugins"]
    assert entry["source"] == "./"


@pytest.mark.parametrize("relative", (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"))
def test_plugin_manifest_declares_paths_that_exist(relative):
    manifest = load(relative)
    for key in ("skills", "mcpServers", "hooks"):
        declared = manifest.get(key)
        if declared:
            assert (ROOT / declared.lstrip("./")).exists(), f"{relative}: {key} -> {declared}"


def test_codex_manifest_declares_what_codex_cannot_discover():
    """Claude Code finds `hooks/`, `skills/` and the MCP file by convention; Codex does not."""
    manifest = load(".codex-plugin/plugin.json")
    assert manifest["hooks"] == "./hooks/codex-hooks.json"
    assert manifest["mcpServers"] == "./codex-mcp.json"
    assert manifest["skills"] == "./skills/"


@pytest.mark.parametrize("relative,variable", HOOK_FILES.items())
def test_hooks_name_their_own_plugin_root_and_point_at_real_scripts(relative, variable):
    other = "PLUGIN_ROOT" if variable == "CLAUDE_PLUGIN_ROOT" else "CLAUDE_PLUGIN_ROOT"
    for event, groups in load(relative)["hooks"].items():
        for entry in (hook for group in groups for hook in group["hooks"]):
            command = entry["command"]
            assert f"${{{variable}}}" in command, f"{relative}: {event} does not use {variable}"
            if variable == "PLUGIN_ROOT":  # substring of the Claude name, so check explicitly
                assert f"${{{other}}}" not in command, f"{relative}: {event} uses {other}"
            script = command.split("/hooks/scripts/")[1].strip('"')
            assert (ROOT / "hooks" / "scripts" / script).exists()


def test_both_hosts_get_the_same_events():
    assert load("hooks/hooks.json")["hooks"].keys() == load("hooks/codex-hooks.json")["hooks"].keys()


def run_hook(script: str, payload: str, tmp_path: Path, **env: str) -> str:
    # A failing `uvx` shadow makes the script print its own fallback text, deterministically.
    (tmp_path / "uvx").write_text("#!/bin/sh\nexit 1\n")
    (tmp_path / "uvx").chmod(0o755)
    full_env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path), "PATH": f"{tmp_path}:{os.environ['PATH']}", **env}
    return subprocess.run(
        [str(ROOT / "hooks" / "scripts" / script)],
        input=payload,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=tmp_path,
        check=True,
    ).stdout


def stop(payload: str, tmp_path: Path, interval: str = "900") -> str:
    return run_hook("stop.sh", payload, tmp_path, ACH_MEMORY_NUDGE_INTERVAL=interval)


def test_idle_nudge_fires_only_when_nothing_was_retained_and_once_per_window(tmp_path):
    """The proxy stamps retains under the same key; a fresh checkout starts the clock instead of nagging."""
    def idle(payload: str) -> str:
        return run_hook("idle-nudge.sh", payload, tmp_path, ACH_MEMORY_IDLE_INTERVAL="600")

    stamp = tmp_path / "ach-memory" / ("retain-" + str(tmp_path).replace("/", "_"))

    assert idle('{"session_id": "s1"}') == ""  # first prompt ever: clock starts, no nag
    assert stamp.exists()
    stamp.write_text("0")  # nothing retained for ages
    assert "for over 30 minutes" in idle('{"session_id": "s1"}')
    assert idle('{"session_id": "s1"}') == ""  # marked as sent for this session
    assert idle('{"session_id": "s2"}') != ""  # another session has its own mark
    stamp.write_text(str(10**10))  # a retain just happened
    assert idle('{"session_id": "s3"}') == ""


def test_subagent_activation_is_the_session_activation():
    """One text, two carriers: the JSON is generated from the txt and must not drift."""
    text = (ROOT / "hooks" / "activation.txt").read_text().rstrip("\n")
    assert load("hooks/activation.subagent.json")["hookSpecificOutput"]["additionalContext"] == text


def test_stop_hook_blocks_once_per_interval_and_never_loops(tmp_path):
    """Stop stdout never reaches the model: only a `block` decision does, and each costs a turn."""
    first = json.loads(stop('{"session_id": "s1", "stop_hook_active": false}', tmp_path))
    assert first["decision"] == "block" and "Retain ONLY" in first["reason"]
    assert stop('{"session_id": "s1"}', tmp_path) == ""  # inside the window
    assert stop('{"session_id": "s2"}', tmp_path) != ""  # another session has its own window
    assert stop('{"session_id": "s3", "stop_hook_active": true}', tmp_path) == ""  # already continuing
    assert stop("not json", tmp_path, interval="0") != ""  # garbage input still fails open, not loud


@pytest.mark.parametrize("script", HOOK_SCRIPTS)
def test_hook_scripts_are_executable(script):
    assert os.access(ROOT / "hooks" / "scripts" / script, os.X_OK)


@pytest.mark.parametrize("relative,variable", (("mcp.json", "CLAUDE_PLUGIN_ROOT"), ("codex-mcp.json", "PLUGIN_ROOT")))
def test_mcp_runs_the_proxy_from_the_plugin_root(relative, variable):
    """No version pin: the host config freezes whatever it is given at install time."""
    args = load(relative)["mcpServers"]["ach-memory"]["args"]
    assert args[:2] == ["--from", f"${{{variable}}}"]
    assert not any("@v" in arg for arg in args)


def test_package_json_gives_opencode_and_pi_their_entry_points():
    package = load("package.json")
    assert (ROOT / package["main"]).exists()
    assert (ROOT / package["pi"]["extensions"][0]).exists()
    assert (ROOT / package["pi"]["skills"][0]).is_dir()


def test_every_manifest_carries_the_release_version():
    for relative in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "package.json"):
        assert load(relative)["version"] == VERSION, relative
    (entry,) = load(".claude-plugin/marketplace.json")["plugins"]
    assert entry["version"] == VERSION


def test_the_skill_has_exactly_one_copy():
    assert [p.relative_to(ROOT) for p in ROOT.glob("**/SKILL.md") if ".venv" not in p.parts] == [
        Path("skills/ach-memory/SKILL.md")
    ]


def test_the_plugin_endpoint_reads_where_the_image_writes():
    """The HTTPS install path is a Dockerfile COPY and a route that agree on one filename.

    Nothing else connects them: move either and the endpoint 404s on a healthy service,
    which reads as "this release has no bundle" rather than as a broken build.
    """
    from memory.mcp.server import PLUGIN_TARBALL

    copied = [
        line.split()[2]
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("COPY ") and line.rstrip().endswith(".tgz")
    ]
    assert copied == [str(PLUGIN_TARBALL)]


def test_package_json_ships_the_whole_tree():
    """`files` would strip `pyproject.toml` and `src/`, and the proxy is built from this tree.

    opencode and pi install the npm package, then run
    `uvx --from <package dir> ach-memory mcp`. A `files` allowlist that leaves
    the Python project out packs a bundle whose MCP server cannot start at all:
    `MCP error -32000: Connection closed`, with nothing to say why.
    """
    assert "files" not in load("package.json")
