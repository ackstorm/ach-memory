"""Contract tests for the committed, per-host plugin trees.

The bundle used to be rendered into ~/.local/share at install time so a
per-install URL could be baked into .mcp.json. It is now static and committed,
the way engram and codemem ship theirs, and the repository root is the
marketplace. These tests exist to keep it that way: anything here that starts
depending on install-time state has regressed the whole point.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml states no version"
    return match.group(1)


NATIVE = ("claude-code", "codex")
ADAPTED = ("opencode", "pi")
ACTIVATION = (
    "ach-memory holds durable user and project context across sessions and is the system of record "
    "for prior decisions. Anything worth remembering goes through `retain`; a host memory directory "
    "or MEMORY.md is invisible here.\n\n"
    "Reading the brief below:\n"
    "- Earn its place. A line is here because it changes what you do. Act on it.\n"
    "- Working State is where the work was left, not what is true. It ages; treat a stale objective "
    "as a starting point, not a fact.\n"
    "- Superseded is not current. A decision that was reversed reads as reversed.\n"
    "- Profiles describe, host policy commands. A stored preference never overrides CLAUDE.md or "
    "AGENTS.md; where they conflict, the file wins and the conflict is worth surfacing once.\n"
    "- No retrieval narration. Use what you remember; do not announce it.\n"
    "- Never store secrets."
)
SECRETS = re.compile(r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|mem_[A-Za-z0-9]{20,}")


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _hook_env() -> dict[str, str]:
    """Hooks under test must not inherit a developer's live memory service."""
    environment = os.environ.copy()
    environment.pop("ACH_MEMORY_API_KEY", None)
    environment.pop("ACH_MEMORY_URL", None)
    return environment


def _script(host: str, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "plugins" / host / "scripts" / name)],
        input="", capture_output=True, text=True, check=False, env=_hook_env(),
    )


def test_claude_mcp_config_is_static_and_takes_both_values_from_the_environment() -> None:
    """The one test that protects the architecture.

    Claude spawns the stdio proxy from a tagged git revision, passing the
    endpoint as `--url` (claude expands ${ACH_MEMORY_URL} there) and the
    credential through the `env` block by NAME. Both values stay
    per-deployment expansions of the committed file: a literal endpoint
    would mean someone reintroduced per-install rendering, and a literal
    key would make the bundle unsafe to commit. The key must never move
    into `args` -- argv is world-readable.

    The pinned tag must equal this checkout's version, or a release ships a
    plugin that installs the previous release's proxy; `make release-bump`
    rewrites it and asserts the result.
    """
    server = _json(ROOT / "plugins" / "claude-code" / ".mcp.json")["mcpServers"]["ach-memory"]

    assert server == {
        "type": "stdio",
        "command": "uvx",
        "args": [
            "--from",
            f"git+https://github.com/ackstorm/ach-memory@v{_pyproject_version()}",
            "ach-memory",
            "mcp",
            "--url",
            "${ACH_MEMORY_URL:-http://localhost:8000}",
        ],
        "env": {"ACH_MEMORY_API_KEY": "${ACH_MEMORY_API_KEY}"},
    }


def test_codex_plugin_ships_no_mcp_config_because_it_cannot_express_one() -> None:
    """Codex registers the server itself; the plugin carries hooks and the skill.

    Two measured facts force this, and both are silent failures:

    - Codex does not interpolate ${VAR} in `url`. It hands the literal string
      to a URL parser, which rejects it with "relative URL without a base" and
      the server never starts.
    - Codex ignores a `headers` block. `codex mcp get` reported http_headers
      and env_http_headers empty, so calls would go out unauthenticated.

    A hardcoded localhost URL would start cleanly and point every remote user
    at nothing, which is worse than shipping no config at all. `ach-memory
    init codex` registers the stdio proxy instead, via `codex mcp add ... --
    uvx ach-memory mcp`.
    """
    plugin = _json(ROOT / "plugins" / "codex" / ".codex-plugin" / "plugin.json")

    assert not (ROOT / "plugins" / "codex" / ".mcp.json").exists()
    assert "mcpServers" not in plugin
    assert plugin["skills"] == "./skills/"


