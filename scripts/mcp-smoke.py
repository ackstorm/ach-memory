#!/usr/bin/env python3
"""MCP smoke test: a real MCP client against the live streamable-HTTP server (SPEC §6).
Exercises all 15 tools. Each check prints "ok <name>" or "FAIL <name>: <why>";
the run continues past a failure and exits 1 at the end if any check failed.
"""

import argparse
import asyncio
import os
import re
import secrets
import sys
import uuid
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from leakscan import find as leak_find

EXPECTED_TOOLS = {
    "retain", "recall", "reflect", "correct", "forget", "restore", "delete_memory",
    "list_memories", "get_memory", "history", "working_state_get", "working_state_put",
    "working_state_delete", "load_context", "get_operation",
}
MEMORY_ID_RE = re.compile(r"^mem_[0-9a-f]{32}$")
_log: list[str] = []
_failures: list[str] = []

def say(line: str) -> None:
    _log.append(line)
    print(line)
def ok(name: str) -> None:
    say(f"ok {name}")
def fail(name: str, why: str) -> None:
    _failures.append(name)
    say(f"FAIL {name}: {why}")
def text(res) -> str:
    return res.content[0].text if res.content else "<no content>"
def unwrap(res) -> dict:
    assert not res.is_error, text(res)
    return res.structured_content["result"]
def proj(t: dict, **extra: object) -> dict:
    return {"scope": "project", "project_slug": t["project"], **extra}
def retain_args(scope: str, content: str, *, op_id: str | None = None, project: str | None = None,
                 **extra: object) -> dict:
    d = {"scope": scope, "content": content, "basis": "human_explicit", "wait": True,
         "operation_id": op_id or str(uuid.uuid4()), **extra}
    if project:
        d["project_slug"] = project
    return d

async def step(name: str, coro) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 -- any check failure becomes a FAIL line, not a crash
        fail(name, str(exc))
    else:
        ok(name)

# Each check makes one or more tool calls and asserts on the result; a failed
# assert (or any exception) becomes the FAIL message for its check.
async def check_tools(t, s) -> None:
    names = {tool.name for tool in (await s.list_tools()).tools}
    assert names == EXPECTED_TOOLS, f"extra={sorted(names - EXPECTED_TOOLS)} missing={sorted(EXPECTED_TOOLS - names)}"
async def check_retain_project(t, s) -> None:
    t["op_id"] = str(uuid.uuid4())
    args = retain_args("project", t["content"], op_id=t["op_id"], project=t["project"], memory_type="convention")
    body = unwrap(await s.call_tool("retain", args))
    assert body["status"] in ("completed", "replayed"), body
    assert body.get("notice") == "PROJECT_CREATED", body
    assert MEMORY_ID_RE.match(body["memory_id"]), body
    t["project_memory_id"] = body["memory_id"]
    t["created"].append(("project", t["project"], body["memory_id"]))
async def check_retain_replay(t, s) -> None:
    args = retain_args("project", t["content"], op_id=t["op_id"], project=t["project"], memory_type="convention")
    body = unwrap(await s.call_tool("retain", args))
    assert body["status"] == "replayed" and body["memory_id"] == t["project_memory_id"], body
async def check_retain_user(t, s) -> None:
    args = retain_args("user", "This smoke-test user prefers concise answers.", memory_type="preference")
    body = unwrap(await s.call_tool("retain", args))
    assert body.get("notice") is None and MEMORY_ID_RE.match(body["memory_id"]), body
    t["created"].append(("user", None, body["memory_id"]))
async def check_recall(t, s) -> None:
    items = unwrap(await s.call_tool("recall", proj(t, query="which tool manages python dependencies?")))["items"]
    assert t["project_memory_id"] in {i["memory_id"] for i in items}, items
    assert all("score" in i for i in items), items
async def check_list_get_history(t, s) -> None:
    mid = t["project_memory_id"]
    listing = unwrap(await s.call_tool("list_memories", proj(t)))
    assert listing["total"] >= 1 and mid in {i["memory_id"] for i in listing["items"]}, listing
    got = unwrap(await s.call_tool("get_memory", proj(t, memory_id=mid)))
    assert got["content"] == t["content"], got
    journal = unwrap(await s.call_tool("history", proj(t, memory_id=mid)))["journal"]
    assert any(e["action"] == "memory.retain" for e in journal), journal
async def check_correct(t, s) -> None:
    mid = t["project_memory_id"]
    t["corrected"] = "This smoke-test project uses uv, pinned via uv.lock, for Python dependencies."
    unwrap(await s.call_tool("correct", proj(t, memory_id=mid, content=t["corrected"], reason="smoke: sharpen")))
    got = unwrap(await s.call_tool("get_memory", proj(t, memory_id=mid)))
    assert got["content"] == t["corrected"] and got["memory_id"] == mid, got
    journal = unwrap(await s.call_tool("history", proj(t, memory_id=mid)))["journal"]
    entry = next((e for e in journal if e["action"] == "memory.correct"), None)
    assert entry and entry["details"].get("before") and entry["details"].get("after") == t["corrected"], journal
