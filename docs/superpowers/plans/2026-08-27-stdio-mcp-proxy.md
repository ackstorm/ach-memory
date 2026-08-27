# stdio MCP Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `ach-memory mcp`, a local stdio MCP server that forwards to the remote HTTP endpoint and auto-fills project context from `MEMORY_PROJECT` or the cwd's Git origin, and make it the installed default on all four hosts (remote HTTP stays first-class and untouched).

**Architecture:** A FastMCP proxy (`create_proxy` over `StreamableHttpTransport` with a bearer header) plus one middleware that injects `git_locator`/`project_slug` into `scope="project"` tool calls that carry neither. The server side is unchanged except a friendlier `PROJECT_CONTEXT_UNAVAILABLE` message. Host installers switch from four divergent HTTP configs to one shape: spawn `uvx ach-memory mcp` with env.

**Tech Stack:** Python 3.12, `fastmcp>=3.4` (new dep), existing `mcp` SDK for the smoke client, argparse CLI, pytest.

## Global Constraints

- All code/comments/docs/commits in English; conventional commit subjects <72 chars.
- Comment style: this repo writes rationale-heavy comments explaining *why* (see `src/memory/cli.py`); match it, don't strip it.
- SPEC §8 resolution order is `MEMORY_PROJECT` → Git-derived → `PROJECT_CONTEXT_UNAVAILABLE`; the proxy must implement exactly this order and never override an explicit `project_slug`/`git_locator` passed by the model.
- No full-suite runs per task. Scoped `pytest` per task; the whole suite runs ONCE in Task 5.
- Server slug normalization/digesting stays server-side (`projects.resolve`); the proxy sends raw values.
- `SUPPORTED` hosts are claude, codex, opencode, pi; every host ends configured with command `uvx`, args `["ach-memory", "mcp"]`, credentials via inherited env (`ACH_MEMORY_URL`, `ACH_MEMORY_API_KEY`), never literal secrets in config files.

---

### Task 1: Reword PROJECT_CONTEXT_UNAVAILABLE for the caller that can act on it

**Files:**
- Modify: `src/memory/banks.py:69-72`
- Test: `tests/test_memory_api.py` (the test asserting the code at line ~231)

**Interfaces:**
- Produces: error message text `"scope=project needs a project: pass project_slug (or git_locator with the repo's origin URL)"`. No API change; SPEC §18 pins only the code, and only the code is asserted today.

- [ ] **Step 1: Extend the existing test to pin the actionable message**

In `tests/test_memory_api.py`, find the test containing `assert response.json()["error"]["code"] == "PROJECT_CONTEXT_UNAVAILABLE"` (around line 231) and add below it:

```python
    # The caller that hits this is usually an LLM holding only the tool
    # schema: "MEMORY_PROJECT or a Git repository" names things it cannot
    # touch, so the message must name the parameters it can actually pass.
    assert "project_slug" in response.json()["error"]["message"]
    assert "git_locator" in response.json()["error"]["message"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_memory_api.py -q -k "context_unavailable or CONTEXT" `
(if `-k` matches nothing, run the file and read the one failure)
Expected: FAIL on the new `project_slug` assertion.

- [ ] **Step 3: Reword the message**

In `src/memory/banks.py`:

```python
    if not slug:
        raise ProjectContextUnavailable(
            "scope=project needs a project: pass project_slug "
            "(or git_locator with the repo's origin URL)"
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_memory_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/memory/banks.py tests/test_memory_api.py
git commit -m "fix(errors): name the params a tool caller can pass in PROJECT_CONTEXT_UNAVAILABLE"
```

---

### Task 2: Proxy module with project-context injection

**Files:**
- Create: `src/memory/mcp/proxy.py`
- Modify: `pyproject.toml` (add `fastmcp>=3.4` to `[project]` dependencies, with a placement comment matching the style of the `mcp` entry)
- Test: `tests/test_mcp_proxy.py` (new)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (Task 3 depends on these exact names):
  - `resolve_project_context(cwd: str | None = None) -> tuple[str | None, str | None]` — `(slug, locator)`; slug from `MEMORY_PROJECT`, else locator from `git remote get-url origin`, else `(None, None)`.
  - `fill_project_arguments(arguments: dict, slug: str | None, locator: str | None) -> None` — mutates in place.
  - `ProjectContextMiddleware(Middleware)` — resolves once in `__init__`.
  - `build_proxy(url: str, api_key: str) -> FastMCP` — proxy with middleware attached, not yet running.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml` `[project]` dependencies, next to the `mcp` entries:

```toml
    # Only src/memory/mcp/proxy.py (the local stdio forwarder behind
    # `ach-memory mcp`) imports this. It is a hard dep, not an extra,
    # because `uvx ach-memory mcp` must work bare -- an extra would put
    # `--from 'ach-memory[proxy]'` into every host config for zero gain.
    "fastmcp>=3.4",
