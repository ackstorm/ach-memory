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
    "ach-memory is available for durable context. Recall when prior decisions, preferences, or "
    "project facts may affect the task, and prefer it over grepping local files or transcripts "
    "for that kind of question -- it is the system of record for them. Retain only durable, "
    "useful context after it is established. Never store secrets. Do not recall or retain merely "
    "because a session, subagent, or greeting started; a memory call needs a task that depends "
    "on it."
)
PROMPT_HINT = (
    "ach-memory holds durable user and project context. If this task depends on prior decisions, "
    "preferences, or established facts, call recall before searching local files or transcripts."
)
SECRETS = re.compile(r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|mem_[A-Za-z0-9]{20,}")


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _script(host: str, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "plugins" / host / "scripts" / name)],
        input="", capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("host", NATIVE)
def test_the_mcp_config_is_static_and_takes_both_values_from_the_environment(host: str) -> None:
    """The one test that protects the architecture.

    A literal URL means someone reintroduced per-install rendering; a literal
    credential means the bundle stopped being safe to commit. Neither may
    appear, and the URL carries a localhost default so a fresh checkout works
    against docker compose with nothing exported.

    The credential is spelled differently per host and that is not cosmetic.
    Claude Code interpolates ${VAR} inside header values; Codex does not
    interpolate at all and instead takes the NAME of an env var it resolves
    itself. Handing Codex a `headers` block is not an error there -- it is
    dropped in silence, `codex mcp get` reports http_headers as empty, and
    every tool call goes out unauthenticated. Measured, after this test was
    briefly parameterized as if one shape fit both.
    """
    server = _json(ROOT / "plugins" / host / ".mcp.json")["mcpServers"]["ach-memory"]

    assert server["type"] == "http"
    assert server["url"] == "${ACH_MEMORY_URL:-http://localhost:8000}/mcp/"
    if host == "codex":
        assert server["bearer_token_env_var"] == "ACH_MEMORY_API_KEY"
        assert "headers" not in server
    else:
        assert server["headers"] == {"Authorization": "Bearer ${ACH_MEMORY_API_KEY}"}
        assert "bearer_token_env_var" not in server


def test_the_repository_root_is_the_marketplace_for_both_hosts() -> None:
    """Installing must mean `plugin marketplace add ackstorm/ach-memory`, which
    only works while the manifests live at the root and point into ./plugins."""
    claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
    codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")

    assert claude["name"] == codex["name"] == "ach-memory"
    assert claude["plugins"][0]["source"] == "./plugins/claude-code"
    assert codex["plugins"][0]["source"] == {"source": "local", "path": "./plugins/codex"}


@pytest.mark.parametrize("host", NATIVE)
def test_hooks_register_the_three_activation_events_against_their_own_script(host: str) -> None:
    hooks = _json(ROOT / "plugins" / host / "hooks" / "hooks.json")["hooks"]

    assert set(hooks) == {"SessionStart", "SubagentStart", "UserPromptSubmit"}
    for event, registrations in hooks.items():
        hook = registrations[0]["hooks"][0]
        assert hook["type"] == "command"
        name = {
            "SessionStart": "session-start.sh",
            "SubagentStart": "subagent-start.sh",
            "UserPromptSubmit": "user-prompt-submit.sh",
        }[event]
        assert hook["command"] == f'"${{CLAUDE_PLUGIN_ROOT}}/scripts/{name}"'
        # No spinner label on the per-prompt hook: it fires on every message.
        assert ("statusMessage" in hook) == (event != "UserPromptSubmit")


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
def test_session_and_prompt_hooks_emit_their_text_as_plain_stdout(host: str) -> None:
    session = _script(host, "session-start.sh")
    prompt = _script(host, "user-prompt-submit.sh")

    assert session.returncode == prompt.returncode == 0
    assert session.stdout.strip() == ACTIVATION
    assert prompt.stdout.strip() == PROMPT_HINT
    assert session.stderr == prompt.stderr == ""


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
    """A non-zero UserPromptSubmit hook blocks the user's message outright, so
    every failure mode here has to degrade to silence, never to an error."""
    import shutil

    staged = tmp_path / host
    shutil.copytree(ROOT / "plugins" / host, staged)
    for text in ("activation.txt", "prompt-hint.txt", "activation.subagent.json"):
        (staged / text).unlink()

    for name in ("session-start.sh", "user-prompt-submit.sh", "subagent-start.sh"):
        result = subprocess.run(
            [str(staged / "scripts" / name)], input="", capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, name
        assert result.stdout == ""


@pytest.mark.parametrize("host", NATIVE + ADAPTED)
def test_activation_policy_is_identical_everywhere_and_carries_no_secret(host: str) -> None:
    """Four hosts each hold a copy; they must not drift into four policies."""
    root = ROOT / "plugins" / host
    skill = (root / "skills" / "ach-memory" / "SKILL.md").read_text()

    assert (root / "activation.txt").read_text().strip() == ACTIVATION
    assert "automatic recall" not in skill.lower()
    assert "automatic retain" not in skill.lower()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            assert not SECRETS.search(path.read_text(errors="ignore")), path


def test_the_prompt_hint_stays_a_pointer_not_a_second_policy() -> None:
    """It is paid on every message, so its length is a budget, not a detail."""
    for host in NATIVE:
        assert (ROOT / "plugins" / host / "prompt-hint.txt").read_text().strip() == PROMPT_HINT
    assert len(PROMPT_HINT) < len(ACTIVATION) / 2


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
