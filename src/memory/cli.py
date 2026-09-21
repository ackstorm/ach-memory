"""CLI: `ach-memory mcp`, `ach-memory context load`, `ach-memory hook <pre-compact|retain-nudge|idle-nudge>`.

Installing is the host's own job -- `claude plugin install`, `codex plugin add`,
`opencode plugin`, `pi install`; see docs/hosts.md.
"""

import argparse
import asyncio
import os
import sys

from memory.mcp import proxy

_NO_PROJECT_NOTICE = (
    "No project context: this directory has no git origin. Pass project_slug or set "
    "MEMORY_PROJECT to retain project memory."
)

_PRE_COMPACT_NUDGE = (
    "Before context is compacted, retain any durable decision, constraint, convention, "
    "fact or verified gotcha that is not yet in ach-memory. Do not retain the transcript "
    "or a generic session summary."
)

_RETAIN_NUDGE = (
    "Before this turn ends, check ach-memory for anything durable that is not stored yet. "
    "Retain ONLY what a future session would need: a decision with its rationale, a "
    "constraint, a convention, a verified gotcha, a fact not cheaply rediscoverable from "
    "the repo, or a dated landmark that lets someone reconstruct what changed and when. "
    "Do NOT retain session progress, transcripts, summaries, plans, options considered, "
    "things you tried, or anything the code and git history already say. One claim per "
    "retain call, in English. If nothing qualifies - the usual case - retain nothing, say "
    "nothing, and stop."
)

_IDLE_NUDGE = (
    "Nothing has been retained in ach-memory for over 30 minutes. If a durable decision "
    "with its rationale, a constraint, a convention, a verified gotcha, or a dated landmark "
    "has landed in that time, retain it now - one claim per call, in English. If nothing "
    "has, carry on; do not retain session progress or summaries to fill the gap."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-memory")
    commands = parser.add_subparsers(dest="command", required=True)

    mcp_cmd = commands.add_parser("mcp", help="run the stdio MCP proxy")
    mcp_cmd.add_argument("--url", default=os.environ.get("ACH_MEMORY_URL"))

    context = commands.add_parser("context", help="load bounded standing context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    load = context_sub.add_parser("load")
    load.add_argument("--project", default=None)

    hook = commands.add_parser("hook", help="host lifecycle nudges")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_sub.add_parser("pre-compact")
    hook_sub.add_parser("retain-nudge")
    hook_sub.add_parser("idle-nudge")
    return parser


def _context_load(project: str | None) -> int:
    """Print standing context text; any failure prints nothing so a hook never breaks a session."""
    url = os.environ.get("ACH_MEMORY_URL", "")
    slug = project or proxy.resolve_project_context()
    try:
        text = asyncio.run(proxy.call_load_context(url, slug))
    except Exception:  # noqa: BLE001 -- fail open, must never break the session
        return 0
    if text:
        print(text)
    if slug is None:
        print(_NO_PROJECT_NOTICE)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.command == "mcp":
        if not args.url:
            print("ach-memory: --url or ACH_MEMORY_URL is required", file=sys.stderr)
            return 1
        asyncio.run(proxy.serve(args.url))
        return 0

    if args.command == "context" and args.context_command == "load":
        return _context_load(args.project)

    if args.command == "hook" and args.hook_command == "pre-compact":
        print(_PRE_COMPACT_NUDGE)
        return 0

    if args.command == "hook" and args.hook_command == "retain-nudge":
        print(_RETAIN_NUDGE)
        return 0

    if args.command == "hook" and args.hook_command == "idle-nudge":
        print(_IDLE_NUDGE)
        return 0

    return 1