```

Run: `uv sync && uv run python -c "from fastmcp.server import create_proxy; from fastmcp.server.middleware import Middleware; from fastmcp.client.transports import StreamableHttpTransport; print('ok')"`
Expected: `ok`. If an import path fails, check `uv run python -c "import fastmcp; print(fastmcp.__version__)"` against https://gofastmcp.com/servers/proxy and fix the import in the step below to the installed version's path — the three names (`create_proxy`, `Middleware`, `StreamableHttpTransport`) are the stable API per FastMCP 3.x docs.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_mcp_proxy.py`:

```python
"""The proxy's own logic: SPEC §8 resolution done client-side, and the
injection rule that must never override what the model passed.

The forwarding itself is FastMCP's create_proxy and is not re-tested here;
scripts/mcp-smoke.py --proxy exercises it end to end against a live stack.
"""

import subprocess

import pytest

from memory.mcp.proxy import (
    ProjectContextMiddleware,
    fill_project_arguments,
    resolve_project_context,
)


def _git_repo(tmp_path, origin: str | None):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if origin:
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", origin],
            check=True,
        )
    return tmp_path


def test_memory_project_env_wins_over_git(tmp_path, monkeypatch):
    # SPEC §8: MEMORY_PROJECT is checked before Git, even inside a repo.
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.setenv("MEMORY_PROJECT", "payments-api")
    assert resolve_project_context(str(repo)) == ("payments-api", None)


def test_git_origin_becomes_locator(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, "git@github.com:acme/payments-api.git")
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (
        None,
        "git@github.com:acme/payments-api.git",
    )


def test_repo_without_origin_resolves_nothing(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, None)
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(repo)) == (None, None)


def test_no_repo_resolves_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_PROJECT", raising=False)
    assert resolve_project_context(str(tmp_path)) == (None, None)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # The whole point: bare project call gets the locator.
        ({"scope": "project"}, {"scope": "project", "git_locator": "L"}),
        # Explicit values from the model are never overridden.
        (
            {"scope": "project", "project_slug": "theirs"},
            {"scope": "project", "project_slug": "theirs"},
        ),
        (
            {"scope": "project", "git_locator": "theirs"},
            {"scope": "project", "git_locator": "theirs"},
        ),
        # scope=user is untouched -- injecting here would attach a project
        # to a user-bank call the server would then reject or misroute.
        ({"scope": "user"}, {"scope": "user"}),
        # Tools with no scope argument (get_operation etc.) are untouched:
        # injecting an argument their schema lacks fails validation upstream.
        ({"operation_id": "op_1"}, {"operation_id": "op_1"}),
    ],
)
def test_fill_project_arguments_locator(arguments, expected):
    fill_project_arguments(arguments, None, "L")
    assert arguments == expected


def test_fill_prefers_slug_over_locator():
    arguments = {"scope": "project"}
    fill_project_arguments(arguments, "payments-api", "L")
    assert arguments == {"scope": "project", "project_slug": "payments-api"}


@pytest.mark.anyio
async def test_middleware_injects_into_call_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_PROJECT", "payments-api")
    middleware = ProjectContextMiddleware()

    seen = {}

    async def call_next(context):
        seen.update(context.message.arguments)
        return "result"

    class Message:
        name = "list_memories"
        arguments = {"scope": "project"}

    class Context:
        message = Message()

    assert await middleware.on_call_tool(Context(), call_next) == "result"
    assert seen == {"scope": "project", "project_slug": "payments-api"}
```

If the suite's anyio marker differs, mirror whatever an existing async test in `tests/` uses (check `grep -rn "anyio\|asyncio" tests/conftest.py`) rather than inventing new config.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_mcp_proxy.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory.mcp.proxy'`.

- [ ] **Step 4: Implement `src/memory/mcp/proxy.py`**

```python
"""Local stdio MCP server that forwards to the remote HTTP endpoint.

This is the client-side half SPEC §8 always assumed and nothing ever
shipped: "the MCP derives a slug from the current Git repository" cannot
run on the remote server, which sees a bearer token and JSON and nothing
else. Running as a stdio child of the agent host, this process has the
cwd, so it resolves MEMORY_PROJECT -> git origin -> nothing (SPEC §8
order) once at startup and fills the gap into project-scoped tool calls
the model left bare. Measured motivation: pi called
list_memories(scope="project") with neither param and got
PROJECT_CONTEXT_UNAVAILABLE with no way to recover (2026-08-27).

The remote HTTP endpoint stays first-class: this proxy adds arguments the
model omitted and forwards everything else verbatim, so a host talking
HTTP directly sees identical behavior minus the auto-fill.
"""

