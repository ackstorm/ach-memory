"""CLI: `ach-memory mcp`, `ach-memory context load`, `ach-memory families [set ...]`,
`ach-memory hook <pre-compact|idle-nudge>`.

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
    "or a generic session summary. Internal nudge: act on it, say nothing about it."
)

_IDLE_NUDGE = (
    "Nothing has been retained in ach-memory for over 30 minutes. If a durable decision "
    "with its rationale, a constraint, a convention, a verified gotcha, or a dated landmark "
    "has landed in that time, retain it now - one claim per call, in English. If nothing "
    "has, carry on; do not retain session progress or summaries to fill the gap. "
    "Internal nudge: act on it, say nothing about it."
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

    families = commands.add_parser("families", help="show or set the project's family list")
    families.add_argument("--project", default=None)
    families_sub = families.add_subparsers(dest="families_command")
    set_cmd = families_sub.add_parser("set", help="replace the list; 'other' is always kept")
    set_cmd.add_argument("names", nargs="+", metavar="FAMILY")
    set_cmd.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                         help="move OLD's memories to NEW (repeatable)")

    hook = commands.add_parser("hook", help="host lifecycle nudges")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_sub.add_parser("pre-compact")
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


def _families(args: argparse.Namespace) -> int:
    url = os.environ.get("ACH_MEMORY_URL", "")
    slug = args.project or proxy.resolve_project_context()
    if not url or not slug:
        print("ach-memory: needs ACH_MEMORY_URL and a project (--project or a git origin)", file=sys.stderr)
        return 1
    try:
        if args.families_command == "set":
            rename = dict(pair.split("=", 1) for pair in args.rename if "=" in pair)
            result = asyncio.run(proxy.call_tool(
                url, "set_families", {"families": args.names, "rename": rename}, slug))
            print("\n".join(result.get("families") or []))
            return 0
        result = asyncio.run(proxy.call_tool(url, "list_families", {}, slug))
    except Exception as exc:  # noqa: BLE001 -- report the server's message, not a traceback
        print(f"ach-memory: {exc}", file=sys.stderr)
        return 1
    if not result.get("has_list"):
        print(f"{slug} has no family list yet.")
    for row in result.get("families") or []:
        print(f"{row['family']}\t{row['memories']}")
    for family, count in (result.get("unlisted") or {}).items():
        print(f"{family}\t{count}\t(not on the list)")
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

    if args.command == "families":
        return _families(args)

    if args.command == "hook" and args.hook_command == "pre-compact":
        print(_PRE_COMPACT_NUDGE)
        return 0

    if args.command == "hook" and args.hook_command == "idle-nudge":
        print(_IDLE_NUDGE)
        return 0

    return 1