def test_the_repository_root_is_the_marketplace_for_both_hosts() -> None:
    """Installing must mean `plugin marketplace add ackstorm/ach-memory`, which
    only works while the manifests live at the root and point into ./plugins."""
    claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
    codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")

    assert claude["name"] == codex["name"] == "ach-memory"
    assert claude["plugins"][0]["source"] == "./plugins/claude-code"
    assert codex["plugins"][0]["source"] == {"source": "local", "path": "./plugins/codex"}


@pytest.mark.parametrize("host", NATIVE)
def test_hooks_register_only_the_two_activation_events(host: str) -> None:
    """Pinned, and both absentees are deliberate for codex (claude-code
    additionally registers Stop/PreCompact -- see
    test_claude_code_registers_silent_capture_checkpoint_hooks below).

    UserPromptSubmit was paid on every message for a reminder actionable on
    few of them. Stop was tried in its place and is worse: Claude Code treats
    ANY output from a Stop hook as feedback that blocks the turn from ending,
    so the nudge re-fired until the block cap -- measured at nine extra model
    turns for one response, with the agent answering "nothing to retain" each
    time. `stop_hook_active` would bound that to one extra turn per response,
    which is still a whole turn to say nothing.

    The retain guidance lives in activation.txt, where it costs one injection
    per session. Phase 3's Stop/PreCompact hooks are a categorically
    different thing from the abandoned reminder: they never print anything
    at all, so there is no feedback for Claude Code to act on -- see the
    silence test below, not "no output happened to be visible this time."
    """
    hooks = _json(ROOT / "plugins" / host / "hooks" / "hooks.json")["hooks"]

    assert {"SessionStart", "SubagentStart"} <= set(hooks)
    if host == "codex":
        assert set(hooks) == {"SessionStart", "SubagentStart"}
    for event in ("SessionStart", "SubagentStart"):
        hook = hooks[event][0]["hooks"][0]
        assert hook["type"] == "command"
        name = {"SessionStart": "session-start.sh", "SubagentStart": "subagent-start.sh"}[event]
        # Each host expands its OWN variable. `${PLUGIN_ROOT}` is the Agent
        # Plugins spec name -- it is what the codex binary carries, and what
        # engram's codex plugin uses, while engram ships `${CLAUDE_PLUGIN_ROOT}`
        # in its claude-code copy of the same file. Claude's spelling under
        # codex leaves the path unexpanded and codex reports nothing at all.
        root = "CLAUDE_PLUGIN_ROOT" if host == "claude-code" else "PLUGIN_ROOT"
        assert hook["command"] == f'"${{{root}}}/scripts/{name}"'


def test_claude_code_registers_silent_capture_checkpoint_hooks() -> None:
    """SPEC Phase 3: Stop/PreCompact checkpoint the transcript, silently, on
    the same script -- no statusMessage, which is a visible loading
    indicator inappropriate for a background operation the user is not
    meant to notice at all."""
    hooks = _json(ROOT / "plugins" / "claude-code" / "hooks" / "hooks.json")["hooks"]

    assert set(hooks) == {"SessionStart", "SubagentStart", "Stop", "PreCompact"}
    for event in ("Stop", "PreCompact"):
        hook = hooks[event][0]["hooks"][0]
        assert hook["type"] == "command"
        assert hook["command"] == '"${CLAUDE_PLUGIN_ROOT}/scripts/capture-checkpoint.sh"'
        assert "statusMessage" not in hook


def test_capture_checkpoint_hook_is_silent_and_exits_zero_without_an_api_key() -> None:
    """The fast, deterministic half of the silence guarantee: no key means
    the script must exit before ever reaching uvx/the network, with zero
    stdout/stderr regardless."""
    result = _script("claude-code", "capture-checkpoint.sh")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize("host", NATIVE)