import os
import subprocess

from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware, MiddlewareContext


def resolve_project_context(cwd: str | None = None) -> tuple[str | None, str | None]:
    """SPEC §8 order: MEMORY_PROJECT, else the repo's origin URL, else nothing.

    Returns (project_slug, git_locator); at most one is set. The raw origin
    URL is sent as git_locator -- canonicalization and the digest suffix are
    the server's job (projects.resolve), same as for any other caller.
    """
    slug = os.environ.get("MEMORY_PROJECT")
    if slug:
        return slug, None
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        # No git on PATH (or a hung filesystem): same outcome as no repo.
        return None, None
    locator = result.stdout.strip()
    if result.returncode != 0 or not locator:
        return None, None
    return None, locator


def fill_project_arguments(
    arguments: dict, slug: str | None, locator: str | None
) -> None:
    """Inject project context into a bare scope=project call, in place.

    Only when the call already carries scope="project": every tool that
    accepts `scope` accepts both project params (they share ScopedRequest),
    while injecting into a scope-less tool (get_operation, ...) would add
    an argument its schema lacks and fail validation upstream. Explicit
    values from the model always win -- MEMORY_PROJECT pointing a second
    repository at an existing project (SPEC §8.1) must not be overridden,
    and neither must a model deliberately addressing another project.
    """
    if arguments.get("scope") != "project":
        return
    if arguments.get("project_slug") or arguments.get("git_locator"):
        return
    if slug:
        arguments["project_slug"] = slug
    elif locator:
        arguments["git_locator"] = locator


class ProjectContextMiddleware(Middleware):
    """Resolves once at startup: the cwd of a stdio child never changes."""

    def __init__(self) -> None:
        self._slug, self._locator = resolve_project_context()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        arguments = context.message.arguments
        if isinstance(arguments, dict):
            fill_project_arguments(arguments, self._slug, self._locator)
        return await call_next(context)


def build_proxy(url: str, api_key: str) -> FastMCP:
    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    proxy = create_proxy(transport, name="ach-memory")
    proxy.add_middleware(ProjectContextMiddleware())
    return proxy
```

If Step 1's import check forced different import paths, use those here. If `create_proxy` will not take a transport directly on the installed version, wrap it: `create_proxy(ProxyClient(transport), ...)` with `from fastmcp.server.providers.proxy import ProxyClient`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_mcp_proxy.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/memory/mcp/proxy.py tests/test_mcp_proxy.py
git commit -m "feat(mcp): stdio proxy with client-side SPEC §8 project resolution"
```

---

### Task 3: `ach-memory mcp` CLI subcommand

**Files:**
- Modify: `src/memory/cli.py` (`_parser` ~line 489, `main` ~line 500)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `build_proxy(url, api_key)` from Task 2.
- Produces: `ach-memory mcp` subcommand; exit 1 with `ACH_MEMORY_API_KEY must be set` on empty key; serves stdio otherwise. Task 4's host configs spawn exactly `uvx ach-memory mcp`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (match the module's existing monkeypatch style):

```python
def test_mcp_requires_api_key(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("ACH_MEMORY_API_KEY", raising=False)
    assert cli.main(["mcp"]) == 1
    assert "ACH_MEMORY_API_KEY" in capsys.readouterr().err


def test_mcp_builds_proxy_from_env_and_runs_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACH_MEMORY_URL", "https://mem.example.com")
    monkeypatch.setenv("ACH_MEMORY_API_KEY", "mem_secret")
    calls: list[tuple[str, str]] = []

    class FakeProxy:
        def run(self) -> None:
            calls.append(("run", "stdio"))

    def fake_build(url: str, key: str) -> FakeProxy:
        calls.append((url, key))
        return FakeProxy()

    monkeypatch.setattr("memory.mcp.proxy.build_proxy", fake_build)
    assert cli.main(["mcp"]) == 0
    # Same /mcp/ derivation init uses -- one _mcp_url, not a second parser.
    assert calls == [("https://mem.example.com/mcp/", "mem_secret"), ("run", "stdio")]
```

