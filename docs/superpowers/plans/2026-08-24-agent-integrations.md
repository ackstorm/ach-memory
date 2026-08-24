# Conservative Agent Integrations Implementation Plan

> **Required sub-skill:** Use `superpowers:subagent-driven-development` to execute this plan task by task. Each implementation task must also follow `superpowers:test-driven-development`; before claiming completion use `superpowers:verification-before-completion`. Work directly in the shared `main` checkout as requested—do not create a worktree.

**Goal:** Ship `ach-memory init codex|claude|opencode|pi|all` so every supported agent receives the conservative memory protocol at root-session and subagent startup, with authenticated remote MCP configuration and no automatic memory calls.

**Architecture:** Keep one installable integration bundle in `plugins/ach-memory/`: one activation text, one skill, one hook, and thin OpenCode/Pi adapters. `src/memory/cli.py` performs URL validation, an authenticated MCP `list_tools` preflight, native Codex/Claude/Pi registration, and JSON upserts for OpenCode/Pi. Hatch includes the bundle in the wheel under `memory/integrations`, so source and installed-package flows use the same assets.

**Tech stack:** Python 3.12 stdlib (`argparse`, `asyncio`, `importlib.resources`, `json`, `os`, `pathlib`, `shutil`, `subprocess`, `tempfile`, `urllib.parse`), existing MCP Python SDK, dependency-free JavaScript adapters, pytest, native agent CLIs.

**Specification:** `docs/superpowers/specs/2026-08-24-agent-integrations-design.md`

## Global constraints

- Preserve unrelated user configuration; replace only the `ach-memory` MCP/plugin entry and ach-memory-owned files.
- Never write or pass the value of `ACH_MEMORY_API_KEY` to a subprocess; only generated environment-variable references are allowed.
- Validate every target executable and the authenticated MCP connection before the first write, especially for `all`.
- Hooks and adapters only inject context. They do no network I/O, never call a memory tool, and fail open.
- Use the existing `mcp` dependency and `mcp.shared._httpx_utils.create_mcp_http_client`; do not add another package.
- Stage commits by explicit path because `README.md`, `.agents/`, and `plugins/` begin dirty in the shared checkout.

---

### Task 1: Add the CLI contract, URL derivation, and MCP preflight

**Files:**

- Create: `src/memory/cli.py`
- Modify: `pyproject.toml`
- Create: `tests/test_cli.py`

**Step 1: Write failing URL and argument tests**

Add table-driven tests for `_mcp_url()`:

```python
@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000/mcp/"),
        ("https://memory.example.com/", "https://memory.example.com/mcp/"),
        ("https://example.com/team/memory", "https://example.com/team/memory/mcp/"),
    ],
)
def test_mcp_url(base: str, expected: str) -> None:
    assert cli._mcp_url(base) == expected

@pytest.mark.parametrize(
    "base",
    ["localhost:8000", "ftp://example.com", "https://example.com/?x=1", "https://example.com/#x"],
)
def test_mcp_url_rejects_invalid_input(base: str) -> None:
    with pytest.raises(ValueError):
        cli._mcp_url(base)
```

Also assert `main(["init"])` and unknown targets return non-zero, while the parser accepts exactly `codex`, `claude`, `opencode`, `pi`, and `all`.

**Step 2: Run the focused tests and confirm red**

Run: `uv run pytest tests/test_cli.py -q`

Expected: FAIL because `memory.cli` does not exist.

**Step 3: Implement the minimum parser and URL function**

Expose the console script:

```toml
[project.scripts]
ach-memory = "memory.cli:main"
```

Implement `_mcp_url` with `urllib.parse.urlsplit`, requiring `http`/`https`, a non-empty `netloc`, and no query/fragment. Normalize only trailing slashes and append `mcp/`, preserving any path prefix. Implement `main(argv: list[str] | None = None) -> int` with stdlib `argparse` and a required `init` target.

**Step 4: Write a failing authenticated preflight test**

