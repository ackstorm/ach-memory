# ach-memory setup

The plugin installs a local stdio proxy that resolves the project and forwards to the remote
service. Install with the host's own plugin command:

```bash
claude plugin marketplace add ackstorm/ach-memory && claude plugin install ach-memory@ach-memory
codex  plugin marketplace add ackstorm/ach-memory && codex  plugin add ach-memory@ach-memory
```

Re-running those does not update: both hosts keep the marketplace snapshot they have. Refresh it
first, then restart the host:

```bash
claude plugin marketplace update ach-memory && claude plugin update ach-memory@ach-memory
codex  plugin marketplace upgrade ach-memory && codex  plugin add ach-memory@ach-memory
```

With `ach-cli` installed and signed in (`ach-cli login`) there is nothing to set: the proxy uses
the Hub of its default profile and `ach-cli token`. `ACH_MEMORY_URL` wins over that; with neither,
the proxy tries `http://localhost:8000/mcp/`. To set the connection yourself, put one of these in
the shell profile (`~/.zshrc`, `~/.bashrc`), open a new terminal, and restart the host:

```bash
# A. ACH login, through the LiteLLM gateway (short-lived JWT, refreshed per request)
export ACH_MEMORY_URL=https://api.ackstorm.ai/mcp/ach-memory
export ACH_MEMORY_TOKEN_COMMAND="/path/to/ach-cli token"   # run by sh: full path, not an alias

# B. Static key
export ACH_MEMORY_URL=https://<host>/mcp/
export ACH_MEMORY_API_KEY=<token from your identity provider>
# export ACH_MEMORY_HEADER=x-litellm-api-key   # only if a proxy blocks Authorization
```

`ACH_MEMORY_TOKEN_COMMAND` wins over `ACH_MEMORY_API_KEY`. `ACH_MEMORY_HEADER` defaults to
`Authorization: Bearer <token>`; any other header carries the bare token, so leave it unset for
option A. Never write a token value into a file the user did not ask for, and never echo one.

Check from a repository: `uvx --from git+https://github.com/ackstorm/ach-memory ach-memory
context load` prints the session start when everything works (a `### Memory index` section in a
project with memories), and nothing where nothing is retained yet. When the tools
answer `UNAUTHORIZED: missing or malformed credential`, no credential reached the service:
`ach-cli` is signed out, the variables are unset in the host's environment, or `ACH_MEMORY_URL`
bypasses the gateway that accepts the token. In Codex, remove any hand-written `[mcp_servers.ach-memory]` entry
(`codex mcp remove ach-memory`); the plugin brings its own.