Adjust the expected URL to whatever `_mcp_url` actually returns (read it; it exists near the top of `cli.py`) — the assertion's point is that `mcp` reuses `_mcp_url`, not a hand-built string.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -q -k "test_mcp_"`
Expected: FAIL (argparse rejects `mcp` as a choice).

- [ ] **Step 3: Implement**

In `_parser()` after the `init` subparser:

```python
    commands.add_parser(
        "mcp",
        help="run the local stdio MCP proxy (forwards to $ACH_MEMORY_URL)",
    )
```

In `main()`, right after argument parsing succeeds and before the init flow:

```python
    if args.command == "mcp":
        return _serve_mcp()
```

New function near `main()`:

```python
def _serve_mcp() -> int:
    """Run the stdio proxy until the host closes stdin.

    Imported lazily: `init` must keep working on an interpreter where
    fastmcp failed to install, and a plain `ach-memory --help` should not
    pay the fastmcp import.
    """
    from memory.mcp import proxy

    key = os.environ.get("ACH_MEMORY_API_KEY", "")
    if not key:
        print(
            "ach-memory: ACH_MEMORY_API_KEY must be set to run the MCP proxy",
            file=sys.stderr,
        )
        return 1
    url = _mcp_url(os.environ.get("ACH_MEMORY_URL") or "http://localhost:8000")
    proxy.build_proxy(url, key).run()
    return 0
```

Note the monkeypatch target in the test is `memory.mcp.proxy.build_proxy` — the lazy `from memory.mcp import proxy` + `proxy.build_proxy(...)` attribute access is what makes that patchable; do not switch it to `from memory.mcp.proxy import build_proxy`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: PASS (all, not just the new two — `main` changed).

- [ ] **Step 5: Commit**

```bash
git add src/memory/cli.py tests/test_cli.py
git commit -m "feat(cli): ach-memory mcp serves the stdio proxy"
```

---

### Task 4: All four hosts install the stdio proxy

**Files:**
- Modify: `plugins/claude-code/.mcp.json`
- Modify: `src/memory/cli.py` (`_config_plan` ~lines 226-249, `_register_codex_server` ~line 318, `_install_pi` ~line 307, codex guard in `main` ~line 516, codex note in `_report` ~line 458)
- Test: `tests/test_cli.py` (existing assertions at ~lines 403-425, 474, 976)

**Interfaces:**
- Consumes: the `ach-memory mcp` subcommand from Task 3.
- Produces: every host config spawning `uvx ach-memory mcp`. Remote-HTTP configs remain *documented* (Task 5) but are no longer what `init`/the plugin installs.

- [ ] **Step 1: Verify each host's stdio config schema before editing**

The shapes below are the documented ones as of 2026-08-27; confirm each before writing, and prefer the host's documented spelling over the plan's if they diverge:
- claude: stdio entry in `.mcp.json` is `{"type": "stdio", "command": ..., "args": [...]}` — check an existing plugin or https://docs.anthropic.com/en/docs/claude-code/mcp
- codex: `codex mcp add --help` for the stdio form (`codex mcp add <name> -- <command> <args...>`)
- opencode: local server is `{"type": "local", "command": [...], "enabled": true}` — https://opencode.ai/docs/mcp-servers
- pi: stdio server in `mcp.json` (the same file the installer already writes) — check pi's MCP docs / `pi-mcp-adapter` README for the native stdio spelling

- [ ] **Step 2: Update the failing tests first**

In `tests/test_cli.py`:
- ~line 403 (opencode expected server): replace the `{"type": "remote", "url": ..., "headers": ...}` dict with `{"type": "local", "command": ["uvx", "ach-memory", "mcp"], "enabled": True}`.
- ~line 420 (pi expected server): replace the `{"url": ..., "auth": "bearer", "bearerTokenEnv": ..., "lifecycle": ..., "directTools": ...}` dict with `{"command": "uvx", "args": ["ach-memory", "mcp"]}` (or the spelling Step 1 confirmed).
- ~line 474: pi no longer shells out to `pi install npm:pi-mcp-adapter` — expected commands for pi become `[]` like opencode's.
- ~line 976 (codex register): assert the add command is `["codex", "mcp", "add", "ach-memory", "--", "uvx", "ach-memory", "mcp"]` and that `--bearer-token-env-var` / `--url` no longer appear.
- `test_init_refuses_codex_without_an_explicit_endpoint` (~line 812) and `test_init_notes_that_codex_pins_the_endpoint...` (~line 791): the guard and the note they pin are being deleted — rewrite the first to assert codex init *succeeds* without `ACH_MEMORY_URL`, delete the second.