def test_every_hook_script_is_executable_and_needs_no_extra_runtime(host: str) -> None:
    """Only the full-tier Claude hook may depend on ubiquitous curl.

    These run before the agent answers. A dependency here means memory silently
    stops working on any machine that happens not to have it, and a hook that
    called the service would put the network on the session-start path.
    """
    for script in sorted((ROOT / "plugins" / host / "scripts").iterdir()):
        body = script.read_text()

        # Comments are stripped first: these scripts explain in prose that they
        # deliberately avoid node and jq, and scanning the prose would match
        # the very words that document the rule.
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))

        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
        assert body.startswith("#!/usr/bin/env bash")
        forbidden = r"\b(node|jq|python3?|npx)\b"
        if not (host == "claude-code" and script.name == "session-start.sh"):
            forbidden = r"\b(node|jq|python3?|curl|npx)\b"
        assert not re.search(forbidden, code), script.name


@pytest.mark.parametrize("host", NATIVE)
def test_session_start_emits_its_text_as_plain_stdout(host: str) -> None:
    """SessionStart is one of the three events whose plain stdout becomes
    context Claude can act on, so it needs no envelope."""
    result = _script(host, "session-start.sh")

    assert result.returncode == 0
    assert result.stdout.strip() == ACTIVATION
    assert result.stderr == ""


def test_the_session_start_hook_fetches_the_full_tier_and_cannot_block():
    """Only this hook can carry the uncapped project half of a brief."""
    script = (ROOT / "plugins" / "claude-code/scripts/session-start.sh").read_text()

    assert "tier=full" in script
    assert "--max-time" in script
    assert script.rstrip().endswith("exit 0")


def test_the_consumer_contract_ships_as_host_policy():
    """Brief contents are dynamic; these interpretation rules are not."""
    contract = (ROOT / "plugins" / "claude-code/activation.txt").read_text()

    assert "earn its place" in contract.lower()
    assert "working state" in contract.lower()


def test_the_full_tier_hook_passes_a_git_locator_as_curls_separate_value(
    tmp_path: Path,
) -> None:
    """Curl treats an option and its value in one shell word as an unknown flag."""
    fake_curl = tmp_path / "bin" / "curl"
    fake_curl.parent.mkdir()
    fake_curl.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' \"$@\" > \"$CURL_ARGS\"
