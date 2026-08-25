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
| codex | **no expansion** -- a literal `${VAR}` fails with `relative URL without a base` | `bearer_token_env_var`, an env var *name* |
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