Run: `uv run pytest tests/test_cli.py -q` — expected: the edited tests FAIL against current code.

- [ ] **Step 3: Implement the config changes**

`plugins/claude-code/.mcp.json` — replace entirely:

```json
{
  "mcpServers": {
    "ach-memory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["ach-memory", "mcp"]
    }
  }
}
```

`_config_plan` — the `server` dicts become:

```python
    if target == "opencode":
        server = {
            "type": "local",
            "command": ["uvx", "ach-memory", "mcp"],
            "enabled": True,
        }
        ...
    else:
        server = {"command": "uvx", "args": ["ach-memory", "mcp"]}
```

(keep the `paths` tuples — activation/skill assets are unchanged; drop only what referenced the old remote entries).

`_install_pi` — delete the `_run(["pi", "install", "npm:pi-mcp-adapter"])` line and update the function's role: pi now spawns our stdio proxy natively, the HTTP adapter is not needed.

`_register_codex_server` — replace body:

```python
    _run(["codex", "mcp", "remove", "ach-memory"])
    _run(["codex", "mcp", "add", "ach-memory", "--", "uvx", "ach-memory", "mcp"])
```

and rewrite its docstring: the endpoint is no longer pinned at install time — the proxy reads `ACH_MEMORY_URL` per spawn, so re-running init after changing the URL is no longer required for codex.

`main()` — delete the `if "codex" in targets and base is None: raise CLIError(...)` block (the reason it existed — codex storing the endpoint literally — is gone). `_report` — delete the codex re-run-init note for the same reason.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/claude-code/.mcp.json src/memory/cli.py tests/test_cli.py
git commit -m "feat(install): every host spawns the stdio proxy via uvx"
```

---

### Task 5: End-to-end smoke through the proxy, docs, final gates

**Files:**
- Modify: `scripts/mcp-smoke.py`
- Modify: `README.md` (install/config section), `TODO.md` (host-matrix section note)

**Interfaces:**
- Consumes: everything prior.
- Produces: `uv run python scripts/mcp-smoke.py --proxy` runs the existing fifteen-tool smoke through a spawned `ach-memory mcp` child instead of direct HTTP.

- [ ] **Step 1: Add `--proxy` to the smoke**

In `scripts/mcp-smoke.py`, where the session is opened over `streamable_http_client`, branch on `"--proxy" in sys.argv`:

```python
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

# --proxy: same fifteen-tool run, but through a spawned `ach-memory mcp`
# child -- proving the stdio transport, the bearer forwarding, and that the
# proxy's argument injection does not corrupt any tool's schema. The child
# gets the provisioned key via env exactly the way a host would pass it.
params = StdioServerParameters(
    command="uv",
    args=["run", "ach-memory", "mcp"],
    env={
        **os.environ,
        "ACH_MEMORY_URL": base_url,
        "ACH_MEMORY_API_KEY": api_key,
    },
)
client_cm = stdio_client(params)
```

Reuse the existing session-handling and checks verbatim — only the transport context manager differs. Use the same variable names the script already has for the base URL and the provisioned key (read the script; it provisions the key itself in `provision_user_key`).

- [ ] **Step 2: Run the smoke both ways against the live stack**

```bash
docker compose up -d --build
docker compose run --rm api python -m alembic upgrade head
uv run python scripts/mcp-smoke.py            # direct HTTP still green
uv run python scripts/mcp-smoke.py --proxy    # through the stdio proxy
```

Expected: both pass. If `--proxy` fails on tool listing, the fastmcp↔mcp SDK version pairing is the first suspect — check versions before touching code.

- [ ] **Step 3: Documentation**

- `README.md`: in the setup/config section, present `uvx ach-memory mcp` (stdio, auto project resolution) as the default for all four hosts, and keep the direct remote HTTP config documented as fully supported for anything that prefers it — include the existing HTTP JSON block under a "direct HTTP" heading rather than deleting it.
- `TODO.md`: in "Use opencode's and pi's own plugin systems" / the host-matrix table section, add a dated note (2026-08-27) that the stdio proxy removed the per-host endpoint/credential divergence (codex header-ignoring, literal URLs, `pi-mcp-adapter`), and what remains of that TODO is only the native-plugin distribution question.

- [ ] **Step 4: Full gates, once**

```bash
uv run ruff check src tests scripts
uv run pytest -q
```

Expected: clean. This is the single full-suite run for the whole plan; do not repeat it per fix — re-run only the failing file until green, then once more `uv run pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp-smoke.py README.md TODO.md
git commit -m "test(mcp): smoke runs through the stdio proxy; document stdio-first install"
```