Monkeypatch `create_mcp_http_client`, `streamable_http_client`, and `ClientSession` with async fakes. Assert `_preflight(url, "user-secret")`:

- constructs only `Authorization: Bearer user-secret` in the in-process HTTP client;
- initializes the MCP session;
- calls `list_tools()` exactly once;
- calls no memory tool;
- rejects an empty `ACH_MEMORY_API_KEY` before opening a connection.

**Step 5: Implement the preflight with the existing MCP SDK**

Use:

```python
client = create_mcp_http_client({"Authorization": f"Bearer {api_key}"})
async with client, streamable_http_client(url, http_client=client) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
```

Require at least the public `recall` and `retain` tools, but do not invoke them. Convert transport/auth/tool-surface failures to a concise `CLIError` printed without the key.

**Step 6: Verify and commit**

Run: `uv run pytest tests/test_cli.py -q`

Run: `uv run ruff check src/memory/cli.py tests/test_cli.py`

Commit exact paths:

```bash
git add src/memory/cli.py tests/test_cli.py pyproject.toml uv.lock
git commit -m "feat: add ach-memory init preflight"
```

---

### Task 2: Build the canonical bundle and Codex/Claude hooks

**Required skills:** Before editing, read and follow `plugin-creator`, `skill-creator`, and `superpowers:writing-skills` in addition to TDD.

**Files:**

- Modify: `plugins/ach-memory/.codex-plugin/plugin.json`
- Create: `plugins/ach-memory/.claude-plugin/plugin.json`
- Create: `plugins/ach-memory/.claude-plugin/marketplace.json`
- Modify: `.agents/plugins/marketplace.json`
- Modify: `plugins/ach-memory/.mcp.json`
- Create: `plugins/ach-memory/activation.txt`
- Create: `plugins/ach-memory/hooks/hooks.json`
- Create: `plugins/ach-memory/hooks/activate.js`
- Modify: `plugins/ach-memory/skills/ach-memory/SKILL.md`
- Modify: `pyproject.toml`
- Create: `tests/test_agent_bundle.py`

**Step 1: Write failing bundle and hook tests**

Tests must assert:

- both plugin manifests point to `./hooks/hooks.json` and `./skills/`;
- hooks register only `SessionStart` and `SubagentStart`, both with `statusMessage: "Loading ach-memory..."`;
- both events inject the exact text from `activation.txt`;
- simulated Codex (`PLUGIN_DATA` set) receives `hookSpecificOutput.additionalContext`;
- simulated Claude `SessionStart` receives plain context and `SubagentStart` receives the native JSON context shape;
- malformed or empty stdin exits zero and emits nothing;
- neither the activation text nor skill instructs automatic recall/retain or contains a credential.

Run: `uv run pytest tests/test_agent_bundle.py -q`

Expected: FAIL because the activation and hooks are absent.

**Step 2: Add one compact activation protocol**

Use this single source in `activation.txt`:

```text
ach-memory is available for durable context. Recall when prior decisions, preferences, or project facts may affect the task. Retain only durable, useful context after it is established. Never store secrets. Memory calls are explicit: do not recall or retain merely because a session, subagent, or greeting started.
```

Keep the existing skill as the detailed tool protocol; tighten only wording needed to match this policy.

**Step 3: Add one dependency-free hook adapter**

`activate.js` reads the hook payload from stdin, accepts only `SessionStart`/`SubagentStart`, reads `../activation.txt`, and writes the host-native shape. Wrap the body in `try/catch { process.exitCode = 0; }`; do not log on failure. `hooks.json` invokes it with Node and `${CLAUDE_PLUGIN_ROOT}` exactly as supported by both current hosts.

**Step 4: Complete both native plugin manifests and marketplaces**

Keep the Codex interface metadata minimal. Add the Claude manifest and marketplace manifest using the same plugin name/version. Make both marketplace names `ach-memory` and both source paths `./plugins/ach-memory`. The source `.mcp.json` remains a valid default-local Codex definition; the installer will render host-specific copies for remote URLs.

**Step 5: Package the bundle without duplicating it**

