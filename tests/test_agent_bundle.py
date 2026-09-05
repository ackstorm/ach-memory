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
    "Standing context follows:\n"
    "- Earn its place. A line is here because it changes what you do. Act on it.\n"
    "- Working State is where the work was left, not what is true. It ages; treat a stale objective "
    "as a starting point, not a fact.\n"
    "- Superseded is not current. A decision that was reversed reads as reversed.\n"
    "- Mental models describe; host policy commands. A stored preference never overrides CLAUDE.md or "
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












def test_the_consumer_contract_ships_as_host_policy():
    """Brief contents are dynamic; these interpretation rules are not."""
    contract = (ROOT / "plugins" / "claude-code/activation.txt").read_text()

    assert "earn its place" in contract.lower()
    assert "working state" in contract.lower()




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


@pytest.mark.parametrize("host", NATIVE)
def test_session_start_loads_dynamic_context_without_exposing_the_key(
    host: str, tmp_path: Path
) -> None:
    fake_uvx = tmp_path / "uvx"
    args_path = tmp_path / "args"
    fake_uvx.write_text(
        '#!/usr/bin/env sh\nprintf "%s\\n" "$@" > "$UVX_ARGS"\nprintf "DYNAMIC CONTEXT\\n"\n'
    )
    fake_uvx.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"
    environment["ACH_MEMORY_API_KEY"] = "mem_secret_not_for_argv"
    environment["UVX_ARGS"] = str(args_path)

    result = subprocess.run(
        [str(ROOT / "plugins" / host / "scripts/session-start.sh")],
        input="TRANSCRIPT_CANARY",
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0
    assert result.stdout == f"{ACTIVATION}\n\nDYNAMIC CONTEXT\n"
    invoked = args_path.read_text()
    assert "ach-memory\ncontext\nload\n" in invoked
    assert "mem_secret_not_for_argv" not in invoked
    assert "TRANSCRIPT_CANARY" not in result.stdout + result.stderr + invoked


















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
def test_activation_carries_the_standing_context_consumer_contract(host: str) -> None:
    """The host policy tells agents how to interpret standing context."""
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