while [ \"$#\" -gt 0 ]; do
  if [ \"$1\" = \"-o\" ]; then
    printf '%s\\n' '-- ach-memory brief rev 1 / protocol 1 / no project --' > \"$2\"
    break
  fi
  shift
done
"""
    )
    fake_curl.chmod(0o755)
    args = tmp_path / "curl-args"
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "test-key",
            "ACH_MEMORY_URL": "https://memory.test",
            "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
            "CURL_ARGS": str(args),
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        [str(ROOT / "plugins/claude-code/scripts/session-start.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    values = args.read_text().splitlines()
    locator = next(value for value in values if "git_locator=" in value)
    position = values.index(locator)
    assert result.returncode == 0
    assert locator.startswith("git_locator=")
    assert values[position - 1] == "--data-urlencode"


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def _capture_curl_args(tmp_path: Path, fail: bool = True) -> Path:
    fake_curl = tmp_path / "bin" / "curl"
    fake_curl.parent.mkdir(exist_ok=True)
    exit_line = "exit 1" if fail else "exit 0"
    fake_curl.write_text(
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CURL_ARGS"\n{exit_line}\n'
    )
    fake_curl.chmod(0o755)
    return fake_curl


def test_the_full_tier_hook_passes_the_resolved_workspace_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    fake_curl = _capture_curl_args(tmp_path)
    args = tmp_path / "curl-args"
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "test-key",
            "ACH_MEMORY_URL": "https://memory.test",
            "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
            "CURL_ARGS": str(args),
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        [str(ROOT / "plugins/claude-code/scripts/session-start.sh")],
        capture_output=True, text=True, check=False, env=environment, cwd=repo,
    )

    values = args.read_text().splitlines()
    workspace_value = next(v for v in values if v.startswith("workspace_id="))
    position = values.index(workspace_value)
    assert result.returncode == 0
    assert re.fullmatch(r"workspace_id=ws_[0-9a-f]{32}", workspace_value)
    assert values[position - 1] == "--data-urlencode"


def test_the_full_tier_hook_omits_workspace_id_outside_a_worktree(tmp_path: Path) -> None:
    fake_curl = _capture_curl_args(tmp_path)
    args = tmp_path / "curl-args"
    outside = tmp_path / "outside"
    outside.mkdir()
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "test-key",
            "ACH_MEMORY_URL": "https://memory.test",
            "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
            "CURL_ARGS": str(args),
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        [str(ROOT / "plugins/claude-code/scripts/session-start.sh")],
        capture_output=True, text=True, check=False, env=environment, cwd=outside,
    )

    assert result.returncode == 0
    assert "workspace_id=" not in args.read_text()


def test_repeated_resolution_of_one_worktree_is_a_stable_workspace_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    fake_curl = _capture_curl_args(tmp_path)
    args = tmp_path / "curl-args"
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "test-key",
            "ACH_MEMORY_URL": "https://memory.test",
            "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
            "CURL_ARGS": str(args),
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )
    script = [str(ROOT / "plugins/claude-code/scripts/session-start.sh")]

    subprocess.run(script, capture_output=True, text=True, check=False, env=environment, cwd=repo)
    first = next(v for v in args.read_text().splitlines() if v.startswith("workspace_id="))
    subprocess.run(script, capture_output=True, text=True, check=False, env=environment, cwd=repo)
    second = next(v for v in args.read_text().splitlines() if v.startswith("workspace_id="))

    assert first == second


def test_the_full_tier_cache_differs_across_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "feature"], cwd=repo, check=True
    )
    fake_curl = tmp_path / "bin" / "curl"
    fake_curl.parent.mkdir()
    fake_curl.write_text(
        """#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    printf '%s\\n' '-- ach-memory brief rev 1 / protocol 1 / no project --' > "$2"
    exit 0
  fi
  shift
done
exit 1
"""
    )
    fake_curl.chmod(0o755)
    cache_dir = tmp_path / "cache"
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "test-key",
            "ACH_MEMORY_URL": "https://memory.test",
            "ACH_MEMORY_CACHE_DIR": str(cache_dir),
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )
    script = [str(ROOT / "plugins/claude-code/scripts/session-start.sh")]

    root_result = subprocess.run(
        script, capture_output=True, text=True, check=False, env=environment, cwd=repo
    )
    worktree_result = subprocess.run(
        script, capture_output=True, text=True, check=False, env=environment, cwd=worktree
    )

    assert root_result.returncode == 0
    assert worktree_result.returncode == 0
    assert len(list(cache_dir.glob("full-*.txt"))) == 2


def test_the_full_tier_hook_fails_open_without_home_or_xdg_cache(tmp_path: Path) -> None:
    """SessionStart must still exit zero in a minimal inherited environment."""
    fake_curl = tmp_path / "bin" / "curl"
    fake_curl.parent.mkdir()
    fake_curl.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_curl.chmod(0o755)
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "test-key",
            "ACH_MEMORY_URL": "https://memory.test",
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )
    environment.pop("HOME", None)
    environment.pop("XDG_CACHE_HOME", None)

    result = subprocess.run(
        [str(ROOT / "plugins/claude-code/scripts/session-start.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0


def test_the_full_cache_exposes_its_revision_and_age(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    printf '%s\\n' '-- ach-memory brief rev 17 / protocol 2 / cache-age 0000000000s / project acme-api --' > "$2"
    exit 0
  fi
  shift
done
exit 1
"""
    )
    curl.chmod(0o755)
    date = fake_bin / "date"
    date.write_text("#!/usr/bin/env bash\ncat \"$FAKE_EPOCH\"\n")
    date.chmod(0o755)
    epoch = tmp_path / "epoch"
    epoch.write_text("100")
    environment = _hook_env()
    environment.update({
        "ACH_MEMORY_API_KEY": "test-key", "ACH_MEMORY_URL": "https://memory.test",
        "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"), "FAKE_EPOCH": str(epoch),
        "PATH": f"{fake_bin}:{environment['PATH']}",
    })
    script = [str(ROOT / "plugins/claude-code/scripts/session-start.sh")]
    first = subprocess.run(script, capture_output=True, text=True, check=False, env=environment)
    assert first.returncode == 0
    curl.write_text("#!/usr/bin/env bash\nexit 1\n")
    curl.chmod(0o755)
    epoch.write_text("165")
    second = subprocess.run(script, capture_output=True, text=True, check=False, env=environment)
    assert second.returncode == 0
    assert "brief rev 17" in second.stdout
    assert "cache-age 0000000065s" in second.stdout


