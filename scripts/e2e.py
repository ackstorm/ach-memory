#!/usr/bin/env python3
"""End-to-end test of the entire ach-memory product against real infrastructure.

Unlike scripts/smoke.sh (REST happy path only) and scripts/mcp-smoke.py (7 of
15 MCP tools), this exercises every documented surface: identity/access,
projects, memory in both scopes, curation, documents, operations, directives,
mental models, admin, all fifteen MCP tools, and rate limiting -- against the
live docker-compose stack, not mocks.

Usage:
    set -a && . ./.env && set +a
    uv run python scripts/e2e.py

Design
------
Coverage is ~70 small, named scenarios in SCENARIOS, run in order and printed
as they go. Each scenario is a plain async function that raises
AssertionError on failure -- with the request and response embedded in the
message -- and returns None on success. One scenario's exception never stops
the run: the runner in main() catches it, prints FAIL, and continues, so a
single broken surface does not hide problems on every other one. Scenarios
that depend on an earlier scenario's output (a user id, a project slug, a
memory id, ...) read it from the module-level `S` dict; if a prerequisite did
not run, the dependent scenario fails with a clear "prerequisite missing"
message instead of crashing on `None`.

Every response this script collects -- success or error -- is scanned for a
leaked bank id via `scan()`, called from the one HTTP helper (`call`) and
from the MCP result unwrapper (`mcp_unwrap`). `check_no_leaks` (last
scenario) asserts nothing was ever caught.

Names and slugs are namespaced with a random RUN id so two consecutive runs
never collide -- scripts/smoke.sh was silently non-idempotent for weeks
because it reused a fixed project slug across runs with a fresh user each
time.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

API = os.environ.get("API", "http://localhost:8000")
MCP_URL = f"{API}/mcp/"
HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "http://localhost:8888")

sys.stdout.reconfigure(line_buffering=True)  # keep PASS/FAIL in true chronological order
                                              # under redirection (2>&1 | tee, etc.)

MASTER = os.environ.get("MEMORY_MASTER_KEY")
if not MASTER:
    print(
        "FAIL: MEMORY_MASTER_KEY is not set in the environment.\n"
        "Run:  set -a && . ./.env && set +a\n"
        "then re-invoke this script.",
        file=sys.stderr,
    )
    sys.exit(1)

RUN = uuid.uuid4().hex[:10]

# ---------------------------------------------------------------------------
# Leak scanning -- applied to every response this script ever collects.
# The pattern lives in scripts/leakscan.py so smoke.sh, mcp-smoke.py and this
# script cannot drift apart again: this file's own copy was \b-anchored and
# therefore could not see a bank id embedded in a chunk_id, the one shape the
# scan existed to catch.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from leakscan import LEAK_RE

LEAKS: list[tuple[str, str]] = []


def scan(label: str, body: Any) -> None:
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    if LEAK_RE.search(text):
        LEAKS.append((label, text[:600]))


# ---------------------------------------------------------------------------
# HTTP helper. One choke point so every response is scanned automatically.
# ---------------------------------------------------------------------------
CLIENT: httpx.AsyncClient | None = None


async def call(
    method: str,
    path: str,
    key: str | None,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    assert CLIENT is not None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    resp = await CLIENT.request(
        method, path, json=json_body, params=params, headers=headers, timeout=timeout
    )
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    scan(f"{method} {path} -> {resp.status_code}", data)
    return resp.status_code, data


def fmt(method: str, path: str, req: Any, status: int, data: Any) -> str:
    return f"{method} {path} req={req!r} -> HTTP {status} resp={data!r}"


def expect_status(method: str, path: str, req: Any, status: int, data: Any, want: int) -> None:
    if status != want:
        raise AssertionError(f"expected HTTP {want}: {fmt(method, path, req, status, data)}")


def expect_code(method: str, path: str, req: Any, status: int, data: Any, code: str) -> None:
    got = data.get("error", {}).get("code") if isinstance(data, dict) else None
    if got != code:
        raise AssertionError(
            f"expected error code {code}: {fmt(method, path, req, status, data)}"
        )


def sc_body(scope: str, **kw: Any) -> dict:
    """A ScopedRequest-shaped JSON body: scope plus whichever of
    user_id/project_slug/git_locator/... the caller supplies."""
    body = {"scope": scope}
    body.update({k: v for k, v in kw.items() if v is not None})
    return body


def acceptable_race_outcome(status: int, data: Any) -> bool:
    """For an operation that may have already settled by the time we act on
    it (cancel_operation racing Hindsight's own processing): success, or any
    well-formed typed DomainError envelope, is an acceptable outcome. Only an
    untyped/INTERNAL_ERROR shape (or a leak, scanned separately) is a real
    failure here -- see the operations.cancel scenario docstring."""
    if status == 200:
        return True
    return (
        isinstance(data, dict)
        and isinstance(data.get("error"), dict)
        and data["error"].get("code") not in (None, "INTERNAL_ERROR")
    )


async def reflect_with_retry(
    body: dict, key: str, expect_kw: str, *, attempts: int = 4, delay: float = 3.0
) -> dict:
    """`reflect` can run moments after a `retain`/`sync_retain` before
    Hindsight's post-write consolidation has finished, and answers "I don't
    have information" even though the fact is already searchable via
    `recall`. Bounded retry (fixed attempt count, not a naked poll loop) so a
    call made right after a write is not a flaky failure."""
    last: dict = {}
    for _ in range(attempts):
        status, data = await call(
            "POST", "/v1/memory/reflect", key, json_body=body, timeout=60.0
        )
        expect_status("POST", "/v1/memory/reflect", body, status, data, 200)
        if expect_kw in json.dumps(data).lower():
            return data
        last = data
        await asyncio.sleep(delay)
    raise AssertionError(
        f"reflect never surfaced {expect_kw!r} after {attempts} attempts: {last}"
    )


# ---------------------------------------------------------------------------
# Shared fixtures, threaded scenario to scenario.
# ---------------------------------------------------------------------------
S: dict[str, Any] = {}


def need(*keys: str) -> None:
    missing = [k for k in keys if k not in S]
    if missing:
        raise AssertionError(f"prerequisite missing (an earlier scenario failed): {missing}")


SCENARIOS: list[tuple[str, Callable[[], Awaitable[None]]]] = []


def scenario(name: str) -> Callable:
    def deco(fn: Callable[[], Awaitable[None]]) -> Callable:
        SCENARIOS.append((name, fn))
        return fn

    return deco


# ===========================================================================
# 1. Identity and access
# ===========================================================================

USERS = ["alice", "bob", "carol", "dave", "ratelimituser", "mcpuser", "victim", "renamevictim"]


@scenario("identity.provision_users")
async def _() -> None:
    for name in USERS:
        uid = f"e2e-{name}-{RUN}"
        status, data = await call("POST", "/v1/users", MASTER, json_body={"id": uid})
        expect_status("POST", "/v1/users", {"id": uid}, status, data, 201)
        assert data["user_id"] == uid, f"user id echoed back wrong: {data}"
        S[f"user.{name}"] = uid


@scenario("identity.mint_keys")
async def _() -> None:
    need(*[f"user.{n}" for n in USERS])
    for name in USERS:
        uid = S[f"user.{name}"]
        status, data = await call("POST", f"/v1/users/{uid}/keys", MASTER, json_body={})
        expect_status("POST", f"/v1/users/{uid}/keys", {}, status, data, 201)
        assert data["key"].startswith("mem_"), f"key has no mem_ prefix: {data}"
        S[f"key.{name}"] = data["key"]
        S[f"key_id.{name}"] = data["key_id"]


@scenario("identity.create_group")
async def _() -> None:
    gid = f"e2e-team-{RUN}"
    status, data = await call("POST", "/v1/groups", MASTER, json_body={"id": gid})
    expect_status("POST", "/v1/groups", {"id": gid}, status, data, 201)
    assert data["members"] == [], f"fresh group has members: {data}"
    S["group.team"] = gid


@scenario("identity.add_and_remove_member")
async def _() -> None:
    need("group.team", "user.dave")
    gid, uid = S["group.team"], S["user.dave"]
    status, data = await call("PUT", f"/v1/groups/{gid}/members/{uid}", MASTER)
    expect_status("PUT", f"/v1/groups/{gid}/members/{uid}", None, status, data, 204)

    status, data = await call("GET", f"/v1/groups/{gid}", MASTER)
    expect_status("GET", f"/v1/groups/{gid}", None, status, data, 200)
    assert uid in data["members"], f"add_member did not add dave: {data}"

    status, data = await call("DELETE", f"/v1/groups/{gid}/members/{uid}", MASTER)
    expect_status("DELETE", f"/v1/groups/{gid}/members/{uid}", None, status, data, 204)

    status, data = await call("GET", f"/v1/groups/{gid}", MASTER)
    expect_status("GET", f"/v1/groups/{gid}", None, status, data, 200)
    assert uid not in data["members"], f"remove_member left dave in the group: {data}"


@scenario("identity.durable_team_membership")
async def _() -> None:
    """alice and bob are the durable team members every later project/memory
    scenario relies on (Projects and Memory sections)."""
    need("group.team", "user.alice", "user.bob")
    gid = S["group.team"]
    for name in ("alice", "bob"):
        uid = S[f"user.{name}"]
        status, data = await call("PUT", f"/v1/groups/{gid}/members/{uid}", MASTER)
        expect_status("PUT", f"/v1/groups/{gid}/members/{uid}", None, status, data, 204)
    status, data = await call("GET", f"/v1/groups/{gid}", MASTER)
    expect_status("GET", f"/v1/groups/{gid}", None, status, data, 200)
    for name in ("alice", "bob"):
        assert S[f"user.{name}"] in data["members"], f"{name} missing from team: {data}"


@scenario("identity.user_key_refused_on_group_routes")
async def _() -> None:
    need("key.alice", "group.team", "user.carol")
    alice = S["key.alice"]
    gid, other = S["group.team"], S["user.carol"]

    status, data = await call("GET", "/v1/groups", alice)
    expect_status("GET", "/v1/groups", None, status, data, 403)
    expect_code("GET", "/v1/groups", None, status, data, "FORBIDDEN")

    status, data = await call("POST", "/v1/groups", alice, json_body={})
    expect_status("POST", "/v1/groups", {}, status, data, 403)
    expect_code("POST", "/v1/groups", {}, status, data, "FORBIDDEN")

    status, data = await call("PUT", f"/v1/groups/{gid}/members/{other}", alice)
    expect_status("PUT", f"/v1/groups/{gid}/members/{other}", None, status, data, 403)
    expect_code("PUT", f"/v1/groups/{gid}/members/{other}", None, status, data, "FORBIDDEN")


@scenario("keys.revocation_stops_authentication")
async def _() -> None:
    """Review Critical C2, SPEC §5.3: a leaked key must die on request.

    Before Plan 6 the only way to kill a compromised key was
    `UPDATE api_keys SET status='revoked'` directly in Postgres -- there was
    no revoke route at all. Uses its own throwaway user rather than one of
    the shared USERS so revoking it cannot break a later scenario.
    """
    uid = f"e2e-keyrevoke-{RUN}"
    status, data = await call("POST", "/v1/users", MASTER, json_body={"id": uid})
    expect_status("POST", "/v1/users", {"id": uid}, status, data, 201)

    status, data = await call("POST", f"/v1/users/{uid}/keys", MASTER, json_body={})
    expect_status("POST", f"/v1/users/{uid}/keys", {}, status, data, 201)
    key, key_id = data["key"], data["key_id"]

    status, data = await call("GET", "/v1/projects", key)
    expect_status("GET", "/v1/projects", None, status, data, 200)

    status, data = await call("DELETE", f"/v1/users/{uid}/keys/{key_id}", MASTER)
    expect_status("DELETE", f"/v1/users/{uid}/keys/{key_id}", None, status, data, 204)

    status, data = await call("GET", "/v1/projects", key)
    expect_status("GET", "/v1/projects", None, status, data, 401)


# ===========================================================================
# 2. Projects
# ===========================================================================


@scenario("projects.create_owned_by_self")
async def _() -> None:
    need("key.alice")
    slug = f"e2e-proj-self-{RUN}"
    status, data = await call(
        "POST", "/v1/projects", S["key.alice"], json_body={"project_slug": slug}
    )
    expect_status("POST", "/v1/projects", {"project_slug": slug}, status, data, 201)
    assert data["owner"] == {"type": "user", "id": S["user.alice"]}, data
    S["project.self"] = slug


@scenario("projects.master_create_owned_by_group")
async def _() -> None:
    need("group.team")
    slug = f"e2e-proj-group-{RUN}"
    body = {"project_slug": slug, "owner": {"type": "group", "id": S["group.team"]}}
    status, data = await call("POST", "/v1/projects", MASTER, json_body=body)
    expect_status("POST", "/v1/projects", body, status, data, 201)
    assert data["owner"] == {"type": "group", "id": S["group.team"]}, data
    S["project.group"] = slug


@scenario("projects.get")
async def _() -> None:
    need("project.self", "key.alice")
    slug = S["project.self"]
    status, data = await call("GET", f"/v1/projects/{slug}", S["key.alice"])
    expect_status("GET", f"/v1/projects/{slug}", None, status, data, 200)
    assert data["project_slug"] == slug and data["resolved_from"] is None, data


@scenario("projects.create_transfer_and_release_fixtures")
async def _() -> None:
    need("key.alice")
    for label, slug_key in (("transfer", "project.transfer"), ("release", "project.release")):
        slug = f"e2e-proj-{label}-{RUN}"
        status, data = await call(
            "POST", "/v1/projects", S["key.alice"], json_body={"project_slug": slug}
        )
        expect_status("POST", "/v1/projects", {"project_slug": slug}, status, data, 201)
        S[slug_key] = slug


@scenario("projects.rename_forwards_old_slug")
async def _() -> None:
    need("project.self", "key.alice")
    old = S["project.self"]
    new = f"{old}-renamed"
    status, data = await call(
        "PATCH", f"/v1/projects/{old}", S["key.alice"], json_body={"project_slug": new}
    )
    expect_status("PATCH", f"/v1/projects/{old}", {"project_slug": new}, status, data, 200)
    assert data["project_slug"] == new, data
    S["project.self"] = new  # the live slug from here on

    status, data = await call("GET", f"/v1/projects/{old}", S["key.alice"])
    expect_status("GET", f"/v1/projects/{old}", None, status, data, 200)
    assert data["resolved_from"] == old, f"old slug lost resolved_from: {data}"
    assert data["notice"] == "PROJECT_RENAMED", f"missing PROJECT_RENAMED notice: {data}"
    assert data["project_slug"] == new, f"forwarding GET returned stale slug: {data}"

    status, data = await call("GET", f"/v1/projects/{new}", S["key.alice"])
    expect_status("GET", f"/v1/projects/{new}", None, status, data, 200)
    assert data["resolved_from"] is None and data["notice"] is None, data


@scenario("projects.bob_denied_before_transfer")
async def _() -> None:
    need("project.transfer", "key.bob")
    slug = S["project.transfer"]
    status, data = await call("GET", f"/v1/projects/{slug}", S["key.bob"])
    expect_status("GET", f"/v1/projects/{slug}", None, status, data, 403)
    expect_code("GET", f"/v1/projects/{slug}", None, status, data, "PROJECT_ACCESS_DENIED")


@scenario("projects.retain_before_transfer")
async def _() -> None:
    """Write a fact into proj.transfer while alice is still its sole owner --
    the fact the group-transfer scenario later checks bob CAN read.

    Uses a plain, concrete sentence rather than an injected random marker:
    Hindsight's fact extraction paraphrases retained content through an LLM,
    and a random "E2E-<hex>" token does not reliably survive that rewrite
    (measured live -- see e2e-report.md). A short, ordinary keyword like
    "runbook" does. Bank isolation between runs comes from each run using a
    freshly provisioned user/project, not from the content itself.
    """
    need("project.transfer", "key.alice")
    slug = S["project.transfer"]
    content = "The deploy runbook lives in docs/runbooks/deploy.md."
    body = sc_body("project", project_slug=slug, content=content)
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.alice"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)
    S["fact.transfer_keyword"] = "runbook"


@scenario("projects.transfer_to_group_then_member_gets_in")
async def _() -> None:
    need("project.transfer", "group.team", "key.alice", "key.bob")
    slug, gid = S["project.transfer"], S["group.team"]
    body = {"type": "group", "id": gid}
    status, data = await call(
        "PATCH", f"/v1/projects/{slug}/owner", S["key.alice"], json_body=body
    )
    expect_status("PATCH", f"/v1/projects/{slug}/owner", body, status, data, 200)
    assert data["owner"] == {"type": "group", "id": gid}, data

    status, data = await call("GET", f"/v1/projects/{slug}", S["key.bob"])
    expect_status("GET", f"/v1/projects/{slug}", None, status, data, 200)


@scenario("projects.outsider_refused_names_slug_and_kind_not_identity")
async def _() -> None:
    need("project.transfer", "group.team", "key.carol", "user.alice", "user.bob")
    slug, gid = S["project.transfer"], S["group.team"]
    status, data = await call("GET", f"/v1/projects/{slug}", S["key.carol"])
    expect_status("GET", f"/v1/projects/{slug}", None, status, data, 403)
    expect_code("GET", f"/v1/projects/{slug}", None, status, data, "PROJECT_ACCESS_DENIED")
    details = data["error"].get("details", {})
    assert details.get("project_slug") == slug, f"refusal did not name the slug: {data}"
    assert details.get("owner_type") == "group", f"refusal did not name the owner kind: {data}"
    blob = json.dumps(data)
    for leaked_identity in (gid, S["user.alice"], S["user.bob"]):
        assert leaked_identity not in blob, (
            f"refusal disclosed an owner identity ({leaked_identity!r}): {data}"
        )


@scenario("projects.rename_release_fixture_and_confirm_tombstone_blocks_reuse")
async def _() -> None:
    need("project.release", "key.alice", "key.bob")
    old = S["project.release"]
    new = f"{old}-renamed"
    status, data = await call(
        "PATCH", f"/v1/projects/{old}", S["key.alice"], json_body={"project_slug": new}
    )
    expect_status("PATCH", f"/v1/projects/{old}", {"project_slug": new}, status, data, 200)
    S["project.release_old_slug"] = old
    S["project.release"] = new

    status, data = await call(
        "POST", "/v1/projects", S["key.bob"], json_body={"project_slug": old}
    )
    expect_status("POST", "/v1/projects", {"project_slug": old}, status, data, 409)
    expect_code("POST", "/v1/projects", {"project_slug": old}, status, data, "PROJECT_SLUG_CONFLICT")


@scenario("projects.create_outsider_only_fixture")
async def _() -> None:
    """A project carol owns solo and never shares -- the negative case for
    the reachability check below. alice and bob are both durable team
    members (see identity.durable_team_membership), so they correctly see
    every project owned by the team (proj.group, proj.transfer); the only
    project alice must NOT see is one she has no path to at all."""
    need("key.carol")
    slug = f"e2e-proj-outsider-only-{RUN}"
    status, data = await call(
        "POST", "/v1/projects", S["key.carol"], json_body={"project_slug": slug}
    )
    expect_status("POST", "/v1/projects", {"project_slug": slug}, status, data, 201)
    S["project.outsider_only"] = slug


@scenario("projects.list_scoped_to_reachability")
async def _() -> None:
    need(
        "project.self", "project.release", "project.transfer", "project.group",
        "project.outsider_only", "key.alice",
    )
    status, data = await call("GET", "/v1/projects", S["key.alice"])
    expect_status("GET", "/v1/projects", None, status, data, 200)
    slugs = {p["project_slug"] for p in data}
    for reachable in (S["project.self"], S["project.release"], S["project.transfer"], S["project.group"]):
        assert reachable in slugs, f"alice can't see a project she owns or shares: {sorted(slugs)}"
    assert S["project.outsider_only"] not in slugs, (
        f"alice sees a project she has no path to at all: {sorted(slugs)}"
    )


@scenario("projects.list_master_sees_tenant")
async def _() -> None:
    need(
        "project.self", "project.release", "project.transfer", "project.group",
        "project.outsider_only",
    )
    status, data = await call("GET", "/v1/projects", MASTER)
    expect_status("GET", "/v1/projects", None, status, data, 200)
    slugs = {p["project_slug"] for p in data}
    expected = {
        S["project.self"], S["project.release"], S["project.transfer"],
        S["project.group"], S["project.outsider_only"],
    }
    missing = expected - slugs
    assert not missing, f"master key does not see the whole tenant, missing: {missing}"


@scenario("projects.poisoned_locator_can_be_repaired")
async def _() -> None:
    """SPEC §8.4's promised recovery path, over real HTTP (review finding
    I1): a project's first locator otherwise poisons it for every future
    caller presenting a different one, with no documented way back.

    Creates a fresh project already carrying "wrong", confirms a
    sync_retain naming "right" is refused with PROJECT_LOCATOR_MISMATCH,
    PATCHes the locator to "right", then confirms the identical retain now
    succeeds.
    """
    need("key.alice")
    slug = f"e2e-proj-locator-{RUN}"
    wrong = "github.com/e2e/wrong-repo"
    right = "github.com/e2e/right-repo"
    status, data = await call(
        "POST",
        "/v1/projects",
        S["key.alice"],
        json_body={"project_slug": slug, "git_locator": wrong},
    )
    expect_status("POST", "/v1/projects", {"project_slug": slug}, status, data, 201)

    body = sc_body(
        "project", project_slug=slug, git_locator=right, content="poisoned locator probe"
    )
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.alice"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 409)
    expect_code(
        "POST", "/v1/memory/sync_retain", body, status, data, "PROJECT_LOCATOR_MISMATCH"
    )

    patch_body = {"git_locator": right}
    status, data = await call(
        "PATCH", f"/v1/projects/{slug}", S["key.alice"], json_body=patch_body
    )
    expect_status("PATCH", f"/v1/projects/{slug}", patch_body, status, data, 200)
    assert data["git_locator"] == right, f"PATCH did not repair the locator: {data}"

    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.alice"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)


# ===========================================================================
# 3. Memory, both scopes
# ===========================================================================


USER_FACT_KEYWORD = "uv"
USER_FACT_CONTENT = "This project pins its Python dependencies with uv, never with pip."
USER_FACT_QUERY = "how are Python dependencies managed here"


@scenario("memory.sync_retain_and_recall_user_scope")
async def _() -> None:
    need("key.alice")
    body = sc_body("user", content=USER_FACT_CONTENT)
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.alice"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)

    recall_body = sc_body("user", query=USER_FACT_QUERY)
    status, data = await call(
        "POST", "/v1/memory/recall", S["key.alice"], json_body=recall_body
    )
    expect_status("POST", "/v1/memory/recall", recall_body, status, data, 200)
    assert USER_FACT_KEYWORD in json.dumps(data).lower(), (
        f"recall did not surface the retained fact: {data}"
    )


@scenario("memory.second_user_cannot_see_first_users_memory")
async def _() -> None:
    need("key.alice", "key.bob")  # depends on memory.sync_retain_and_recall_user_scope's write
    body = sc_body("user", query=USER_FACT_QUERY)
    status, data = await call("POST", "/v1/memory/recall", S["key.bob"], json_body=body)
    expect_status("POST", "/v1/memory/recall", body, status, data, 200)
    assert USER_FACT_KEYWORD not in json.dumps(data).lower(), (
        f"bob's recall reached alice's private memory: {data}"
    )


@scenario("memory.retain_async_returns_operation")
async def _() -> None:
    need("key.alice")
    content = f"E2E-{RUN}: scenario P, the async retain lifecycle marker."
    body = sc_body("user", content=content)
    status, data = await call("POST", "/v1/memory/retain", S["key.alice"], json_body=body)
    expect_status("POST", "/v1/memory/retain", body, status, data, 200)
    op_id = data["result"].get("operation_id")
    assert op_id, f"retain did not return an operation_id: {data}"
    S["operation.async_retain"] = op_id


@scenario("memory.reflect_user_scope")
async def _() -> None:
    need("key.alice")  # depends on memory.sync_retain_and_recall_user_scope's write
    body = sc_body("user", query="what tool manages python dependencies here")
    await reflect_with_retry(body, S["key.alice"], USER_FACT_KEYWORD)


@scenario("memory.project_scope_shared_with_authorized_group_member")
async def _() -> None:
    """proj.group is owned by the team from creation -- bob writes it, alice
    (a different member, never the writer) reads it back. This is what
    proves group-based sharing rather than owner privilege."""
    need("project.group", "key.bob", "key.alice")
    slug = S["project.group"]
    content = "Migrations in this project run with alembic upgrade head."
    body = sc_body("project", project_slug=slug, content=content)
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.bob"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)

    recall_body = sc_body("project", project_slug=slug, query="how do migrations run")
    status, data = await call(
        "POST", "/v1/memory/recall", S["key.alice"], json_body=recall_body
    )
    expect_status("POST", "/v1/memory/recall", recall_body, status, data, 200)
    assert "alembic" in json.dumps(data).lower(), (
        f"a team member (alice) could not recall a teammate's (bob's) project memory: {data}"
    )


@scenario("memory.project_scope_outsider_refused")
async def _() -> None:
    need("project.group", "key.carol")
    slug = S["project.group"]
    body = sc_body("project", project_slug=slug, query="how do migrations run")
    status, data = await call("POST", "/v1/memory/recall", S["key.carol"], json_body=body)
    expect_status("POST", "/v1/memory/recall", body, status, data, 403)
    expect_code("POST", "/v1/memory/recall", body, status, data, "PROJECT_ACCESS_DENIED")


@scenario("memory.transferred_project_memory_reaches_new_member")
async def _() -> None:
    need("project.transfer", "fact.transfer_keyword", "key.bob")
    slug, keyword = S["project.transfer"], S["fact.transfer_keyword"]
    body = sc_body("project", project_slug=slug, query="where is the deploy runbook")
    status, data = await call("POST", "/v1/memory/recall", S["key.bob"], json_body=body)
    expect_status("POST", "/v1/memory/recall", body, status, data, 200)
    assert keyword in json.dumps(data).lower(), (
        f"bob, newly authorized via group transfer, could not recall the project's memory: {data}"
    )


# ===========================================================================
# 4. Curation
# ===========================================================================


@scenario("curation.list_and_get")
async def _() -> None:
    # Prefer a fact_type == "world" match, deterministically: a single
    # sync_retain of a preference-shaped sentence ("X pins its deps with Y")
    # produces BOTH a "world" fact and an "observation" fact about the same
    # content (measured live), and curating the "observation" one 400s
    # upstream -- see curation.forget_observation_type_memory_is_broken
    # below. Picking "world" here keeps this scenario's outcome independent
    # of that separately-tracked defect and of Hindsight's own item ordering.
    need("key.alice")  # depends on memory.sync_retain_and_recall_user_scope's write
    body = sc_body("user", limit=50)
    status, data = await call("POST", "/v1/memory/list", S["key.alice"], json_body=body)
    expect_status("POST", "/v1/memory/list", body, status, data, 200)
    items = data["result"].get("items", [])
    candidates = [m for m in items if USER_FACT_KEYWORD in json.dumps(m).lower()]
    match = next((m for m in candidates if m.get("fact_type") == "world"), None) or (
        candidates[0] if candidates else None
    )
    assert match, f"list_memories did not surface the retained fact: {data}"
    S["memory.curated_id"] = match["id"]

    get_body = sc_body("user", memory_id=match["id"])
    status, data = await call("POST", "/v1/memory/get", S["key.alice"], json_body=get_body)
    expect_status("POST", "/v1/memory/get", get_body, status, data, 200)
    assert data["result"]["id"] == match["id"], data


@scenario("curation.forget_removes_from_active_set")
async def _() -> None:
    need("memory.curated_id", "key.alice")
    mid = S["memory.curated_id"]
    body = sc_body("user", memory_id=mid, reason="e2e forget")
    status, data = await call("POST", "/v1/memory/forget", S["key.alice"], json_body=body)
    expect_status("POST", "/v1/memory/forget", body, status, data, 200)

    list_body = sc_body("user", limit=50)
    status, data = await call("POST", "/v1/memory/list", S["key.alice"], json_body=list_body)
    expect_status("POST", "/v1/memory/list", list_body, status, data, 200)
    ids = {m["id"] for m in data["result"].get("items", [])}
    assert mid not in ids, f"forget did not remove the memory from the active set: {data}"


@scenario("curation.restore_brings_it_back")
async def _() -> None:
    need("memory.curated_id", "key.alice")
    mid = S["memory.curated_id"]
    body = sc_body("user", memory_id=mid)
    status, data = await call("POST", "/v1/memory/restore", S["key.alice"], json_body=body)
    expect_status("POST", "/v1/memory/restore", body, status, data, 200)

    list_body = sc_body("user", limit=50)
    status, data = await call("POST", "/v1/memory/list", S["key.alice"], json_body=list_body)
    expect_status("POST", "/v1/memory/list", list_body, status, data, 200)
    ids = {m["id"] for m in data["result"].get("items", [])}
    assert mid in ids, f"restore did not bring the memory back into the active set: {data}"


@scenario("curation.correct_replaces_text")
async def _() -> None:
    need("memory.curated_id", "key.alice")
    mid = S["memory.curated_id"]
    new_text = f"E2E-{RUN}-corrected: this project pins dependencies with uv, corrected copy."
    body = sc_body("user", memory_id=mid, content=new_text)
    status, data = await call("POST", "/v1/memory/correct", S["key.alice"], json_body=body)
    expect_status("POST", "/v1/memory/correct", body, status, data, 200)

    get_body = sc_body("user", memory_id=mid)
    status, data = await call("POST", "/v1/memory/get", S["key.alice"], json_body=get_body)
    expect_status("POST", "/v1/memory/get", get_body, status, data, 200)
    assert f"E2E-{RUN}-corrected" in json.dumps(data), f"correct did not stick: {data}"


@scenario("curation.forgetting_a_derived_observation_is_refused_cleanly")
async def _() -> None:
    """A memory with fact_type=="observation" cannot be curated, and that is
    Hindsight behaving correctly, not a defect: an observation is synthesized
    from other facts and regenerates from them, so invalidating one changes
    nothing that lasts. Upstream says so plainly -- "only world/experience
    facts can be curated. Observations are derived and regenerate from their
    sources."

    What WAS a defect, found by this script and since fixed, is that we
    folded that 400 into a 502 HINDSIGHT_ERROR, which tells an agent to retry
    something that can never succeed. This asserts the corrected behavior:
    a clean 409 MEMORY_NOT_CURATABLE.

    Whether a given retain produces an "observation" sibling alongside its
    "world" fact is itself non-deterministic -- measured live: the same
    preference-shaped sentence produced one on roughly 1 of 3 identical
    tries (extraction is LLM-sampled). So this makes a bounded number of
    independent attempts on a fresh bank (carol's, untouched by any other
    scenario) rather than a single one, and reports "not observed this run"
    -- not a failure -- if none of them produced one. That keeps the
    scenario's outcome deterministic across two runs even though the
    underlying artifact it is probing for is not; the defect itself was
    confirmed independently by hand (see e2e-report.md) and reproduces here
    whenever the artifact appears.
    """
    need("key.carol")
    obs = None
    for i in range(5):
        body = sc_body(
            "user",
            content=f"This project pins its Python dependencies with uv (attempt {i}).",
        )
        status, data = await call(
            "POST", "/v1/memory/sync_retain", S["key.carol"], json_body=body, timeout=60.0
        )
        expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)

        list_body = sc_body("user", limit=50)
        status, data = await call(
            "POST", "/v1/memory/list", S["key.carol"], json_body=list_body
        )
        expect_status("POST", "/v1/memory/list", list_body, status, data, 200)
        obs = next(
            (m for m in data["result"].get("items", []) if m.get("fact_type") == "observation"),
            None,
        )
        if obs:
            break

    if not obs:
        print("    note: no observation-type fact observed in 5 attempts this run")
        return

    forget_body = sc_body("user", memory_id=obs["id"], reason="e2e observation probe")
    status, data = await call(
        "POST", "/v1/memory/forget", S["key.carol"], json_body=forget_body
    )
    expect_status("POST", "/v1/memory/forget", forget_body, status, data, 409)
    assert data["error"]["code"] == "MEMORY_NOT_CURATABLE", data
    # The whole point of the fix: not a 502 blaming the backend.
    assert data["error"]["code"] != "HINDSIGHT_ERROR", data


# ===========================================================================
# 5. Documents
# ===========================================================================

DOC_ID = "github:acme/api:pr:382"


@scenario("documents.retain_with_colon_and_slash_id")
async def _() -> None:
    # sync_retain, not retain: the document row and the async retain's own
    # extraction settle on different schedules, and an immediate
    # documents/list right after an async retain can race the document's own
    # materialization (measured live -- see e2e-report.md). sync_retain
    # blocks until the write, including the document, is complete.
    need("key.alice")
    content = "PR 382 fixes the document id round trip."
    body = sc_body("user", content=content, document_id=DOC_ID)
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.alice"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)
    S["document.roundtrip_written"] = True


@scenario("documents.list_finds_it")
async def _() -> None:
    need("document.roundtrip_written", "key.alice")
    body = sc_body("user")
    status, data = await call(
        "POST", "/v1/memory/documents/list", S["key.alice"], json_body=body
    )
    expect_status("POST", "/v1/memory/documents/list", body, status, data, 200)
    ids = {d["id"] for d in data["result"].get("items", [])}
    assert DOC_ID in ids, f"documents/list did not round-trip the colon/slash id: {data}"


@scenario("documents.get_by_colon_and_slash_id")
async def _() -> None:
    need("document.roundtrip_written", "key.alice")
    body = sc_body("user", document_id=DOC_ID)
    status, data = await call(
        "POST", "/v1/memory/documents/get", S["key.alice"], json_body=body
    )
    expect_status("POST", "/v1/memory/documents/get", body, status, data, 200)
    assert data["result"]["id"] == DOC_ID, f"document id mangled in transit: {data}"


@scenario("documents.delete_then_confirm_gone")
async def _() -> None:
    need("document.roundtrip_written", "key.alice")
    body = sc_body("user", document_id=DOC_ID)
    status, data = await call(
        "POST", "/v1/memory/documents/delete", S["key.alice"], json_body=body
    )
    expect_status("POST", "/v1/memory/documents/delete", body, status, data, 200)

    status, data = await call(
        "POST", "/v1/memory/documents/get", S["key.alice"], json_body=body
    )
    expect_status("POST", "/v1/memory/documents/get", body, status, data, 404)
    expect_code("POST", "/v1/memory/documents/get", body, status, data, "DOCUMENT_NOT_FOUND")


APPEND_DOC_ID = "session:e2e-append"


@scenario("retain.append_accumulates_document_text")
async def _() -> None:
    # Before Plan 6 this whole scenario 502'd (sync route) or hung its parent
    # operation `pending` forever with the real error buried in
    # child_operations[0].error_message (async route): our own
    # store_document_text=false made hindsight-api reject an append. sync_retain,
    # not retain, so the write -- including the document -- is guaranteed
    # complete before documents/get runs.
    need("key.alice")
    first_line = "the first line of the session"
    second_line = "the second line of the session"

    body = sc_body("user", content=first_line, document_id=APPEND_DOC_ID, update_mode="replace")
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.alice"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)

    body = sc_body("user", content=second_line, document_id=APPEND_DOC_ID, update_mode="append")
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.alice"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)

    body = sc_body("user", document_id=APPEND_DOC_ID)
    status, data = await call(
        "POST", "/v1/memory/documents/get", S["key.alice"], json_body=body
    )
    expect_status("POST", "/v1/memory/documents/get", body, status, data, 200)
    text = data["result"].get("original_text") or ""
    assert first_line in text, f"first line missing from accumulated document: {data}"
    assert second_line in text, f"second line missing from accumulated document: {data}"


# ===========================================================================
# 6. Operations -- chains the operation_id memory.retain_async_returns_operation
#    produced above.
# ===========================================================================


@scenario("operations.get_resolves_retains_operation")
async def _() -> None:
    need("operation.async_retain", "key.alice")
    op_id = S["operation.async_retain"]
    body = sc_body("user", operation_id=op_id)
    status, data = await call(
        "POST", "/v1/memory/operations/get", S["key.alice"], json_body=body
    )
    expect_status("POST", "/v1/memory/operations/get", body, status, data, 200)
    assert data["result"]["operation_id"] == op_id, data


@scenario("operations.list_contains_it")
async def _() -> None:
    need("operation.async_retain", "key.alice")
    op_id = S["operation.async_retain"]
    body = sc_body("user", limit=50)
    status, data = await call(
        "POST", "/v1/memory/operations/list", S["key.alice"], json_body=body
    )
    expect_status("POST", "/v1/memory/operations/list", body, status, data, 200)
    ids = {op["id"] for op in data["result"].get("operations", [])}
    assert op_id in ids, f"operations/list did not surface retain's operation: {data}"


@scenario("operations.cancel")
async def _() -> None:
    """Best-effort: Hindsight may already have finished extraction by the
    time this runs, in which case cancel legitimately reports the operation
    is no longer cancellable. Only an untyped/INTERNAL_ERROR shape (or a
    leak, scanned separately) is a real failure here.

    Note: a race loss surfaces as HTTP 502 HINDSIGHT_ERROR with
    upstream_status 409 (observed live) -- Hindsight's own 409 Conflict for
    "already completed" gets folded into the same catch-all as a genuine
    backend fault by memory.hindsight.client._request's blanket
    `status >= 400 -> HindsightError`, which reads as "our backend is
    broken" rather than "that operation already finished." Flagged in
    e2e-report.md; tolerated here so this scenario is deterministic across
    runs rather than flaking on the race.
    """
    need("operation.async_retain", "key.alice")
    op_id = S["operation.async_retain"]
    body = sc_body("user", operation_id=op_id)
    status, data = await call(
        "POST", "/v1/memory/operations/cancel", S["key.alice"], json_body=body
    )
    assert acceptable_race_outcome(status, data), (
        f"cancel_operation returned an unexpected shape: "
        f"{fmt('POST', '/v1/memory/operations/cancel', body, status, data)}"
    )
    if status != 200:
        print(f"    note: cancel arrived after the operation settled: {data}")


# ===========================================================================
# 7. Directives (REST-only, never MCP)
# ===========================================================================


@scenario("directives.create")
async def _() -> None:
    need("key.alice")
    name = f"e2e-directive-{RUN}"
    body = sc_body("user", name=name, content="Always use uv for Python dependency management.")
    status, data = await call("POST", "/v1/directives", S["key.alice"], json_body=body)
    expect_status("POST", "/v1/directives", body, status, data, 201)
    S["directive.id"] = data["result"]["id"]


@scenario("directives.list")
async def _() -> None:
    need("directive.id", "key.alice")
    status, data = await call(
        "GET", "/v1/directives", S["key.alice"], params={"scope": "user"}
    )
    expect_status("GET", "/v1/directives", None, status, data, 200)
    ids = {d["id"] for d in data["result"].get("items", data["result"].get("directives", []))}
    assert S["directive.id"] in ids, f"directives list did not surface the created one: {data}"


@scenario("directives.get")
async def _() -> None:
    need("directive.id", "key.alice")
    did = S["directive.id"]
    status, data = await call(
        "GET", f"/v1/directives/{did}", S["key.alice"], params={"scope": "user"}
    )
    expect_status("GET", f"/v1/directives/{did}", None, status, data, 200)
    assert data["result"]["id"] == did, data


@scenario("directives.update")
async def _() -> None:
    need("directive.id", "key.alice")
    did = S["directive.id"]
    body = sc_body("user", content="Always use uv; updated by e2e.")
    status, data = await call(
        "PATCH", f"/v1/directives/{did}", S["key.alice"], json_body=body
    )
    expect_status("PATCH", f"/v1/directives/{did}", body, status, data, 200)


@scenario("directives.delete_then_confirm_gone")
async def _() -> None:
    need("directive.id", "key.alice")
    did = S["directive.id"]
    status, data = await call(
        "DELETE", f"/v1/directives/{did}", S["key.alice"], params={"scope": "user"}
    )
    expect_status("DELETE", f"/v1/directives/{did}", None, status, data, 200)

    status, data = await call(
        "GET", f"/v1/directives/{did}", S["key.alice"], params={"scope": "user"}
    )
    expect_status("GET", f"/v1/directives/{did}", None, status, data, 404)
    expect_code("GET", f"/v1/directives/{did}", None, status, data, "DIRECTIVE_NOT_FOUND")


@scenario("directives.create_on_never_touched_bank")
async def _() -> None:
    """Regression probe for a defect found while building this script:
    creating a directive on a bank nothing has ever retained into used to 502,
    because Hindsight's bank row did not exist yet. `create_directive` is the
    one route that calls `ensure_bank` for exactly this reason (Plan 6 Task 1:
    every other caller was dropped, since retain/recall/reflect and
    create_mental_model all auto-create). Asserts the SPEC-correct 201."""
    need("key.bob")
    slug = f"e2e-proj-freshbank-{RUN}"
    status, data = await call(
        "POST", "/v1/projects", S["key.bob"], json_body={"project_slug": slug}
    )
    expect_status("POST", "/v1/projects", {"project_slug": slug}, status, data, 201)

    body = sc_body("project", project_slug=slug, name="d-fresh", content="never touched")
    status, data = await call("POST", "/v1/directives", S["key.bob"], json_body=body)
    expect_status("POST", "/v1/directives", body, status, data, 201)


# ===========================================================================
# 8. Mental models (REST-only, never MCP)
# ===========================================================================


@scenario("mental_models.create")
async def _() -> None:
    need("key.alice")
    name = f"e2e-mentalmodel-{RUN}"
    body = sc_body(
        "user", name=name, source_query="What tool manages Python dependencies here?"
    )
    status, data = await call("POST", "/v1/mental-models", S["key.alice"], json_body=body)
    expect_status("POST", "/v1/mental-models", body, status, data, 201)
    mm_id = data["result"].get("mental_model_id") or data["result"].get("id")
    assert mm_id, f"create_mental_model returned no usable id: {data}"
    S["mental_model.id"] = mm_id


@scenario("mental_models.list")
async def _() -> None:
    need("mental_model.id", "key.alice")
    status, data = await call(
        "GET", "/v1/mental-models", S["key.alice"], params={"scope": "user"}
    )
    expect_status("GET", "/v1/mental-models", None, status, data, 200)
    ids = {m["id"] for m in data["result"].get("items", [])}
    assert S["mental_model.id"] in ids, f"mental model missing from list: {data}"


@scenario("mental_models.get")
async def _() -> None:
    need("mental_model.id", "key.alice")
    mid = S["mental_model.id"]
    status, data = await call(
        "GET", f"/v1/mental-models/{mid}", S["key.alice"], params={"scope": "user"}
    )
    expect_status("GET", f"/v1/mental-models/{mid}", None, status, data, 200)
    assert data["result"]["id"] == mid, data


@scenario("mental_models.update")
async def _() -> None:
    need("mental_model.id", "key.alice")
    mid = S["mental_model.id"]
    body = sc_body("user", max_tokens=1024)
    status, data = await call(
        "PATCH", f"/v1/mental-models/{mid}", S["key.alice"], json_body=body
    )
    expect_status("PATCH", f"/v1/mental-models/{mid}", body, status, data, 200)


@scenario("mental_models.refresh")
async def _() -> None:
    """Costs a full reflect upstream -- slow. Bounded wait: a generous
    client-side timeout, no naked retry loop."""
    need("mental_model.id", "key.alice")
    mid = S["mental_model.id"]
    status, data = await call(
        "POST",
        f"/v1/mental-models/{mid}/refresh",
        S["key.alice"],
        params={"scope": "user"},
        timeout=90.0,
    )
    expect_status("POST", f"/v1/mental-models/{mid}/refresh", None, status, data, 200)


@scenario("mental_models.clear")
async def _() -> None:
    need("mental_model.id", "key.alice")
    mid = S["mental_model.id"]
    status, data = await call(
        "POST", f"/v1/mental-models/{mid}/clear", S["key.alice"], params={"scope": "user"}
    )
    expect_status("POST", f"/v1/mental-models/{mid}/clear", None, status, data, 200)


@scenario("mental_models.delete")
async def _() -> None:
    need("mental_model.id", "key.alice")
    mid = S["mental_model.id"]
    status, data = await call(
        "DELETE", f"/v1/mental-models/{mid}", S["key.alice"], params={"scope": "user"}
    )
    expect_status("DELETE", f"/v1/mental-models/{mid}", None, status, data, 200)


# ===========================================================================
# 9. Admin (master-key only)
# ===========================================================================


@scenario("admin.audit_shows_actions_just_performed")
async def _() -> None:
    need("user.alice", "key_id.alice", "project.self")
    status, data = await call(
        "GET", "/v1/admin/audit", MASTER, params={"limit": 500}
    )
    expect_status("GET", "/v1/admin/audit", None, status, data, 200)
    blob = json.dumps(data)
    assert S["user.alice"] in blob, "audit trail missing this run's user.create"
    assert S["key_id.alice"] in blob, "audit trail missing this run's key.create"
    actions = {e["action"] for e in data}
    for expected_action in ("user.create", "key.create", "project.rename", "project.transfer"):
        assert expected_action in actions, f"audit trail never recorded {expected_action}"


@scenario("admin.clear_refused_for_user_key_that_owns_the_bank")
async def _() -> None:
    need("key.alice")
    status, data = await call(
        "POST", "/v1/admin/memory/user/clear", S["key.alice"], params={}
    )
    expect_status("POST", "/v1/admin/memory/user/clear", None, status, data, 403)
    expect_code("POST", "/v1/admin/memory/user/clear", None, status, data, "FORBIDDEN")


@scenario("admin.delete_bank_refused_for_user_key_that_owns_the_bank")
async def _() -> None:
    need("key.alice")
    status, data = await call("DELETE", "/v1/admin/memory/user", S["key.alice"])
    expect_status("DELETE", "/v1/admin/memory/user", None, status, data, 403)
    expect_code("DELETE", "/v1/admin/memory/user", None, status, data, "FORBIDDEN")


@scenario("admin.release_slug_refused_for_user_key")
async def _() -> None:
    need("key.alice")
    status, data = await call(
        "POST", "/v1/admin/slugs/does-not-matter/release", S["key.alice"]
    )
    expect_status("POST", "/v1/admin/slugs/does-not-matter/release", None, status, data, 403)
    expect_code(
        "POST", "/v1/admin/slugs/does-not-matter/release", None, status, data, "FORBIDDEN"
    )


@scenario("admin.clear_memories")
async def _() -> None:
    need("user.victim", "key.victim")
    keyword = "uv"
    body = sc_body("user", content="This bank also pins its dependencies with uv.")
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.victim"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)

    list_body = sc_body("user", limit=50)
    status, data = await call(
        "POST", "/v1/memory/list", S["key.victim"], json_body=list_body
    )
    expect_status("POST", "/v1/memory/list", list_body, status, data, 200)
    assert keyword in json.dumps(data).lower(), (
        f"setup: victim's fact never landed, clear would pass trivially: {data}"
    )

    status, data = await call(
        "POST",
        "/v1/admin/memory/user/clear",
        MASTER,
        params={"user_id": S["user.victim"]},
    )
    expect_status("POST", "/v1/admin/memory/user/clear", None, status, data, 200)

    status, data = await call(
        "POST", "/v1/memory/list", S["key.victim"], json_body=list_body
    )
    expect_status("POST", "/v1/memory/list", list_body, status, data, 200)
    assert keyword not in json.dumps(data).lower(), f"admin clear did not empty the bank: {data}"


@scenario("admin.delete_bank")
async def _() -> None:
    need("user.victim", "key.victim")
    status, data = await call(
        "DELETE", "/v1/admin/memory/user", MASTER, params={"user_id": S["user.victim"]}
    )
    expect_status("DELETE", "/v1/admin/memory/user", None, status, data, 200)

    # A torn-down bank re-materializes transparently on the next write.
    body = sc_body("user", content=f"E2E-{RUN}: victim writes again after delete_bank.")
    status, data = await call(
        "POST", "/v1/memory/sync_retain", S["key.victim"], json_body=body, timeout=60.0
    )
    expect_status("POST", "/v1/memory/sync_retain", body, status, data, 200)


@scenario("admin.release_slug_frees_it_for_reuse")
async def _() -> None:
    need("project.release_old_slug", "key.bob")
    old = S["project.release_old_slug"]
    status, data = await call("POST", f"/v1/admin/slugs/{old}/release", MASTER)
    expect_status("POST", f"/v1/admin/slugs/{old}/release", None, status, data, 204)

    status, data = await call(
        "POST", "/v1/projects", S["key.bob"], json_body={"project_slug": old}
    )
    expect_status("POST", "/v1/projects", {"project_slug": old}, status, data, 201)
    assert data["owner"] == {"type": "user", "id": S["user.bob"]}, data


# ===========================================================================
# 10. The MCP surface -- all fifteen tools, plus the master-key refusal.
# ===========================================================================

EXPECTED_MCP_TOOLS = {
    "retain", "sync_retain", "recall", "reflect",
    "list_memories", "get_memory", "forget", "correct", "restore",
    "list_documents", "get_document", "delete_document",
    "get_operation", "list_operations", "cancel_operation",
}


def mcp_unwrap(label: str, result) -> dict:
    if result.is_error:
        text = result.content[0].text if result.content else "<no content>"
        scan(label, text)
        raise AssertionError(f"{label}: tool call failed: {text}")
    scan(label, result.structured_content)
    return result.structured_content["result"]


@scenario("mcp.list_tools_is_exactly_fifteen")
async def _() -> None:
    need("key.mcpuser")
    authed = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {S['key.mcpuser']}"}, timeout=30.0
    )
    async with (
        streamable_http_client(MCP_URL, http_client=authed) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert len(names) == 15, f"expected 15 tools, got {len(names)}: {sorted(names)}"
        assert names == EXPECTED_MCP_TOOLS, (
            f"advertised tool set drifted: extra={sorted(names - EXPECTED_MCP_TOOLS)} "
            f"missing={sorted(EXPECTED_MCP_TOOLS - names)}"
        )


@scenario("mcp.exercise_all_fifteen_tools")
async def _() -> None:
    need("key.mcpuser")
    authed = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {S['key.mcpuser']}"}, timeout=30.0
    )
    content = "This service pins its Python dependencies with uv, never with pip."
    doc_id = "mcp:e2e:doc-1"
    async with (
        streamable_http_client(MCP_URL, http_client=authed) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        called: list[str] = []

        async def tool(name: str, args: dict) -> dict:
            called.append(name)
            res = await session.call_tool(name, args)
            return mcp_unwrap(f"mcp:{name}", res)

        async def reflect_tool(args: dict, expect_kw: str, attempts=4, delay=3.0) -> dict:
            """Same bounded-retry reasoning as reflect_with_retry: reflect
            can run ahead of Hindsight's post-write consolidation."""
            last: dict = {}
            for _ in range(attempts):
                out = await tool("reflect", args)
                if expect_kw in json.dumps(out).lower():
                    return out
                last = out
                await asyncio.sleep(delay)
            raise AssertionError(
                f"mcp reflect never surfaced {expect_kw!r} after {attempts} attempts: {last}"
            )

        # 1. sync_retain
        await tool("sync_retain", {"scope": "user", "content": content})
        # 2. recall
        recalled = await tool(
            "recall", {"scope": "user", "query": "how are python dependencies managed"}
        )
        assert "uv" in json.dumps(recalled).lower(), f"mcp recall missed the fact: {recalled}"
        # 4. retain (async) -- done before reflect so reflect (#3, called later)
        # has real elapsed time for consolidation, not immediately after a write.
        op = await tool("retain", {"scope": "user", "content": "Scenario P via mcp."})
        op_id = op.get("operation_id")
        assert op_id, f"mcp retain returned no operation_id: {op}"
        # 5. get_operation
        got_op = await tool("get_operation", {"scope": "user", "operation_id": op_id})
        assert got_op.get("operation_id") == op_id, got_op
        # 6. list_operations
        ops = await tool("list_operations", {"scope": "user", "limit": 50})
        op_ids = {o["id"] for o in ops.get("operations", [])}
        assert op_id in op_ids, f"mcp list_operations missing the retain op: {ops}"
        # 7. cancel_operation (best-effort -- see operations.cancel's docstring
        # for why an already-settled operation is an acceptable outcome too)
        cancel_res = await session.call_tool(
            "cancel_operation", {"scope": "user", "operation_id": op_id}
        )
        if cancel_res.is_error:
            text = cancel_res.content[0].text if cancel_res.content else ""
            scan("mcp:cancel_operation", text)
            assert not text.startswith("INTERNAL_ERROR"), f"mcp cancel_operation broke: {text}"
            print(f"    note: mcp cancel_operation arrived late: {text}")
        else:
            scan("mcp:cancel_operation", cancel_res.structured_content)
        called.append("cancel_operation")
        # 8. list_memories. Prefer fact_type == "world", deterministically --
        # same reasoning as curation.list_and_get: an "observation" sibling
        # fact can't be forgotten/restored/corrected (a defect tracked
        # separately by curation.forget_observation_type_memory_is_broken)
        # and this scenario should not intermittently hit it too.
        listed = await tool("list_memories", {"scope": "user", "limit": 50})
        uv_items = [m for m in listed.get("items", []) if "uv" in json.dumps(m).lower()]
        match = next((m for m in uv_items if m.get("fact_type") == "world"), None) or (
            uv_items[0] if uv_items else None
        )
        assert match, f"mcp list_memories missed the fact: {listed}"
        mem_id = match["id"]
        # 9. get_memory
        got_mem = await tool("get_memory", {"scope": "user", "memory_id": mem_id})
        assert got_mem["id"] == mem_id, got_mem
        # 10. correct -- a direct text replacement (not re-extracted), so the
        # literal string survives verbatim, same as curation.correct_replaces_text.
        corrected_text = "MCP-CORRECTED: this service pins dependencies with uv."
        await tool(
            "correct", {"scope": "user", "memory_id": mem_id, "content": corrected_text}
        )
        got_corrected = await tool("get_memory", {"scope": "user", "memory_id": mem_id})
        assert corrected_text in json.dumps(got_corrected), (
            f"mcp correct did not stick: {got_corrected}"
        )
        # 11. forget
        await tool("forget", {"scope": "user", "memory_id": mem_id, "reason": "e2e"})
        after_forget = await tool("list_memories", {"scope": "user", "limit": 50})
        assert mem_id not in {m["id"] for m in after_forget.get("items", [])}, (
            "mcp forget did not remove the memory from the active set"
        )
        # 12. restore
        await tool("restore", {"scope": "user", "memory_id": mem_id})
        after_restore = await tool("list_memories", {"scope": "user", "limit": 50})
        assert mem_id in {m["id"] for m in after_restore.get("items", [])}, (
            "mcp restore did not bring the memory back"
        )
        # sync_retain a document (not async retain -- same materialization-race
        # reasoning as documents.retain_with_colon_and_slash_id) to exercise
        # the document tools.
        await tool(
            "sync_retain",
            {"scope": "user", "content": "mcp document round trip.", "document_id": doc_id},
        )
        # 13. list_documents
        docs = await tool("list_documents", {"scope": "user"})
        assert doc_id in {d["id"] for d in docs.get("items", [])}, (
            f"mcp list_documents missed the document: {docs}"
        )
        # 14. get_document
        got_doc = await tool("get_document", {"scope": "user", "document_id": doc_id})
        assert got_doc["id"] == doc_id, got_doc
        # 15. delete_document
        await tool("delete_document", {"scope": "user", "document_id": doc_id})
        del_res = await session.call_tool(
            "get_document", {"scope": "user", "document_id": doc_id}
        )
        assert del_res.is_error, "get_document still found a document mcp just deleted"

        # 3. reflect -- last, so real time has passed since the writes above.
        await reflect_tool(
            {"scope": "user", "query": "what tool manages python dependencies"}, "uv"
        )
        called.append("reflect")

        exercised = set(called) | {"list_memories", "get_memory"}
        missing = EXPECTED_MCP_TOOLS - exercised
        assert not missing, f"did not exercise every tool: missing {missing}"


