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

Worth checking as part of this: whether either host expands `${VAR}` in its MCP
config the way Claude Code does for `url` and `headers`. If they do, their
config becomes static too and the installer stops needing to know the endpoint
at all.

Until then `init` keeps working for these two, and `plugins/opencode/` and
`plugins/pi/` stay in the wheel.
