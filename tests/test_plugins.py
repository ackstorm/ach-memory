"""Every host this repo claims to support must actually be installable.

The plugin bundle is the repository root: `claude plugin install`,
`codex plugin add`, `opencode plugin` and `pi install` all take this tree as-is,
so a broken manifest is a host that silently gets no memory. Before 0.5.0 the
marketplace pointed at one host's subdirectory and the others shipped copies
nobody installed; nothing failed, because nothing asserted the wiring.
"""

import json
import os
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

HOOK_SCRIPTS = ("session-start.sh", "subagent-start.sh", "pre-compact.sh")

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


def test_both_hosts_get_the_same_three_events():
    assert load("hooks/hooks.json")["hooks"].keys() == load("hooks/codex-hooks.json")["hooks"].keys()


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


def test_package_json_ships_the_whole_tree():
    """`files` would strip `pyproject.toml` and `src/`, and the proxy is built from this tree.

    opencode and pi install the npm package, then run
    `uvx --from <package dir> ach-memory mcp`. A `files` allowlist that leaves
    the Python project out packs a bundle whose MCP server cannot start at all:
    `MCP error -32000: Connection closed`, with nothing to say why.
    """
    assert "files" not in load("package.json")