def test_no_full_context_says_the_session_has_only_the_mcp_index(tmp_path: Path) -> None:
    fake_curl = tmp_path / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_curl.chmod(0o755)
    environment = _hook_env()
    environment.update({
        "ACH_MEMORY_API_KEY": "test-key", "ACH_MEMORY_URL": "https://memory.test",
        "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
        "PATH": f"{tmp_path}:{environment['PATH']}",
    })
    result = subprocess.run(
        [str(ROOT / "plugins/claude-code/scripts/session-start.sh")],
        capture_output=True, text=True, check=False, env=environment,
    )
    assert "full tier unavailable" in result.stdout.lower()
    assert "only the mcp index" in result.stdout.lower()
    assert "memory is absent" not in result.stdout.lower()
    assert result.returncode == 0


def test_the_full_tier_cache_never_crosses_api_key_identities(tmp_path: Path) -> None:
    """A cache is user memory; one local account may deliberately switch keys."""
    fake_curl = tmp_path / "bin" / "curl"
    fake_curl.parent.mkdir()
    fake_curl.write_text(
        """#!/usr/bin/env bash
while [ \"$#\" -gt 0 ]; do
  if [ \"$1\" = \"-o\" ]; then
    printf '%s\\n' 'ALICE FULL BRIEF' > \"$2\"
    exit 0
  fi
  shift
done
exit 1
"""
    )
    fake_curl.chmod(0o755)
    cache = tmp_path / "cache"
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "key-for-alice",
            "ACH_MEMORY_URL": "https://memory.test",
            "ACH_MEMORY_CACHE_DIR": str(cache),
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )
    script = [str(ROOT / "plugins/claude-code/scripts/session-start.sh")]

    assert subprocess.run(script, capture_output=True, text=True, check=False, env=environment).returncode == 0

    fake_curl.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_curl.chmod(0o755)
    environment["ACH_MEMORY_API_KEY"] = "key-for-bob"
    result = subprocess.run(script, capture_output=True, text=True, check=False, env=environment)

    assert result.returncode == 0
    assert "ALICE FULL BRIEF" not in result.stdout


@pytest.mark.parametrize("host", NATIVE)
def test_subagent_hook_emits_the_envelope_that_event_requires(host: str) -> None:
    """SubagentStart is the only event that takes JSON rather than raw stdout.

    The envelope ships pre-built rather than being assembled at run time: doing
    it live would mean a jq or node dependency for one string escape. The
    duplication is the price, and this assertion is what keeps the two copies
    honest.
    """
    result = _script(host, "subagent-start.sh")

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"] == {
        "hookEventName": "SubagentStart",
        "additionalContext": ACTIVATION,
    }


@pytest.mark.parametrize("host", NATIVE)
def test_hook_scripts_survive_a_missing_text_file(host: str, tmp_path: Path) -> None:
    """A hook that errors can block or derail a turn, so every failure mode here
    has to degrade to silence rather than to an error."""
    import shutil

    staged = tmp_path / host
    shutil.copytree(ROOT / "plugins" / host, staged)
    for text in ("activation.txt", "activation.subagent.json"):
        (staged / text).unlink()

    for name in ("session-start.sh", "subagent-start.sh"):
        result = subprocess.run(
            [str(staged / "scripts" / name)], input="", capture_output=True, text=True,
            check=False, env=_hook_env(),
        )

        assert result.returncode == 0, name
        assert result.stdout == ""


