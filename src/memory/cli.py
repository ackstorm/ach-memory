"""CLI: `ach-memory init`, `ach-memory mcp`, `ach-memory context load`, `ach-memory hook pre-compact`."""

import argparse
import asyncio
import os
import sys

from memory import init
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-memory")
    commands = parser.add_subparsers(dest="command", required=True)

    init_cmd = commands.add_parser("init", help="install the plugin into a coding-agent host")
    init_cmd.add_argument("target", choices=(*init.HOSTS, "all"))
    init_cmd.add_argument(
        "--local",
        action="store_true",
        help="opencode/pi: run the MCP proxy from this environment's ach-memory script "
        "instead of uvx, to test unreleased code",
    )

    mcp_cmd = commands.add_parser("mcp", help="run the stdio MCP proxy")
    mcp_cmd.add_argument("--url", default=os.environ.get("ACH_MEMORY_URL"))

    context = commands.add_parser("context", help="load bounded standing context")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    load = context_sub.add_parser("load")
    load.add_argument("--project", default=None)

    hook = commands.add_parser("hook", help="host lifecycle nudges")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_sub.add_parser("pre-compact")
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

    if args.command == "init":
        return init.init(args.target, args.local)

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

    return 1
