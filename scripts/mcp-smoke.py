#!/usr/bin/env python3
"""MCP smoke test: proves the fifteen-tool surface against a live stack.

`scripts/smoke.sh` is curl against REST. This is a real MCP client (the SDK's
`ClientSession` over `streamable_http_client`) against the same running
service, because the transport/session layer and the client API itself are
not something a REST curl script can exercise.

Requires the compose stack up and migrated:
    docker compose up -d --build
    docker compose run --rm api python -m alembic upgrade head
    uv run python scripts/mcp-smoke.py
"""

import asyncio
import json
import os
import re
import sys

import httpx
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

API = os.environ.get("API", "http://localhost:8000")
MCP_URL = f"{API}/mcp/"
MASTER = os.environ["MEMORY_MASTER_KEY"]

# The exact fifteen of SPEC §11 -- the excluded set (bank/mental-model/
# directive/admin management) must never appear here.
EXPECTED_TOOLS = {
    "retain", "sync_retain", "recall", "reflect",
    "list_memories", "get_memory", "forget", "correct", "restore",
    "list_documents", "get_document", "delete_document",
    "get_operation", "list_operations", "cancel_operation",
}

# The same check scripts/smoke.sh does: a literal "bank_id" key, or the
# opaque bank id itself (user_<uuid>/project_<uuid>), or the internal
# project id (prj_<hex>) -- never a substring match on our own exposed
# usr_/key_/grp_ ids, which are meant to be visible.
LEAK_RE = re.compile(r'"bank_id"|user_[0-9a-f]{8}-|project_[0-9a-f]{8}-|prj_[0-9a-f]{8}')


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def scan_for_leak(structured_content) -> None:
    text = json.dumps(structured_content, default=str)
    if LEAK_RE.search(text):
        fail(f"a bank id or internal project id leaked in a tool result: {text}")


async def wait_for_api(client: httpx.AsyncClient) -> None:
    """Bounded wait, explicit failure -- never a naked `until ...; sleep` loop."""
    for _ in range(30):
        try:
            if (await client.get(f"{API}/docs")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2)
    fail(f"API never came up at {API}")


async def provision_user_key(client: httpx.AsyncClient) -> str:
    headers = {"Authorization": f"Bearer {MASTER}"}
    user = (await client.post(f"{API}/v1/users", json={}, headers=headers)).json()
    key = (
        await client.post(
            f"{API}/v1/users/{user['user_id']}/keys", json={}, headers=headers
        )
    ).json()
    return key["key"]


def unwrap(result):
    """A CallToolResult -> its `result` payload, after checking for a leak.

    Fails loudly on `is_error` rather than letting a caller's KeyError on a
    missing field stand in for a real backend rejection. Scans the error text
    for a leak too, not just `structured_content` on the success path: Task 2
    found a bank id leaking through exactly this channel (an unguarded
    exception's `str()` echoed verbatim to the caller) before `_run` existed.
    """
    if result.is_error:
        text = result.content[0].text if result.content else "<no content>"
        scan_for_leak(text)
        fail(f"tool call failed: {text}")
    scan_for_leak(result.structured_content)
    return result.structured_content["result"]


def find_memory(listing: dict, needle: str) -> str | None:
    """Real `memories/list` wraps results under "items", not "memories" --
    measured against the live server in the previous plan's smoke run."""
    items = listing.get("items") or listing.get("memories") or []
    for item in items:
        if needle.lower() in json.dumps(item).lower():
            return item.get("id")
    return None


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as rest:
        await wait_for_api(rest)
        user_key = await provision_user_key(rest)

    authed = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {user_key}"}, timeout=30.0
    )
    async with (
        streamable_http_client(MCP_URL, http_client=authed) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        if names != EXPECTED_TOOLS:
            fail(
                "advertised tool set mismatch: "
                f"extra={sorted(names - EXPECTED_TOOLS)} "
                f"missing={sorted(EXPECTED_TOOLS - names)}"
            )

        content = "The mcp-smoke script pins its Python tooling with uv, never with pip."
        unwrap(
            await session.call_tool(
                "sync_retain", {"scope": "user", "content": content}
            )
        )

        recalled = unwrap(
            await session.call_tool(
                "recall",
                {"scope": "user", "query": "how are Python dependencies managed"},
            )
        )
        if "uv" not in json.dumps(recalled).lower():
            fail(f"recall did not return the retained fact: {recalled}")

        listed = unwrap(
            await session.call_tool("list_memories", {"scope": "user", "limit": 50})
        )
        memory_id = find_memory(listed, "uv")
        if not memory_id:
            fail(f"list_memories did not find the retained memory: {listed}")

        unwrap(
            await session.call_tool(
                "forget",
                {"scope": "user", "memory_id": memory_id, "reason": "smoke"},
            )
        )

        after_forget = unwrap(
            await session.call_tool("list_memories", {"scope": "user", "limit": 50})
        )
        active_ids = {
            m.get("id")
            for m in (after_forget.get("items") or after_forget.get("memories") or [])
        }
        if memory_id in active_ids:
            fail("forget did not remove the memory from the active set")

        unwrap(
            await session.call_tool(
                "restore", {"scope": "user", "memory_id": memory_id}
            )
        )

        after_restore = unwrap(
            await session.call_tool("list_memories", {"scope": "user", "limit": 50})
        )
        restored_ids = {
            m.get("id")
            for m in (
                after_restore.get("items") or after_restore.get("memories") or []
            )
        }
        if memory_id not in restored_ids:
            fail("restore did not bring the memory back into the active set")

        op = unwrap(
            await session.call_tool(
                "retain",
                {
                    "scope": "user",
                    "content": "Scenario P: the async retain lifecycle, chained into get_operation.",
                },
            )
        )
        operation_id = op.get("operation_id")
        if not operation_id:
            fail(f"retain did not return an operation_id: {op}")

        got = unwrap(
            await session.call_tool(
                "get_operation",
                {"scope": "user", "operation_id": operation_id},
            )
        )
        if got.get("operation_id") != operation_id:
            fail(f"get_operation did not resolve retain's operation_id: {got}")

    print(
        "PASS: 15 tools, retain -> recall -> forget -> restore, "
        "operation followed, no bank_id leak"
    )


if __name__ == "__main__":
    asyncio.run(main())