@pytest.mark.parametrize("host", NATIVE + ADAPTED)
def test_activation_policy_is_identical_everywhere_and_carries_no_secret(host: str) -> None:
    """Four hosts each hold a copy; they must not drift into four policies.

    This one file is the whole delivery mechanism, which is why the end-of-
    session retain instruction lives in it rather than in a Stop hook. engram
    does the same: one text reaching claude and codex through SessionStart,
    opencode through its system transform and pi through before_agent_start --
    four injection points, one policy, no hook that can block a turn.
    """
    root = ROOT / "plugins" / host
    skill = (root / "skills" / "ach-memory" / "SKILL.md").read_text()

    assert (root / "activation.txt").read_text().strip() == ACTIVATION
    assert "automatic recall" not in skill.lower()
    assert "automatic retain" not in skill.lower()
    # The skill is the only always-advertised surface that can name lazy-loaded
    # tools, so an agent that never loads them still learns they exist. Two of
    # fifteen used to be named.
    for tool in ("retain", "sync_retain", "recall", "reflect", "list_memories", "get_memory",
                 "correct", "forget", "restore", "delete_document", "list_documents",
                 "get_document", "get_operation", "list_operations", "cancel_operation"):
        assert f"`{tool}`" in skill, f"{host}: {tool} unlisted"
    # Paid on every invocation, so it must not restate what each tool's own
    # description and annotations already make unmissable at the point of use.
    assert "irreversible" not in skill.lower()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            assert not SECRETS.search(path.read_text(errors="ignore")), path


@pytest.mark.parametrize("host", ADAPTED)
def test_adapted_hosts_ship_the_adapter_beside_the_text_it_reads(host: str) -> None:
    """opencode and pi have no marketplace, so the installer copies these two
    files itself -- see TODO.md for replacing that with their own plugin
    systems. The adapter resolves activation.txt relative to itself, so the
    pair has to stay together."""
    root = ROOT / "plugins" / host

    assert (root / f"{host}.js").is_file()
    assert (root / "activation.txt").is_file()