async def check_forget_restore(t, s) -> None:
    mid = t["project_memory_id"]
    unwrap(await s.call_tool("forget", proj(t, memory_id=mid, reason="smoke: forget round-trip")))
    invalid = unwrap(await s.call_tool("list_memories", proj(t, state="invalid")))["items"]
    default = unwrap(await s.call_tool("list_memories", proj(t)))["items"]
    invalid_ids, default_ids = {i["memory_id"] for i in invalid}, {i["memory_id"] for i in default}
    assert mid in invalid_ids and mid not in default_ids, (invalid_ids, default_ids)
    unwrap(await s.call_tool("restore", proj(t, memory_id=mid, reason="smoke: restore round-trip")))
    default = unwrap(await s.call_tool("list_memories", proj(t)))["items"]
    assert mid in {i["memory_id"] for i in default}, default
async def check_reflect(t, s) -> None:
    res = await s.call_tool("reflect", proj(t, query="how does this project manage dependencies?"))
    if res.is_error:
        assert "UNSUPPORTED" in text(res), text(res)
        say("(reflect: UNSUPPORTED)")
    else:
        answer = res.structured_content["result"].get("answer")
        assert isinstance(answer, str) and answer, res.structured_content
        say("(reflect: answer)")
async def check_load_context(t, s) -> None:
    body = unwrap(await s.call_tool("load_context", {"project_slug": t["project"]}))
    assert "text" in body and "entries" in body, body
async def check_working_state(t, s) -> None:
    ws, state = f"ws_smoke_{secrets.token_hex(4)}", {"objective": "run the mcp smoke test"}
    unwrap(await s.call_tool("working_state_put", {"workspace_id": ws, "state": state}))
    assert unwrap(await s.call_tool("working_state_get", {"workspace_id": ws}))["state"] == state
    unwrap(await s.call_tool("working_state_delete", {"workspace_id": ws}))
    assert unwrap(await s.call_tool("working_state_get", {"workspace_id": ws}))["state"] is None
async def check_secret_rejection(t, s) -> None:
    content = "AWS key AKIAIOSFODNN7EXAMPLE secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    res = await s.call_tool("retain", retain_args("project", content, project=t["project"], memory_type="fact"))
    assert res.is_error and "CONTENT_REJECTED_BY_SANITIZER" in text(res), text(res)
async def check_unauthenticated(t, s) -> None:
    async with (
        create_mcp_http_client(headers={}) as http,
        streamable_http_client(t["url"], http_client=http) as (r, w),
        ClientSession(r, w) as anon,
    ):
        await anon.initialize()
        res = await anon.call_tool("recall", {"scope": "user", "query": "anything"})
        assert res.is_error and "UNAUTHORIZED" in text(res), text(res)
async def check_leak_scan(t, s) -> None:
    hit = leak_find("\n".join(_log))
    assert hit is None, f"a bank id leaked: {hit}"

async def cleanup(t, s) -> None:
    for scope, project_slug, memory_id in t["created"]:
        del_args = {"scope": scope, "memory_id": memory_id, "reason": "smoke: cleanup"}
        get_args = {"scope": scope, "memory_id": memory_id}
        if project_slug:
            del_args["project_slug"] = get_args["project_slug"] = project_slug

        async def one(del_args=del_args, get_args=get_args) -> None:
            unwrap(await s.call_tool("delete_memory", del_args))
            res = await s.call_tool("get_memory", get_args)
            assert res.is_error and "MEMORY_NOT_FOUND" in text(res), text(res)

        await step(f"cleanup({memory_id})", one())

CHECKS = [
    ("tools/list", check_tools), ("retain(project)", check_retain_project),
    ("retain(replay)", check_retain_replay), ("retain(user)", check_retain_user),
    ("recall", check_recall), ("list/get/history", check_list_get_history),
    ("correct", check_correct), ("forget/restore", check_forget_restore),
    ("reflect", check_reflect), ("load_context", check_load_context),
    ("working_state", check_working_state), ("secret rejection", check_secret_rejection),
    ("unauthenticated", check_unauthenticated), ("leak scan", check_leak_scan),
]

async def run(args: argparse.Namespace) -> int:
    value = f"Bearer {args.key}" if args.header.lower() == "authorization" else args.key
    t = {"project": args.project, "url": args.url, "created": [],
         "content": "This smoke-test project uses uv for Python dependency management."}
    async with (
        create_mcp_http_client(headers={args.header: value}) as http,
        streamable_http_client(args.url, http_client=http) as (r, w),
        ClientSession(r, w) as s,
    ):
        await s.initialize()
        for name, fn in CHECKS:
            await step(name, fn(t, s))
        if not args.keep:
            await cleanup(t, s)
    return 1 if _failures else 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ACH_MEMORY_URL", "http://localhost:8000/mcp/"))
    parser.add_argument("--key", default=os.environ.get("ACH_MEMORY_API_KEY", "alice"))
    parser.add_argument("--header", default=os.environ.get("ACH_MEMORY_HEADER", "Authorization"))
    parser.add_argument("--project", default=f"github.com-ackstorm-smoke-{secrets.token_hex(4)}")
    parser.add_argument("--keep", action="store_true", help="skip cleanup")
    sys.exit(asyncio.run(run(parser.parse_args())))
