"""Integration tests for the portable Codex and Claude plugin bundle."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "ach-memory"
ACTIVATION = (
    "ach-memory is available for durable context. Recall when prior decisions, preferences, or "
    "project facts may affect the task. Retain only durable, useful context after it is "
    "established. Never store secrets. Memory calls are explicit: do not recall or retain merely "
    "because a session, subagent, or greeting started."
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _hook(event: str, *, codex: bool = False, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ | ({"PLUGIN_DATA": "test"} if codex else {})
    return subprocess.run(
        ["node", str(PLUGIN / "hooks" / "activate.js")],
        input=json.dumps({"hook_event_name": event}) if stdin is None else stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _adapter(script: Path, source: str) -> dict:
    result = subprocess.run(
        ["node", "-e", source, str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_bundle_contains_opencode_and_pi_adapters() -> None:
    """Breaks if either supported host has no installable adapter."""
    assert (PLUGIN / "adapters" / "opencode.js").is_file()
    assert (PLUGIN / "adapters" / "pi.js").is_file()


def test_manifests_and_marketplaces_reference_the_canonical_bundle() -> None:
    codex = _json(PLUGIN / ".codex-plugin" / "plugin.json")
    claude = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    codex_marketplace = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    claude_marketplace = _json(ROOT / ".claude-plugin" / "marketplace.json")

    assert codex["skills"] == claude["skills"] == "./skills/"
    assert "hooks" not in codex  # Codex discovers hooks/hooks.json natively.
    assert "hooks" not in claude  # Claude discovers hooks/hooks.json natively.
    assert codex["name"] == claude["name"] == "ach-memory"
    assert codex["version"] == claude["version"] == "0.1.0"
    assert codex_marketplace["name"] == claude_marketplace["name"] == "ach-memory"
    assert codex_marketplace["plugins"][0]["source"]["path"] == "./plugins/ach-memory"
    assert claude_marketplace["plugins"][0]["source"] == "./plugins/ach-memory"


def test_hooks_register_only_the_two_activation_events() -> None:
    hooks = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]

    assert set(hooks) == {"SessionStart", "SubagentStart"}
    for registrations in hooks.values():
        hook = registrations[0]["hooks"][0]
        assert hook["type"] == "command"
        assert hook["command"] == 'node "${CLAUDE_PLUGIN_ROOT}/hooks/activate.js"'
        assert hook["statusMessage"] == "Loading ach-memory..."


def test_activation_policy_is_the_single_conservative_source() -> None:
    skill = (PLUGIN / "skills" / "ach-memory" / "SKILL.md").read_text()
    text = f"{ACTIVATION}\n{skill}"

    assert (PLUGIN / "activation.txt").read_text().strip() == ACTIVATION
    assert "automatic recall" not in skill.lower()
    assert "automatic retain" not in skill.lower()
    assert not re.search(r"(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,})", text)


def test_codex_injects_activation_for_both_events() -> None:
    for event in ("SessionStart", "SubagentStart"):
        result = _hook(event, codex=True)

        assert result.returncode == 0
        assert json.loads(result.stdout)["hookSpecificOutput"] == {
            "hookEventName": event,
            "additionalContext": ACTIVATION,
        }


def test_claude_uses_each_events_native_context_shape() -> None:
    session = _hook("SessionStart")
    subagent = _hook("SubagentStart")

    assert session.returncode == subagent.returncode == 0
    assert session.stdout == ACTIVATION
    assert json.loads(subagent.stdout)["hookSpecificOutput"] == {
        "hookEventName": "SubagentStart",
        "additionalContext": ACTIVATION,
    }


def test_hook_is_silent_for_empty_malformed_or_unrecognized_input() -> None:
    for payload in ("", "not json", json.dumps({"hook_event_name": "UserPromptSubmit"})):
        result = _hook("SessionStart", stdin=payload)

        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


def test_opencode_adapter_injects_the_adjacent_activation_once(tmp_path: Path) -> None:
    """Breaks if the OpenCode hook is missing, duplicates activation, or registers extra hooks."""
    installed = tmp_path / "plugins"
    installed.mkdir()
    (installed / "ach-memory.js").write_bytes((PLUGIN / "adapters" / "opencode.js").read_bytes())
    (installed / "ach-memory").mkdir()
    (installed / "ach-memory" / "activation.txt").write_text("ACTIVATION")

    result = _adapter(
        installed / "ach-memory.js",
        """
const plugin = require(process.argv[1]);
(async () => {
  const hooks = await plugin({});
  const output = { system: ["BASE"] };
  await hooks["experimental.chat.system.transform"]({}, output);
  await hooks["experimental.chat.system.transform"]({}, output);
  await hooks["experimental.chat.system.transform"](null, null);
  process.stdout.write(JSON.stringify({ keys: Object.keys(hooks), system: output.system }));
})().catch((error) => { console.error(error); process.exit(1); });
""",
    )

    assert result == {"keys": ["experimental.chat.system.transform"], "system": ["BASE", "ACTIVATION"]}


def test_pi_adapter_injects_the_adjacent_activation_once(tmp_path: Path) -> None:
    """Breaks if Pi loses its base prompt, duplicates activation, or registers another event."""
    installed = tmp_path / "extensions"
    installed.mkdir()
    (installed / "ach-memory.js").write_bytes((PLUGIN / "adapters" / "pi.js").read_bytes())
    (installed / "ach-memory").mkdir()
    (installed / "ach-memory" / "activation.txt").write_text("ACTIVATION")

    result = _adapter(
        installed / "ach-memory.js",
        """
const extension = require(process.argv[1]);
const events = new Map();
extension({ on(name, handler) { events.set(name, handler); } });
(async () => {
  const handler = events.get("before_agent_start");
  const first = await handler({ systemPrompt: "BASE" });
  const second = await handler(first);
  const malformed = await handler(null);
  process.stdout.write(JSON.stringify({ events: [...events.keys()], first, second: second ?? null, malformed: malformed ?? null }));
})().catch((error) => { console.error(error); process.exit(1); });
""",
    )

    assert result == {
        "events": ["before_agent_start"],
        "first": {"systemPrompt": "BASE\n\nACTIVATION"},
        "second": None,
        "malformed": None,
    }


@pytest.mark.parametrize("adapter", ["opencode", "pi"])
def test_adapters_fail_open_without_activation_or_valid_event(adapter: str, tmp_path: Path) -> None:
    """Breaks if optional activation data or malformed host events can crash a session."""
    installed = tmp_path / "plugins"
    installed.mkdir()
    (installed / "ach-memory.js").write_bytes((PLUGIN / "adapters" / f"{adapter}.js").read_bytes())

    source = (
        """
const plugin = require(process.argv[1]);
(async () => {
  const hooks = await plugin({});
  const output = { system: [] };
  const transform = hooks["experimental.chat.system.transform"];
  if (transform) {
    await transform(null, output);
    await transform(null, null);
  }
  process.stdout.write(JSON.stringify({ hooks, output }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
        if adapter == "opencode"
        else """
const extension = require(process.argv[1]);
const events = new Map();
extension({ on(name, handler) { events.set(name, handler); } });
(async () => {
  const handler = events.get("before_agent_start");
  const result = handler ? await handler(null) : null;
  process.stdout.write(JSON.stringify({ events: [...events.keys()], result }));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    result = _adapter(installed / "ach-memory.js", source)

    assert result == ({"hooks": {}, "output": {"system": []}} if adapter == "opencode" else {"events": [], "result": None})