Add Hatch force-inclusion:

```toml
[tool.hatch.build.targets.wheel.force-include]
"plugins/ach-memory" = "memory/integrations/plugin"
```

Use `importlib.resources.files("memory").joinpath("integrations/plugin")` from the CLI in later tasks.

**Step 6: Validate and commit**

Run: `uv run pytest tests/test_agent_bundle.py -q`

Run the official Codex plugin and skill validators from their installed skill directories, then run: `claude plugin validate plugins/ach-memory`

Run: `uv build && unzip -l dist/ach_memory-*.whl | rg 'memory/integrations/plugin/(activation.txt|hooks/activate.js|skills/ach-memory/SKILL.md)'`

Commit exact paths:

```bash
git add .agents/plugins/marketplace.json plugins/ach-memory pyproject.toml uv.lock tests/test_agent_bundle.py
git commit -m "feat: add conservative memory activation bundle"
```

---

### Task 3: Install Codex and Claude idempotently

**Files:**

- Modify: `src/memory/cli.py`
- Modify: `tests/test_cli.py`

**Step 1: Write failing native-install tests**

With a temporary `HOME`/`XDG_DATA_HOME` and a fake subprocess runner, assert each target:

- preflights its executable with `shutil.which` before any write;
- copies an owned marketplace to `$XDG_DATA_HOME/ach-memory/<agent>-marketplace` atomically;
- renders the requested MCP URL but only the literal name `ACH_MEMORY_API_KEY`;
- adds the marketplace only when absent;
- installs or refreshes only `ach-memory@ach-memory`;
- never includes the API key in subprocess argv or generated files;
- produces byte-equivalent output on the second run.

For Codex render:

```json
{"mcpServers":{"ach-memory":{"type":"http","url":"https://host/prefix/mcp/","bearer_token_env_var":"ACH_MEMORY_API_KEY"}}}
```

For Claude render:

```json
{"mcpServers":{"ach-memory":{"type":"http","url":"https://host/prefix/mcp/","headers":{"Authorization":"Bearer ${ACH_MEMORY_API_KEY}"}}}}
```

**Step 2: Run focused tests and confirm red**

Run: `uv run pytest tests/test_cli.py -q`

**Step 3: Implement one shared marketplace renderer**

Use `shutil.copytree` into a sibling temporary directory, render only `.mcp.json`, then `os.replace` the owned destination. Never mutate the packaged source bundle. Keep agent differences to a two-branch MCP payload and their native command names.

Native flow:

```text
codex plugin marketplace list --json
codex plugin marketplace add <owned-marketplace> --json       # only if absent
codex plugin remove ach-memory@ach-memory                      # only if already installed
codex plugin add ach-memory@ach-memory --json

claude plugin marketplace list --json
claude plugin marketplace add --scope user <owned-marketplace> # only if absent
claude plugin update ach-memory@ach-memory                     # if installed
claude plugin install -y --scope user ach-memory@ach-memory    # otherwise
```

Check actual JSON list fields defensively, but fail on unsupported shapes instead of guessing.

**Step 4: Verify and commit**

Run: `uv run pytest tests/test_cli.py -q`

Run: `uv run ruff check src/memory/cli.py tests/test_cli.py`

Commit:

```bash
git add src/memory/cli.py tests/test_cli.py
git commit -m "feat: install Codex and Claude memory plugins"
```

---

### Task 4: Add OpenCode and Pi adapters and configuration upserts

**Files:**

- Create: `plugins/ach-memory/adapters/opencode.js`
- Create: `plugins/ach-memory/adapters/pi.js`
- Modify: `src/memory/cli.py`
- Modify: `tests/test_agent_bundle.py`
- Modify: `tests/test_cli.py`

**Step 1: Write failing adapter tests**

Use Node subprocess tests to load each adapter with a temporary installed layout:

- OpenCode appends the activation text once through `experimental.chat.system.transform`;
- Pi appends it once through `before_agent_start` and preserves the existing system prompt;
- missing activation files and malformed event objects fail open;
- neither adapter makes an HTTP request or registers a memory tool.

