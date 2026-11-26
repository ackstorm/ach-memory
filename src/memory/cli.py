"""CLI: `ach-memory mcp`, `ach-memory context load`, `ach-memory families [set ...]`,
`ach-memory project init <name>`, `ach-memory hook <pre-compact|idle-nudge>`.

Installing is the host's own job -- `claude plugin install`, `codex plugin add`,
`opencode plugin`, `pi install`; see docs/hosts.md.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from memory.errors import InvalidRequest
from memory.mcp import proxy
from memory.slugs import normalize_slug

# Under the hosts' 10 s session-start hook, after ~1 s of uvx start: a hung call then ends
# with the hook's "not reachable" line instead of a silent kill.
_CONTEXT_LOAD_DEADLINE = 8.0

_NO_PROJECT_NOTICE = (
    "No project context: this folder has no git origin and no .ach-memory marker. Call "
    "list_projects, ask the user which project this is (suggest the folder name when none fits), "
    "then write the marker as the ach-memory skill says. Never invent a slug silently."
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
    # Antigravity starts the server in the plugin folder but passes down the session's PWD.
    # sh resets PWD to its own folder, and uv's `ach-memory` launcher is a sh trampoline,
    # so the plugin runs `python -m memory.cli`.
    mcp_cmd.add_argument("--in-pwd", action="store_true", help="run in $PWD, the folder the host passed down")

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

    commands.add_parser("whoami", help="who ach-memory sees you as, and your groups")
    share = commands.add_parser("share", help="list who has access to the project, or share it")
    share.add_argument("--project", default=None)
    share.add_argument("kind", nargs="?", choices=("user", "group"))
    share.add_argument("names", nargs="*", metavar="NAME")
    unshare = commands.add_parser("unshare", help="stop sharing the project with users or groups")
    unshare.add_argument("--project", default=None)
    unshare.add_argument("kind", choices=("user", "group"))
    unshare.add_argument("names", nargs="*", metavar="NAME")

    project = commands.add_parser("project", help="name the project of a folder with no git origin")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    init = project_sub.add_parser("init", help=f"write {proxy.MARKER} here")
    init.add_argument("name")

    hook = commands.add_parser("hook", help="host lifecycle nudges")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)
    hook_sub.add_parser("pre-compact")
    hook_sub.add_parser("idle-nudge")
    return parser


def _context_load(project: str | None) -> int:
    """Print standing context text. A failure prints nothing and exits 1: the hook says so."""
    url = proxy.endpoint()
    slug = project or proxy.resolve_project_context()
    try:
        text = asyncio.run(asyncio.wait_for(proxy.call_load_context(url, slug), _CONTEXT_LOAD_DEADLINE))
    except Exception:  # noqa: BLE001 -- no traceback in the hook's output
        return 1
    if text:
        print(text)
    if slug is None:
        print(_NO_PROJECT_NOTICE)
    return 0


def _families(args: argparse.Namespace) -> int:
    url = proxy.endpoint()
    slug = args.project or proxy.resolve_project_context()
    if not slug:
        print("ach-memory: needs a project (--project or a git origin)", file=sys.stderr)
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


def _whoami() -> int:
    try:
        result = asyncio.run(proxy.call_tool(proxy.endpoint(), "whoami", {}, None))
    except Exception as exc:  # noqa: BLE001 -- report the server's message, not a traceback
        print(f"ach-memory: {exc}", file=sys.stderr)
        return 1
    print(result.get("subject") or "")
    print(f"groups: {', '.join(result.get('groups') or []) or 'none'}")
    if result.get("operator"):
        print("operator")
    return 0


def _access(args: argparse.Namespace) -> int:
    """`share` lists, `share user|group` adds, `unshare user|group` removes."""
    slug = args.project or proxy.resolve_project_context()
    if not slug:
        print("ach-memory: needs a project (--project or a git origin)", file=sys.stderr)
        return 1
    if args.command == "share" and args.kind is None:
        tool, arguments = "list_access", {}
    elif not args.names:
        print(f"ach-memory: name at least one {args.kind}", file=sys.stderr)
        return 1
    else:
        tool = "share_project" if args.command == "share" else "unshare_project"
        arguments = {f"{args.kind}s": args.names}
    try:
        result = asyncio.run(proxy.call_tool(proxy.endpoint(), tool, arguments, slug))
    except Exception as exc:  # noqa: BLE001 -- report the server's message, not a traceback
        print(f"ach-memory: {exc}", file=sys.stderr)
        return 1
    for user in result.get("users") or []:
        print(f"user\t{user}")
    for group in result.get("groups") or []:
        print(f"group\t{group}")
    return 0


def _project_init(name: str) -> int:
    """Write the marker in the current folder; refused where a git origin or a marker (here or
    above) already names the project, so it never splits one that has memory."""
    if proxy._origin_url(None):
        print("ach-memory: this folder has a git origin, which names its project", file=sys.stderr)
        return 1
    if (marker := proxy.find_marker()) is not None or os.path.lexists(proxy.MARKER):
        print(f"ach-memory: {marker or proxy.MARKER} already names this folder's project", file=sys.stderr)
        return 1
    if Path.cwd().resolve() in (Path.home().resolve(), Path("/")):
        print("ach-memory: the home folder and / cannot be a project; run it in the project's folder",
              file=sys.stderr)
        return 1
    try:
        slug = normalize_slug(name)
    except InvalidRequest as exc:
        print(f"ach-memory: {exc.message}", file=sys.stderr)
        return 1
    with open(proxy.MARKER, "x", encoding="utf-8") as marker_file:
        marker_file.write(f'project = "{slug}"\n')
    print(f"{proxy.MARKER}: project = {slug}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.command == "mcp":
        if args.in_pwd and os.path.isdir(os.environ.get("PWD", "")):
            os.chdir(os.environ["PWD"])
        asyncio.run(proxy.serve(args.url or proxy.endpoint()))
        return 0

    if args.command == "context" and args.context_command == "load":
        return _context_load(args.project)

    if args.command == "families":
        return _families(args)

    if args.command == "whoami":
        return _whoami()

    if args.command in ("share", "unshare"):
        return _access(args)

    if args.command == "project" and args.project_command == "init":
        return _project_init(args.name)

    if args.command == "hook" and args.hook_command == "pre-compact":
        print(_PRE_COMPACT_NUDGE)
        return 0

    if args.command == "hook" and args.hook_command == "idle-nudge":
        print(_IDLE_NUDGE)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