def test_opencode_adapter_injects_the_adjacent_activation_once(tmp_path: Path) -> None:
    """Breaks if the OpenCode hook is missing, duplicates activation, or registers extra hooks."""
    script = tmp_path / "ach-memory.js"
    script.write_text((ROOT / "plugins" / "opencode" / "opencode.js").read_text())
    (tmp_path / "ach-memory").mkdir()
    (tmp_path / "ach-memory" / "activation.txt").write_text(ACTIVATION)

    result = subprocess.run(
        ["node", "-e", """
  const plugin = require(process.argv[1]);
  const output = { system: [] };
  (async () => {
    const hooks = await plugin({});
    await hooks["experimental.chat.system.transform"]({}, output);
    await hooks["experimental.chat.system.transform"]({}, output);
    await hooks["experimental.chat.system.transform"](null, null);
    process.stdout.write(JSON.stringify({ keys: Object.keys(hooks), system: output.system }));
  })();
""", str(script)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["keys"] == ["experimental.chat.system.transform"]
    assert payload["system"] == [ACTIVATION]


def test_pi_adapter_injects_the_adjacent_activation_once(tmp_path: Path) -> None:
    script = tmp_path / "ach-memory.js"
    script.write_text((ROOT / "plugins" / "pi" / "pi.js").read_text())
    (tmp_path / "ach-memory").mkdir()
    (tmp_path / "ach-memory" / "activation.txt").write_text(ACTIVATION)

    result = subprocess.run(
        ["node", "-e", """
  const plugin = require(process.argv[1]);
  const handlers = {};
  plugin({ on: (event, fn) => { handlers[event] = fn; } });
  (async () => {
    const first = await handlers["before_agent_start"]({ systemPrompt: "base" });
    const again = await handlers["before_agent_start"]({ systemPrompt: first.systemPrompt });
    const bad = await handlers["before_agent_start"]({});
    process.stdout.write(JSON.stringify({ keys: Object.keys(handlers), first: first.systemPrompt,
                                          again: again === undefined, bad: bad === undefined }));
  })();
""", str(script)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["keys"] == ["before_agent_start"]
    assert payload["first"] == f"base\n\n{ACTIVATION}"
    assert payload["again"] and payload["bad"]


@pytest.mark.parametrize("host", ADAPTED)
def test_adapters_fail_open_without_activation(host: str, tmp_path: Path) -> None:
    script = tmp_path / "ach-memory.js"
    script.write_text((ROOT / "plugins" / host / f"{host}.js").read_text())

    result = subprocess.run(
        ["node", "-e", """
  const plugin = require(process.argv[1]);
  (async () => {
    const value = await plugin({ on: () => {} });
    process.stdout.write(JSON.stringify({ empty: value === undefined || Object.keys(value).length === 0 }));
  })();
""", str(script)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["empty"]


@pytest.mark.parametrize("host", NATIVE + ADAPTED)
def test_activation_carries_the_brief_consumer_contract(host: str) -> None:
    """The host policy tells agents how to interpret dynamic brief content."""
    text = (ROOT / "plugins" / host / "activation.txt").read_text().lower()
    assert "earn its place" in text
    assert "working state" in text
    assert "superseded is not current" in text
    assert "no retrieval narration" in text


@pytest.mark.parametrize("host", NATIVE + ADAPTED)
def test_activation_displaces_the_hosts_own_memory_store(host: str) -> None:
    """Names the competitor, because the contrast that was there was the wrong one.

    Claude Code ships its own file-based memory: a per-project directory plus a
    MEMORY.md index, documented in the system prompt with a schema and
    when-to-write rules, and it owns the word "memory". It is a built-in, not a
    user setting, so every host session has it.

    Measured: a fresh session with the MCP server connected, the skill
    available and this activation delivered was told to remember something and
    wrote it to that directory instead, never calling an ach-memory tool. The
    text said only "prefer it over grepping files or transcripts" -- the agent
    did not grep, so nothing in it applied.

    Measured again 2026-08-26 with the naming clause in place, this time in a
    long interactive session doing real work: given process feedback worth
    keeping, the agent announced it was saving it, wrote a file under the
    host's memory directory and added a pointer line to MEMORY.md. No
    ach-memory call. Naming the competitor was not enough because every clause
    was still about reading -- "system of record", "use it instead of",
    "prefer it over grepping" -- while the host's own instruction owns the
    write moment with a procedure attached. So the write side gets its own
    sentence, naming the trigger, the tool, and what happens if it goes to the
    file store instead.
    """
    text = (ROOT / "plugins" / host / "activation.txt").read_text().lower()
    assert "memory.md" in text, "activation must name the host store it replaces"
    assert "invisible here" in text
    assert "worth remembering" in text, "activation must name the write moment, not just the read"
    assert "`retain`" in text, "the write moment must name the tool that replaces the file write"


@pytest.mark.parametrize("host", NATIVE + ADAPTED)
def test_the_skill_carries_the_policy_for_hosts_whose_hooks_never_run(host: str) -> None:
    """The activation policy has a second home, because codex has no first one.

    Measured against codex-cli 0.149.1: the plugin's SessionStart hook does not
    execute under any configuration tried -- trusted and untrusted projects,
    hooks explicitly trusted, three path spellings including absolute,
    with and without matchers, interactive and headless, both the installed
    copy and the marketplace snapshot. See TODO.md.

    What codex does load is skills, including in untrusted projects, where its
    own message is "hooks and exec policies are disabled ... but skills still
    load". superpowers relies on exactly that: its codex plugin declares
    `"hooks": {}` and drives everything from one skill description. So the
    displacement policy lives in the skill body too, and the description says
    to read it early -- otherwise codex gets the tools and never the policy,
    which is how it ended up writing to the host's own store instead.
    """
    text = (ROOT / "plugins" / host / "skills" / "ach-memory" / "SKILL.md").read_text().lower()
    assert "instead of the host's own file-based memory directory and memory.md" in text
    assert "at the start of every conversation" in text
    assert "worth remembering" in text, "the skill copy must carry the write moment too"
    assert "`retain`" in text


@pytest.mark.parametrize("host", NATIVE + ADAPTED)
def test_the_skill_description_carries_the_policy_and_not_just_a_pointer(host: str) -> None:
    """The description is the only text that arrives without being read.

    A skill body is loaded when the host decides the description matches. The
    description itself is injected into every session, in the same block as
    CLAUDE.md, whether or not the body is ever opened -- so it is the cheapest
    channel there is and the only one that survives a host that never runs our
    hooks and never opens the skill.

    engram uses it that way: its entire memory protocol description is
    "ALWAYS ACTIVE -- Persistent memory protocol. You MUST save decisions,
    conventions, bugs, and discoveries to engram proactively. Do NOT wait for
    the user to ask." No CLAUDE.md, no hook -- one imperative sentence in
    frontmatter. Ours described a capability and pointed at the body instead,
    which meant the displacement policy was one indirection away at the moment
    it had to win.

    The proactive mandate is scoped to retain deliberately. Recall stays
    demand-driven: "a memory call needs a task that depends on it, not merely
    a session starting" is about reading, and the two must not blur.
    """
    fm = (ROOT / "plugins" / host / "skills" / "ach-memory" / "SKILL.md").read_text().split("---")[1]
    description = yaml.safe_load(fm)["description"]
    assert len(description) <= 1024, "hosts truncate long descriptions"
    text = description.lower()
    assert "always active" in text, "the description must not read as an optional capability"
    assert "proactively" in text and "never wait to be asked" in text
    assert "memory.md" in text, "the displacement must survive a body that is never opened"
    assert "`retain`" in text
    assert "read this skill" in text, "the description must still route to the body"


@pytest.mark.parametrize("host", NATIVE + ADAPTED)
def test_the_skill_requires_english_at_write_time(host: str) -> None:
    """Retrieval reranks with an English-only cross-encoder.

    Hindsight's default is `cross-encoder/ms-marco-MiniLM-L-6-v2`, trained on
    English MS MARCO. Measured against the deployed 0.9.1: one Spanish fact
    scored reranker 0.988 for the Spanish question and 0.000098 for the English
    translation of that same question -- a 10,000x collapse -- while its
    embedding score barely moved (0.825 -> 0.647) and still ranked it first.
    So the embedding is multilingual and the thing that decides the answer is
    not.

    Storing in English is the mitigation until the reranker is swapped for a
    multilingual one (TODO.md). The rule also lives on the `retain` and
    `sync_retain` tool descriptions, which is the channel an agent cannot skip
    on its way to writing; this is the one that explains why.
    """
    text = (ROOT / "plugins" / host / "skills" / "ach-memory" / "SKILL.md").read_text().lower()
    assert "write every memory in english" in text


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            "https://x-access-token:ghp_CANARYTOKEN123@github.com/acme/api.git",
            "https://github.com/acme/api.git",
        ),
        (
            "https://ghp_CANARYTOKEN123@github.com/acme/api.git",
            "https://github.com/acme/api.git",
        ),
        ("ssh://git@github.com:22/acme/api.git", "ssh://github.com:22/acme/api.git"),
        # No credential to strip: these must pass through untouched.
        ("https://github.com/acme/api.git", "https://github.com/acme/api.git"),
    ],
)
def test_the_session_start_hook_strips_userinfo_from_the_git_locator(
    tmp_path: Path, origin: str, expected: str
) -> None:
    """SPEC Phase 3 review finding 2, on the highest-frequency path there is.

    The locator becomes a URL QUERY PARAMETER, so an unstripped credential
    lands in the service's access logs once per session start.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True)
    fake_curl = _capture_curl_args(tmp_path)
    args = tmp_path / "curl-args"
    environment = _hook_env()
    environment.update(
        {
            "ACH_MEMORY_API_KEY": "test-key",
            "ACH_MEMORY_URL": "https://memory.test",
            "ACH_MEMORY_CACHE_DIR": str(tmp_path / "cache"),
            "CURL_ARGS": str(args),
            "PATH": f"{fake_curl.parent}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        [str(ROOT / "plugins/claude-code/scripts/session-start.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=repo,
    )

    assert result.returncode == 0
    values = args.read_text()
    assert f"git_locator={expected}" in values
    assert "CANARYTOKEN123" not in values
    assert "x-access-token" not in values
