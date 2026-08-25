# TODO

## Use opencode's and pi's own plugin systems

`ach-memory init opencode|pi` still installs by hand: it edits the host's
config file to register the MCP server and copies an adapter plus
`activation.txt` next to it. Every other host we support installs itself —
claude and codex take `plugin marketplace add ackstorm/ach-memory` and read a
committed plugin out of this repository.

Those two are the last places where we write into someone else's config
directory, which means they are also the only ones where an upgrade, an
uninstall, or a host that moved its paths is our problem rather than the
host's.

Both have a plugin mechanism of their own:

- **opencode** — plugins are npm packages or files under `.opencode/plugins`.
  `codemem` ships one (`.opencode/plugins` in its repository), so there is a
  worked example to follow.
- **pi** — `pi install npm:<package>`, which `_install_pi` already shells out
  to for `pi-mcp-adapter`. Publishing our adapter the same way would let pi
  own installation and updates.

### What each host actually supports

Measured 2026-08-25. No two agree, which is why one static config cannot serve
all four and why the installer still exists for two of them.

| host | endpoint | credential |
| --- | --- | --- |
| claude | `${VAR}` expands in `url` | `${VAR}` expands inside header values |
| codex | **no expansion** -- a literal `${VAR}` fails with `relative URL without a base`, so `init` writes it via `codex mcp add` and re-running repoints it | `bearer_token_env_var`, an env var *name* |
| opencode | not documented; installer writes it literally | `{env:VAR}` in header values -- no `$` |
| pi | not documented; installer writes it literally | `bearerTokenEnv`, an env var *name* |

Only Claude can express a per-deployment endpoint in a committed file. Codex
ignores a `headers` block outright rather than erroring, so getting this wrong
there is silent: the server starts and every call is unauthenticated.

`env_vars` (openai/codex#38438) does not help. It is the legacy `.codex-plugin`
spelling for forwarding named variables to a **stdio** server's environment,
it is rejected by the newer Agent Plugin format, and the issue is still open
with no mention of the `url` field. Worth re-checking when it lands, in case it
grows into general interpolation.

Still worth checking for opencode and pi: whether either expands anything in
`url`. If one does, its config becomes static too and the installer stops
needing to know the endpoint.

Until then `init` keeps working for these two, and `plugins/opencode/` and
`plugins/pi/` stay in the wheel.


## Keyword-gated UserPromptSubmit

Dropped 2026-08-25: it was paid on every message for a reminder actionable on
few of them. `SessionStart` announces the tools once, and the retain guidance
lives in `activation.txt` alongside it.

`Stop` was tried as the replacement and reverted the same day. **Claude Code
treats any output from a Stop hook as feedback that blocks the turn from
ending**, so the nudge re-fired until the block cap: measured at nine extra
model turns for a single response, the agent replying "nothing to retain" each
time, ending with *"A hook blocked the turn from ending 9 consecutive times --
overriding"*. Checking `stop_hook_active` in the hook input and returning
success while it is true bounds this to one extra turn per response (and
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` raises the cap), but a whole model turn to
say "nothing to retain" is worse than what it replaced. Note that no unit test
could have caught this: the script emitted exactly the documented envelope and
exited 0. Only a live run showed it.

engram, which does have a `Stop` hook, is the counter-example that explains the
rule. Its hook is `"async": true` and writes nothing at all -- every curl in
`session-stop.sh` is `> /dev/null 2>&1` -- because its only job is a side effect:
telling its local daemon the session ended. **Stop is for side effects, not for
talking to the model.** Its end-of-session recap comes from somewhere else
entirely: a line of instruction text injected at session start, telling the
model to summarize before it finishes. That text reaches every host through the
same four injection points we already use -- `session-start.sh` for claude and
codex, `experimental.chat.system.transform` for opencode, `before_agent_start`
for pi -- which is why our equivalent lives in `activation.txt` and needs no
hook at all.

The version worth trying later fires only when the prompt looks like it turns
on stored context — `remember`, `recall`, `note`, `last time`, `we decided`,
`previously`, and the Spanish equivalents `recuerda`, `acuérdate`, `apunta`,
`habíamos`, `quedamos`, `la última vez`. Two things to settle first:

- A regex gate would not have caught the case that motivated the hook at all.
  "What is the canary phrase?" contains none of those words, and the agent
  still went to the filesystem for it. A gate narrow enough to be cheap may be
  too narrow to be useful.
- Measure before rebuilding. `Stop` fires roughly once per assistant response,
  which is close to the same cadence `UserPromptSubmit` had, so the current
  layout is not obviously cheaper — it is better *placed*. If the token cost is
  what matters, the lever is debouncing `Stop` to once per session (its stdin
  carries `session_id`, readable with bash builtins the way engram does it in
  `_helpers.sh`), not adding a fourth event.

## opencode and pi have no Stop equivalent

Checked when `UserPromptSubmit` was dropped, so nobody re-derives it:

- **pi** exposes `session_start`, `session_shutdown`, `before_agent_start`,
  `tool_execution_end`, `session_compact`. Our adapter uses
  `before_agent_start`, which is the SessionStart equivalent.
  `session_shutdown` cannot carry a nudge — nothing reads a system prompt once
  the session is ending.
- **opencode** exposes an `event` listener (`session.created`,
  `session.deleted`), `chat.message`, `tool.execute.after`,
  `experimental.chat.system.transform`, `experimental.session.compacting`.
  Our adapter uses the system transform, again the SessionStart equivalent.
  There is no post-response injection point.

Both adapters already guard with `includes(activation)`, so they append once
rather than per message. Neither ever had a per-prompt cost to remove, and
neither can host the retain nudge without a real client — which is what
replacing these with their native plugin systems, above, would buy.