**Step 2: Implement the two minimal adapters**

OpenCode exports a plugin factory returning only:

```javascript
{
  "experimental.chat.system.transform": async (_input, output) => {
    if (!output.system.includes(activation)) output.system.push(activation)
  }
}
```

Pi registers only `before_agent_start` and returns `{ systemPrompt: `${event.systemPrompt}\n\n${activation}` }` when the text is not already present. Both read the installer-copied adjacent `ach-memory/activation.txt`.

**Step 3: Write failing installer/upsert tests**

With temporary config homes, assert:

- OpenCode preserves unrelated `opencode.json` keys and MCP servers, adds only `mcp.ach-memory`, and copies the plugin plus skill;
- Pi preserves unrelated `mcp.json` servers, adds only `mcpServers.ach-memory`, copies extension plus skill, and runs `pi install npm:pi-mcp-adapter` without secrets;
- invalid JSON causes no file replacement;
- second runs refresh URL/owned assets and leave unrelated content unchanged.

Expected OpenCode entry:

```json
{"type":"remote","url":"https://host/prefix/mcp/","headers":{"Authorization":"Bearer {env:ACH_MEMORY_API_KEY}"}}
```

Expected Pi entry:

```json
{"url":"https://host/prefix/mcp/","auth":"bearer","bearerTokenEnv":"ACH_MEMORY_API_KEY","lifecycle":"lazy","directTools":false}
```

**Step 4: Implement atomic JSON and owned-file writes**

Use one `_write_json_atomic(path, value)` helper: create the parent, write JSON to `NamedTemporaryFile(dir=path.parent, delete=False)`, `flush`, `os.fsync`, then `os.replace`. Parse an existing file before touching any destination. Copy only:

```text
OpenCode: ~/.config/opencode/plugins/ach-memory.js
          ~/.config/opencode/plugins/ach-memory/activation.txt
          ~/.config/opencode/skills/ach-memory/SKILL.md
Pi:       $PI_CODING_AGENT_DIR/extensions/ach-memory.js
          $PI_CODING_AGENT_DIR/extensions/ach-memory/activation.txt
          $PI_CODING_AGENT_DIR/skills/ach-memory/SKILL.md
```

Resolve OpenCode from `$XDG_CONFIG_HOME/opencode` (default `~/.config/opencode`) and Pi from `$PI_CODING_AGENT_DIR` (default `~/.pi/agent`).

**Step 5: Verify and commit**

Run: `uv run pytest tests/test_cli.py tests/test_agent_bundle.py -q`

Run: `uv run ruff check src/memory/cli.py tests/test_cli.py tests/test_agent_bundle.py`

Commit:

```bash
git add plugins/ach-memory/adapters src/memory/cli.py tests/test_cli.py tests/test_agent_bundle.py
git commit -m "feat: install OpenCode and Pi memory adapters"
```

---

### Task 5: Enforce all-target preflight and complete operator UX

**Files:**

- Modify: `src/memory/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `.env.example`
- Modify: `README.md`

**Step 1: Write failing orchestration tests**

Assert `init all` resolves all four executables and completes the single MCP preflight before invoking any installer. A missing executable or MCP failure must leave all temporary homes unchanged. Assert a single target fails similarly before its first write. Capture stdout/stderr and scan for the test secret.

**Step 2: Implement preflight-first orchestration**

Expand `all` to the fixed order `codex`, `claude`, `opencode`, `pi`; validate executables and config paths; run one MCP preflight; then install sequentially. Print only changed registrations/files and one restart reminder. Return non-zero on `CLIError` without a traceback.

**Step 3: Make `.env.example` sufficient for local Compose**

Add only the variables actually required by `compose.yml` and the approved no-real-model flow:

```dotenv
MEMORY_MASTER_KEY=
MEMORY_MASTER_KEY_HASH=
HINDSIGHT_LLM_PROVIDER=mock
HINDSIGHT_LLM_MODEL=mock-model
HINDSIGHT_LLM_BASE_URL=http://127.0.0.1:9
HINDSIGHT_LLM_API_KEY=dummy
```

Confirm names against `compose.yml`; do not expose `MEMORY_HINDSIGHT_URL` to agent users.

**Step 4: Replace the provisional Codex-only README block with the approved UX**

Keep the short README structure. Document:

1. `cp .env.example .env` and setting the master-key pair;
2. `docker compose up -d --build`;
3. minting a user/key via a mode-600 curl config, never `curl -H "...master..."`;
4. local exports `ACH_MEMORY_URL=http://localhost:8000` and `ACH_MEMORY_API_KEY`;
5. `uv run ach-memory init all` from source or `ach-memory init <agent>` when installed;
6. remote setup using only public base URL plus user key;
7. restart requirement and explicit/no-automatic-memory behavior.

