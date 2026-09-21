# Installing ach-memory on a host

Four supported hosts. Each installs with its own native plugin command; there is
nothing of ours to run first. The plugin *is* this repository: one tree, one
skill, one set of hook scripts, and a manifest per host at the root
(`.claude-plugin/`, `.codex-plugin/`, `package.json`).

```bash
claude   plugin marketplace add ackstorm/ach-memory
claude   plugin install ach-memory@ach-memory

codex    plugin marketplace add ackstorm/ach-memory
codex    plugin add ach-memory@ach-memory

opencode plugin ach-memory@git+https://github.com/ackstorm/ach-memory.git --global
pi       install git:github.com/ackstorm/ach-memory
```

Re-running the same command is the update path for Claude Code, Codex and pi.
opencode caches the git spec as an npm dependency and never re-resolves it, so
its update is:

```bash
rm -rf ~/.cache/opencode/packages/ach-memory@git+https:
opencode plugin ach-memory@git+https://github.com/ackstorm/ach-memory.git --global
```

To pin a version, append `#v0.5.0` to the opencode git spec; the other three
follow their marketplace or package source.

## Installing opencode from the service

opencode accepts a URL as a plugin entry and hands it to bun, which expects an
`npm pack` tarball — the same mechanism as a git spec, over plain HTTPS. The
service serves its own bundle at `/plugin`:

```bash
opencode plugin https://api.<domain>/ach-memory/plugin --global
```

No git, no registry, and no way for the bundle to disagree with the service it
talks to: the tarball is packed from the tree the running image was built from.
Caching works exactly as it does for the git spec, so the update path is the
same shape:

```bash
rm -rf "$HOME/.cache/opencode/packages/https:/api.<domain>/ach-memory/plugin"
opencode plugin https://api.<domain>/ach-memory/plugin --global
```

There is no version in the URL: the endpoint always serves whatever release is
deployed. Pin by installing the git spec with a tag instead.

## No version pin, anywhere

Every host hands its plugin an absolute path to the checkout it installed: the
plugin root. The stdio proxy is built from that same checkout —

```
uvx --from "${CLAUDE_PLUGIN_ROOT}" ach-memory mcp --url "$ACH_MEMORY_URL"
```

— so the proxy always matches the plugin, and updating the plugin updates the
proxy. Earlier releases pinned a git tag into the MCP config instead, which the
host then froze at install time; that is how installs ended up stranded on
0.3.1, and one on a tag that no longer existed.

## Required environment

Set these in your shell profile so every agent process inherits them:

```bash
export ACH_MEMORY_URL=https://api.<domain>/ach-memory/mcp/
export ACH_MEMORY_API_KEY=<token from your identity provider>
# export ACH_MEMORY_HEADER=Authorization   # only if a proxy blocks the default header
```

`ACH_MEMORY_URL` is the MCP endpoint, used verbatim by both the stdio proxy and
`ach-memory context load`. `ACH_MEMORY_API_KEY` is whatever credential your
identity provider issues; the service mints none.

Optional: `ACH_MEMORY_NUDGE_INTERVAL` is how often, in seconds, the periodic
retain nudge may fire per session. Unset means `900` (15 minutes).
`ACH_MEMORY_IDLE_INTERVAL` is how long, in seconds, a checkout may go without
a retain before the next prompt carries a reminder, and how long that reminder
then stays quiet. Unset means `1800` (30 minutes).

## What each host gets

| | Claude Code | Codex | opencode | pi |
|---|---|---|---|---|
| MCP tools | `mcp.json` | `codex-mcp.json` | plugin `config` hook | extension writes `~/.pi/agent/mcp.json` |
| Skill | `skills/` | `skills/` | plugin `config` hook | `package.json` `pi.skills` |
| Standing context at start | `SessionStart` | `SessionStart` | `chat.system.transform` | `before_agent_start` |
| Subagent activation | `SubagentStart` | `SubagentStart` | ❌ no subagent event | ❌ no subagent event |
| Retain nudge before compaction | `PreCompact` | `PreCompact` | `session.compacting` | ❌ not wired |
| Periodic retain nudge | `Stop` | `Stop` | `session.idle` → `promptAsync` | `agent_settled` → `sendMessage` |
| Idle retain nudge | `UserPromptSubmit` | `UserPromptSubmit` | `chat.message` | `before_agent_start` → `message` |

opencode has no subagent lifecycle event; pi has none either. pi's
`session_before_compact` can only cancel or replace the whole compaction (its
result is `{cancel?, compaction?}`; `customInstructions` is read-only input),
so there is no way to add the retain nudge to its summarizer.

Claude Code and Codex share the three hook scripts under `hooks/scripts/` but
need separate hook files, because each expands only its own plugin-root
variable: `${CLAUDE_PLUGIN_ROOT}` in `hooks/hooks.json`, `${PLUGIN_ROOT}` in
`hooks/codex-hooks.json`. `tests/test_plugins.py` asserts this, along with every
declared manifest path.

## What the hooks do

- `session-start.sh` — prints the activation policy, then loads bounded standing
  context for the resolved project/user scope.
- `subagent-start.sh` — announces the ach-memory skill to a spawned subagent;
  makes no network call.
- `pre-compact.sh` — nudges the agent to retain durable claims before context is
  compacted.
- `retain-nudge.sh <session-id>` — the periodic nudge, throttled: prints the
  text at most once per `ACH_MEMORY_NUDGE_INTERVAL` per session, else nothing.
  `/clear` has no hook that gives the agent a turn, so this is what keeps a
  long session from losing what it learned when the context goes.
- `idle-nudge.sh <session-id>` — the idle nudge, for the host's "user submitted
  a prompt" event: prints its text only when nothing has been retained for this
  checkout in `ACH_MEMORY_IDLE_INTERVAL`, then not again for that long in the
  same session. The stdio proxy stamps every successful `retain` under the same
  key, so an agent that saves on its own never sees it. Free: it rides into the
  turn the user just started. Independent of the `Stop` clock.
- `stop.sh` — Claude Code and Codex `Stop`. Their stdout never reaches the
  model, so it wraps `retain-nudge.sh` as `{"decision": "block", "reason"}`,
  which makes the agent take one more turn. opencode and pi have no Stop; they
  call `retain-nudge.sh` themselves and inject the text as a synthetic message.

## Verify

```bash
claude mcp list          # ach-memory should show as connected
codex mcp list           # same, with the plugin's own entry and no stale one
```

Then ask the agent to recall something, or run the proxy's own loader directly:

```bash
uvx --from <plugin root> ach-memory context load
```
