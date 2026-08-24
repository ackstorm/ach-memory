"""Install ach-memory integrations after validating the public MCP endpoint."""

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit, urlunsplit

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


class CLIError(Exception):
    """A concise, user-facing CLI failure."""


def _mcp_url(base: str) -> str:
    parts = urlsplit(base)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise ValueError("ACH_MEMORY_URL must be an absolute http(s) URL without a query or fragment")

    return urlunsplit((parts.scheme, parts.netloc, f"{parts.path.rstrip('/')}/mcp/", "", ""))


async def _preflight(url: str, api_key: str) -> None:
    if not api_key:
        raise CLIError("ACH_MEMORY_API_KEY is required")

    try:
        client = create_mcp_http_client({"Authorization": f"Bearer {api_key}"})
        async with (
            client,
            streamable_http_client(url, http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
    except Exception as exc:
        raise CLIError("MCP preflight failed") from exc

    if {"recall", "retain"} - names:
        raise CLIError("MCP preflight failed: server is missing required public tools")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-memory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("target", choices=("codex", "claude", "opencode", "pi", "all"))
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        url = _mcp_url(os.environ.get("ACH_MEMORY_URL", "http://localhost:8000"))
        asyncio.run(_preflight(url, os.environ.get("ACH_MEMORY_API_KEY", "")))
    except (CLIError, ValueError) as exc:
        print(f"ach-memory: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
