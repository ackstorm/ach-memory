"""Contract tests for the committed, per-host plugin trees.

The bundle used to be rendered into ~/.local/share at install time so a
per-install URL could be baked into .mcp.json. It is now static and committed,
the way engram and codemem ship theirs, and the repository root is the
marketplace. These tests exist to keep it that way: anything here that starts
depending on install-time state has regressed the whole point.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
NATIVE = ("claude-code", "codex")
ADAPTED = ("opencode", "pi")
ACTIVATION = (
    "ach-memory holds durable user and project context across sessions and is the system of record "
    "for prior decisions -- use it instead of the file-based memory directory and MEMORY.md, and "
    "prefer it over grepping files or transcripts. Anything worth remembering goes through `retain`,"
    " never into that directory or index, which ach-memory cannot see. Load the ach-memory skill "
    "before your first memory call. Recall before work that depends on such context. Retain it once "
    "established, including decisions made only in conversation, and again before a session ends. "
    "Never store secrets. A memory call needs a task that depends on it, not merely a session "
    "starting."
)
SECRETS = re.compile(r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|mem_[A-Za-z0-9]{20,}")


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _script(host: str, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "plugins" / host / "scripts" / name)],
        input="", capture_output=True, text=True, check=False,
    )


def test_claude_mcp_config_is_static_and_takes_both_values_from_the_environment() -> None:
    """The one test that protects the architecture.

    A literal URL means someone reintroduced per-install rendering; a literal
    credential means the bundle stopped being safe to commit. Neither may
    appear, and the URL carries a localhost default so a fresh checkout works
    against docker compose with nothing exported.
    """
    server = _json(ROOT / "plugins" / "claude-code" / ".mcp.json")["mcpServers"]["ach-memory"]

    assert server["type"] == "http"
    assert server["url"] == "${ACH_MEMORY_URL:-http://localhost:8000}/mcp/"
    assert server["headers"] == {"Authorization": "Bearer ${ACH_MEMORY_API_KEY}"}


def test_codex_plugin_ships_no_mcp_config_because_it_cannot_express_one() -> None:
    """Codex registers the server itself; the plugin carries hooks and the skill.

    Two measured facts force this, and both are silent failures:

    - Codex does not interpolate ${VAR} in `url`. It hands the literal string
      to a URL parser, which rejects it with "relative URL without a base" and
      the server never starts.
    - Codex ignores a `headers` block. `codex mcp get` reported http_headers
      and env_http_headers empty, so calls would go out unauthenticated.

    A hardcoded localhost URL would start cleanly and point every remote user
    at nothing, which is worse than shipping no config at all. README documents
    the one-line `codex mcp add ... --bearer-token-env-var` instead.
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
    """Pinned, and both absentees are deliberate.

    UserPromptSubmit was paid on every message for a reminder actionable on
    few of them. Stop was tried in its place and is worse: Claude Code treats
    ANY output from a Stop hook as feedback that blocks the turn from ending,
    so the nudge re-fired until the block cap -- measured at nine extra model
    turns for one response, with the agent answering "nothing to retain" each
    time. `stop_hook_active` would bound that to one extra turn per response,
    which is still a whole turn to say nothing.

    The retain guidance lives in activation.txt, where it costs one injection
    per session.
    """
    hooks = _json(ROOT / "plugins" / host / "hooks" / "hooks.json")["hooks"]

    assert set(hooks) == {"SessionStart", "SubagentStart"}
    for event, registrations in hooks.items():
        hook = registrations[0]["hooks"][0]
        assert hook["type"] == "command"
        name = {"SessionStart": "session-start.sh", "SubagentStart": "subagent-start.sh"}[event]
        # Each host expands its OWN variable. `${PLUGIN_ROOT}` is the Agent
        # Plugins spec name -- it is what the codex binary carries, and what
        # engram's codex plugin uses, while engram ships `${CLAUDE_PLUGIN_ROOT}`
        # in its claude-code copy of the same file. Claude's spelling under
        # codex leaves the path unexpanded and codex reports nothing at all.
        root = "CLAUDE_PLUGIN_ROOT" if host == "claude-code" else "PLUGIN_ROOT"
        assert hook["command"] == f'"${{{root}}}/scripts/{name}"'


@pytest.mark.parametrize("host", NATIVE)
def test_every_hook_script_is_executable_and_needs_no_runtime(host: str) -> None:
    """Breaks if a script reaches for node, jq, python, or curl.

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
        assert not re.search(r"\b(node|jq|python3?|curl|npx)\b", code), script.name


@pytest.mark.parametrize("host", NATIVE)
def test_session_start_emits_its_text_as_plain_stdout(host: str) -> None:
    """SessionStart is one of the three events whose plain stdout becomes
    context Claude can act on, so it needs no envelope."""
    result = _script(host, "session-start.sh")

    assert result.returncode == 0
    assert result.stdout.strip() == ACTIVATION
    assert result.stderr == ""


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
            [str(staged / "scripts" / name)], input="", capture_output=True, text=True, check=False
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
def test_activation_routes_the_agent_to_the_skill(host: str) -> None:
    """The clause that makes the skill reachable, pinned against a trim.

    The skill's own frontmatter says to read it before the first memory call,
    but a description is matched for relevance, not executed -- obeying it
    requires already reading it. Nothing else in the loaded path points there:
    activation.txt is the only text guaranteed into every session, and without
    this sentence the sole route to the skill is the agent's own judgement.

    Measured: an agent with this file loaded and the MCP server connected ran
    recall and list_memories without ever opening the skill, having found the
    tool names through the host's own tool search instead. It is ~12 tokens per
    injection and it is the only delivery mechanism there is.
    """
    text = (ROOT / "plugins" / host / "activation.txt").read_text().lower()
    assert "skill" in text, "activation must name the skill or nothing loads it"
    assert "before your first memory call" in text


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
    assert "instead of" in text
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
    assert "read at the start of any conversation" in text
    assert "worth remembering" in text, "the skill copy must carry the write moment too"
    assert "`retain`" in text


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