@scenario("mcp.master_key_refused")
async def _() -> None:
    authed = httpx2.AsyncClient(headers={"Authorization": f"Bearer {MASTER}"}, timeout=30.0)
    async with (
        streamable_http_client(MCP_URL, http_client=authed) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        res = await session.call_tool("recall", {"scope": "user", "query": "anything"})
        text = res.content[0].text if res.content else ""
        scan("mcp:master_key_refused", text)
        assert res.is_error, f"master key was accepted over MCP: {text}"
        assert "FORBIDDEN" in text, f"wrong refusal reason for a master key over MCP: {text}"


# ===========================================================================
# 11. Rate limiting
# ===========================================================================


@scenario("ratelimit.exhaust_write_budget_then_confirm_isolation")
async def _() -> None:
    need("key.ratelimituser", "key.alice")
    key = S["key.ratelimituser"]
    hit = None
    attempts = 0
    for i in range(80):
        attempts += 1
        body = sc_body("user", content=f"E2E-{RUN} rate limit probe {i}")
        status, data = await call("POST", "/v1/memory/retain", key, json_body=body)
        if status == 429:
            hit = data
            break
    assert hit is not None, f"never saw RATE_LIMITED after {attempts} rapid writes"
    assert hit.get("error", {}).get("code") == "RATE_LIMITED", hit
    print(f"    note: rate-limited after {attempts} writes on one credential")

    # A different credential must be unaffected.
    other_body = sc_body("user", content=f"E2E-{RUN}: unaffected credential check.")
    status, data = await call(
        "POST", "/v1/memory/retain", S["key.alice"], json_body=other_body
    )
    expect_status("POST", "/v1/memory/retain", other_body, status, data, 200)


# ===========================================================================
# 12. Cross-cutting: no bank id ever leaked, across every response collected.
# ===========================================================================


@scenario("leaks.no_bank_id_in_any_response_collected")
async def _() -> None:
    if LEAKS:
        detail = "\n".join(f"  - {label}: {snippet}" for label, snippet in LEAKS)
        raise AssertionError(f"{len(LEAKS)} response(s) leaked a bank id:\n{detail}")


# ===========================================================================
# Runner
# ===========================================================================


async def main() -> int:
    global CLIENT
    print(f"ach-memory e2e run {RUN} against {API} (Hindsight at {HINDSIGHT_URL})\n")
    async with httpx.AsyncClient(base_url=API, timeout=30.0) as client:
        CLIENT = client
        passed: list[str] = []
        failed: list[tuple[str, str]] = []
        for name, fn in SCENARIOS:
            try:
                await fn()
                print(f"PASS: {name}")
                passed.append(name)
            except Exception as exc:  # noqa: BLE001 -- report, don't crash the run
                print(f"FAIL: {name}\n  {exc}", file=sys.stderr)
                failed.append((name, str(exc)))

    print()
    print(f"SUMMARY: {len(passed)} passed, {len(failed)} failed, {len(SCENARIOS)} total")
    if failed:
        print("Failed scenarios:", ", ".join(n for n, _ in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