Do not re-expand the README into an API reference or repeat Hindsight internals.

**Step 5: Verify and commit**

Run: `uv run pytest tests/test_cli.py -q`

Run: `uv run ruff check src/memory/cli.py tests/test_cli.py`

Run: `git diff --check`

Commit with explicit paths, reviewing the pre-existing README delta first:

```bash
git add src/memory/cli.py tests/test_cli.py .env.example README.md
git commit -m "docs: add multi-agent setup flow"
```

---

### Task 6: Product verification in isolated homes and Docker Compose

**Files:**

- Modify only if verification exposes a defect in an owned file from Tasks 1–5.

**Step 1: Run the repository gate**

Run:

```bash
uv run ruff check .
uv run pytest -q
uv build
git diff --check
```

Do not claim DB-backed coverage if PostgreSQL is unavailable; record the exact skipped/failing scope.

**Step 2: Validate all real agent installations without touching the user's normal configs**

Create one `mktemp -d` root and point `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, and `PI_CODING_AGENT_DIR` inside it. Keep the existing Docker Compose API running. Export the already-minted user key only in the process environment and run:

```bash
uv run ach-memory init all
uv run ach-memory init all
```

Confirm both runs succeed, generated files contain no literal key, and the installed versions discover:

- Codex plugin, skill, hooks, and MCP;
- Claude plugin, skill, hooks, and MCP;
- OpenCode plugin, skill, and remote MCP;
- Pi extension, skill, `pi-mcp-adapter`, and MCP.

**Step 3: Verify activation without memory traffic**

Record API/MCP request counts, start each agent non-interactively with `hi` where supported, and confirm the activation protocol is present while retain/recall counts remain zero. For hosts without a stable non-interactive inspection command, execute the installed adapter/hook directly and state that boundary.

**Step 4: Verify one real product memory path**

Start a fresh Codex process using the isolated installed plugin and existing mock-Hindsight Compose stack. Ask it to explicitly `sync_retain` a unique harmless project fact, then explicitly recall it. This is the only verification step allowed to create memory/LLM work; tear down its test bank afterward.

**Step 5: Final review**

Inspect `git status --short`, `git diff HEAD^`, and all test output. If a fix was needed, add the smallest regression test first and commit only owned paths. Report any remaining unrelated dirty files separately.

---

## Plan self-review

- Spec coverage: all four hosts, root/subagent activation, remote URL, env-only credential reference, no auto-memory, idempotency, preflight-first `all`, packaging, README, and live verification are assigned to explicit tasks.
- Minimality: one Python CLI module, one activation file, one skill, one shared Codex/Claude hook, and two host adapters; no framework, state database, wizard, rollback layer, or added dependency.
- Security: trust-boundary URL validation, MCP auth preflight, atomic writes, invalid-config refusal, secret scans, and argv assertions remain mandatory.
- Type consistency: target names are the same five literals in parser, tests, and docs; MCP URLs are concrete strings; secrets remain environment values and generated configs contain only variable names/interpolation.
- Placeholder scan: implementation snippets contain no TODO, ellipsis, fake file path, or unresolved design decision. Test-only values such as `user-secret` and `https://host/prefix` are explicit fixtures.
