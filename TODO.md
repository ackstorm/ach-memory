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

## Codex never runs our plugin's SessionStart hook

The activation text has never reached codex. Its skill and MCP server both
work — `sync_retain` stores facts and `codex mcp list` reports the server
connected — but `plugins/codex/hooks/hooks.json` does not execute, so nothing
tells a codex session that ach-memory should displace the host's own store.

Measured against codex-cli 0.149.1 by instrumenting the hook script to append
to a log file and reading that file, which removes the model's self-report
from the loop. It never fired under any of:

- a trusted project (`[projects."..."] trust_level = "trusted"` in
  `$CODEX_HOME/config.toml`, falling back to `~/.codex` when unset) as well as
  an untrusted one
- hooks explicitly trusted at the "Hooks need review" prompt, with the new
  `trusted_hash` written back to `config.toml` and `[plugins."..."] enabled = true`
- `"${CLAUDE_PLUGIN_ROOT}/scripts/session-start.sh"`, a relative
  `./scripts/session-start.sh` matching what the official `replayio` and
  `figma` plugins use, and a fully absolute path
- with and without `"matcher": "startup"`, which `$CODEX_HOME/hooks.json` uses
- interactive `codex` and headless `codex exec`
- instrumenting both the installed copy under
  `$CODEX_HOME/plugins/cache/ach-memory/...` and the marketplace snapshot under
  `$CODEX_HOME/.tmp/marketplaces/ach-memory/plugins/codex/`, which is the path
  `codex plugin list` reports

So codex parses our hooks, hashes them, prompts to trust them and records the
result -- and then does not run them.

Our variable IS wrong and should be fixed regardless. engram ships a plugin per
host and uses a different variable in each: `${CLAUDE_PLUGIN_ROOT}` for
claude-code, plain `${PLUGIN_ROOT}` for codex. `${PLUGIN_ROOT}` is the Agent
Plugins spec name -- the codex binary carries `${PLUGIN_ROOT}${PLUGIN_DATA}`
and the message "Agent Plugins stdio `cwd` must be a contained `./`,
`${PLUGIN_ROOT}`, or `${PLUGIN_DATA}` path". (An earlier note here claimed
`CLAUDE_PLUGIN_ROOT` appears nowhere in the codex binary. That was wrong: it is
there, next to `hooks.json` and `config.toml`.) But it is not the blocker
either -- `${PLUGIN_ROOT}` plus engram's `startup|resume|clear` matchers, with
the hooks freshly trusted, did not fire.

## The pattern that demonstrably works on codex: no hook at all

superpowers' codex plugin declares `"hooks": {}` and `"skills": "./skills/"`.
It has no hooks directory. Its always-on behaviour comes entirely from the
description of one skill, `using-superpowers`: "Use when starting any
conversation - establishes how to find and use skills, requiring skill
invocation before ANY response including clarifying questions."

That works here, and we have already measured it working: in the very first
codex run, codex read `superpowers/6.3.0/skills/using-superpowers/SKILL.md`
unprompted, and then read ours next to it. Codex loads skills in untrusted
projects too -- "hooks and exec policies are disabled ... but skills still
load".

So the codex activation channel should be the skill description, not a hook.
That means the activation policy is no longer one text delivered four ways, and
`test_activation_policy_is_identical_everywhere_and_carries_no_secret` has to
change with it.

`$CODEX_HOME/hooks.json` is the obvious fallback -- it is codex's own
user-level hook file and already carries a working-looking SessionStart entry
from codebase-memory. `ach-memory init codex`, which already registers the MCP
server, could append ours there instead. **Verify that a `$CODEX_HOME/hooks.json`
SessionStart entry actually fires before building on it**; that assumption is
untested, and it is the same assumption that made us ship a plugin hook nobody
ever saw run.

Until then codex has the tools and the skill but not the policy, and
`test_activation_policy_is_identical_everywhere_and_carries_no_secret` asserts
four identical copies of a text that only three hosts can receive.

## Swap the reranker for a multilingual one

Retrieval reranks with an English-only cross-encoder, so a memory stored in
any other language is effectively unfindable by an English question. Storing
in English is the mitigation in place today -- on the `retain` and
`sync_retain` tool descriptions and in the skill -- but it only helps facts
written from now on, and it makes the service quietly monolingual.

Measured against the deployed hindsight-api 0.9.1, one Spanish fact, two
spellings of one question:

| query | semantic | keyword | reranker | final |
| --- | --- | --- | --- | --- |
| `cuál es mi color favorito` | 0.825 | 0.30 | 0.988 | 1.087 |
| `what is my favourite colour` | 0.647 | absent | 0.000098 | 0.00011 |

The embedding is multilingual and degrades gracefully -- it still ranked that
Spanish fact above every English memory in the bank. `final` tracks the
reranker, which collapses 10,000x. Hindsight's default is
`HINDSIGHT_API_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2`,
trained on English MS MARCO, and we set no reranker env at all
(`apps/hindsight/base/api.yaml` in the gitops repo configures only DB and LLM).

Options, in ascending order of effort:

- `HINDSIGHT_API_RERANKER_LOCAL_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`
  -- the multilingual sibling of the current default, no external dependency
  and no `trust_remote_code`. Unverified: that it loads in 0.9.1, and what L12
  costs on the pod's 2-CPU / 4Gi limit.
- `HINDSIGHT_API_RERANKER_PROVIDER=litellm` against
  `http://litellm.litellm.svc:4000`, which would bring metering and budgets --
  but no rerank model is registered in that proxy today.
- `HINDSIGHT_API_RERANKER_PROVIDER=rrf` turns reranking off. It would fix the
  collapse by deleting the thing that collapses, and lose real quality: on a
  same-language query the reranker separates the right answers (0.99) from
  everything else (~1e-5) cleanly.

Whichever is chosen, the check is the two queries in the table above, before
and after. Changing the reranker changes every score, not just cross-language
ones. Note also that the `cohere` provider defaults to `rerank-english-v3.0` --
the same monolingual trap one layer out; `rerank-multilingual-v3.0` is the one
to name.
