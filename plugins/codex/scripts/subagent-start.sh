#!/usr/bin/env bash
# ach-memory - SubagentStart hook.
#
# SubagentStart takes its context as a JSON envelope, so this is a plain cat: no
# node, no jq, no runtime that has to be installed before memory works. The
# hook announces the tools and never calls them -- a hook that talked to the
# service would make every session start depend on the network.
#
# Must exit 0 whatever happens: a non-zero UserPromptSubmit hook blocks the
# message.
set -u
cat "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/activation.subagent.json" 2>/dev/null || true
exit 0
