import json
from typing import ClassVar

import httpx
import pytest
import respx

from memory.mcp.tools import MCPToolError

BASE = "http://hindsight.test"
GHOST = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def call_tool(app, client, master_headers, tenant):
    """Invoke a registered tool the way the SDK would, with real headers.

    Goes through the same registry the transport uses, so a tool that is not
    registered raises here exactly as it would over the wire.
    """
    from memory.mcp import tools as tool_module

    def _make_user() -> str:
        user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
            "user_id"
        ]
        _call.last_user_id = user_id
        return client.post(
            f"/v1/users/{user_id}/keys", json={}, headers=master_headers
        ).json()["key"]

    def _call(name: str, key: str, **kwargs):
        class _Ctx:
            headers: ClassVar = {"authorization": f"Bearer {key}"}

        return tool_module.REGISTRY[name](ctx=_Ctx(), **kwargs)

    _call.make_user = _make_user
    return _call


@respx.mock
def test_retain_reaches_the_callers_own_bank(call_tool, session):
    """Asserts the CALLER's real bank id, not just the `user_` scope prefix --
    a substring match would stay green even if the request were routed to a
    different user's `user_`-prefixed bank."""
    from memory.models import User

    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    key = call_tool.make_user()
    bank_id = session.get(User, call_tool.last_user_id).bank_id

    result = call_tool("retain", key, scope="user", content="uv, not pip")

    assert result.result == {"operation_id": "op_1"}
    assert f"banks/{bank_id}/" in str(route.calls.last.request.url)


@respx.mock
def test_a_tool_never_returns_a_bank_id(call_tool, session):
    """Both the literal `bank_id` key and its use as a chunk_id substring
    (measured against a live server, SPEC inv. 29 -- see
    test_curation_api.py::test_bank_id_embedded_in_chunk_id_is_redacted) must
    be gone from a tool's result. The mock uses the CALLER's real bank_id --
    not a placeholder string -- because `_strip_bank_id`'s substring redaction
    only ever matches the bank_id it is handed, exactly as a live Hindsight
    response would.

    Runs with verbose=True as well, and that is the case that carries the
    weight: the reduced shape drops `chunk_id` outright, so asserting only
    against it would leave the substring redaction untested while still
    looking green. Invariant 29 has to hold on the payload where the field
    actually survives.
    """
    from memory.models import User

    _mock_bank()
    key = call_tool.make_user()
    bank_id = session.get(User, call_tool.last_user_id).bank_id
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(
            200,
            json={"bank_id": bank_id, "results": [{"chunk_id": f"{bank_id}_d_0"}]},
        )
    )

    for verbose in (False, True):
        result = call_tool("recall", key, scope="user", query="deps", verbose=verbose)
        assert bank_id not in str(result.model_dump())

    # The verbose payload still HAS the chunk_id -- otherwise the assertion
    # above would be vacuous in both directions.
    assert "chunk_id" in str(
        call_tool("recall", key, scope="user", query="deps", verbose=True).result
    )


@respx.mock
def test_a_tool_cannot_reach_another_users_project(call_tool):
    """A DomainError raised inside `_run` must surface as `MCPToolError`, not
    escape as the raw `ProjectAccessDenied` -- and it must keep the SPEC §18
    disclosure (code + project_slug + owner_type) REST's JSON envelope makes,
    not just a bare sentence with no code an MCP client could act on."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("recall", alice, scope="project", project_slug="payments", query="x")

    assert exc_info.value.code == "PROJECT_ACCESS_DENIED"
    assert exc_info.value.details == {"project_slug": "payments", "owner_type": "user"}


@respx.mock
def test_a_reserved_metadata_key_is_refused_and_nothing_is_retained(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "retain", key, scope="user", content="x", metadata={"user_id": "someone"}
        )

    assert route.call_count == 0
    assert exc_info.value.code == "INVALID_METADATA"
    assert exc_info.value.details == {"key": "user_id"}


@respx.mock
def test_user_scope_retain_ignores_the_raw_project_slug_argument(call_tool):
    """`project_slug` is meaningless under scope=user (`_resolve_bank` returns
    None for it) but used to be stamped into extraction metadata verbatim
    regardless of scope -- an attacker-chosen value landing in a private
    bank. REST never had this bug: it always used `_resolve_bank`'s resolved
    slug, never the caller's raw argument."""
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    key = call_tool.make_user()

    call_tool("retain", key, scope="user", content="x", project_slug="acme-secrets")

    sent = json.loads(route.calls.last.request.read())
    assert "metadata" not in sent["items"][0]


@respx.mock
def test_project_scope_retain_stamps_the_live_slug_after_a_rename(call_tool, client):
    """Mirrors test_memory_api.py's
    test_retain_against_a_retired_slug_forwards_and_carries_the_notice: after
    a rename, a retain against the OLD slug must forward to the same bank,
    stamp the NEW (live) slug into extraction metadata -- not the retired one
    the caller asked with -- and report resolved_from/project_slug/notice on
    the ToolResult (SPEC §8.6)."""
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_1"})
    )
    key = call_tool.make_user()
    headers = {"Authorization": f"Bearer {key}"}

    result = call_tool(
        "retain", key, scope="project", project_slug="payments-api", content="x"
    )
    assert result.project_slug == "payments-api"
    sent = json.loads(route.calls.last.request.read())
    assert sent["items"][0]["metadata"]["project_slug"] == "payments-api"

    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-service"},
        headers=headers,
    )

    result = call_tool(
        "retain", key, scope="project", project_slug="payments-api", content="y"
    )

    sent = json.loads(route.calls.last.request.read())
    assert sent["items"][0]["metadata"]["project_slug"] == "payments-service"
    assert result.resolved_from == "payments-api"
    assert result.project_slug == "payments-service"
    assert result.notice == "PROJECT_RENAMED"


@respx.mock
def test_an_unexpected_exception_is_sanitized_and_never_echoed(
    call_tool, session, monkeypatch
):
    """A bug below a tool (a driver error, anything not a DomainError) must
    become a fixed message -- never the original text, which can carry a
    bank id, SQL, or a connection string (measured live: a RuntimeError with
    a bank id embedded reached the caller verbatim before this fix)."""
    from memory.hindsight.client import HindsightClient
    from memory.models import User

    key = call_tool.make_user()
    bank_id = session.get(User, call_tool.last_user_id).bank_id

    def _boom(*_args, **_kwargs):
        raise RuntimeError(
            f"psycopg: INSERT INTO projects (bank_id) VALUES ('{bank_id}') -- secret"
        )

    monkeypatch.setattr(HindsightClient, "recall", _boom)

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("recall", key, scope="user", query="deps")

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert exc_info.value.message == "internal error"
    assert bank_id not in str(exc_info.value)


@respx.mock
def test_reflect_reaches_the_reflect_endpoint(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "uv"})
    )
    key = call_tool.make_user()

    call_tool("reflect", key, scope="user", query="deps?")

    assert route.call_count == 1


def _mock_bank() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


def test_create_is_keyword_only_on_run():
    """`create=False` guards against permanently squatting a project slug
    (see resolve_project_bank's docstring); eleven more tools copy `_run`'s
    shape next, and a bare positional `True`/`False` in this slot is the
    exact shape a copy-paste error reintroduces silently."""
    import inspect

    from memory.mcp.tools import _run

    assert (
        inspect.signature(_run).parameters["create"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )


# One table, both flags, all fifteen tools. Each tool declares `create` and
# `is_write` independently at its own `_run(...)` call site (or, for
# list_documents/list_operations, its `_list_documents`/`_list_operations`
# helper) -- there is no shared literal a single test could pin, so each of
# the fifteen needs its own case here. Before this table only `list_memories`
# (create=False) and `retain` (is_write=True, via the shared rate-limit
# tests) were pinned; the other 13 create=False flags and 7 is_write flags
# were each individually deletable with the full suite staying green.
GHOST_EXTRA_KWARGS: dict[str, dict[str, str]] = {
    "retain": {"content": "x"},
    "sync_retain": {"content": "x"},
    "recall": {"query": "x"},
    "reflect": {"query": "x"},
    "get_memory": {"memory_id": GHOST},
    "forget": {"memory_id": GHOST},
    "correct": {"memory_id": GHOST, "content": "x"},
    "restore": {"memory_id": GHOST},
    "get_document": {"document_id": "doc1"},
    "delete_document": {"document_id": "doc1"},
    "get_operation": {"operation_id": GHOST},
    "cancel_operation": {"operation_id": GHOST},
}

MCP_IS_WRITE_TABLE: dict[str, bool] = {
    "retain": True, "sync_retain": True, "recall": True, "reflect": True,
    "list_memories": False, "get_memory": False, "forget": True,
    "correct": True, "restore": True, "list_documents": False,
    "get_document": False, "delete_document": True, "get_operation": False,
    "list_operations": False, "cancel_operation": True,
}

MCP_CREATE_TABLE: dict[str, bool] = {
    "retain": True, "sync_retain": True, "recall": True, "reflect": True,
    "list_memories": False, "get_memory": False, "forget": False,
    "correct": False, "restore": False, "list_documents": False,
    "get_document": False, "delete_document": False, "get_operation": False,
    "list_operations": False, "cancel_operation": False,
}


def test_the_security_tables_cover_every_registered_tool():
    """A sixteenth tool landing in REGISTRY without an entry in both tables
    must fail loudly here, not be silently unverified by the two tests
    below."""
    from memory.mcp.tools import REGISTRY

    assert set(REGISTRY) == set(MCP_IS_WRITE_TABLE) == set(MCP_CREATE_TABLE)


@respx.mock
def test_mcp_is_write_flags_match_the_security_table(call_tool, monkeypatch):
    """Verified by mutation (see plan4-final-report.md): dropping `is_write`
    from any single write tool's `_run` call, over any one of the fifteen,
    makes exactly that tool's case fail here -- not a different one, and not
    all of them.

    One write already consumes the whole limit (=1), so every `is_write=True`
    tool must now refuse with RATE_LIMITED, and every `is_write=False` tool
    must NOT -- regardless of whatever else happens to it (a generic 200 from
    the catch-all mock is enough to let it proceed past the point a wrongly-
    set flag would have blocked it)."""
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()
    respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    key = call_tool.make_user()
    call_tool("retain", key, scope="user", content="warmup")  # consumes the slot

    for name, expect_write in MCP_IS_WRITE_TABLE.items():
        kwargs = {"scope": "user", **GHOST_EXTRA_KWARGS.get(name, {})}
        if expect_write:
            with pytest.raises(MCPToolError) as exc_info:
                call_tool(name, key, **kwargs)
            assert exc_info.value.code == "RATE_LIMITED", name
        else:
            call_tool(name, key, **kwargs)  # must NOT raise RATE_LIMITED


@respx.mock
def test_mcp_create_flags_match_the_security_table(call_tool, session):
    """Verified by mutation: flipping any single tool's `create` makes exactly
    that tool's case fail here. A fresh, never-seen project_slug per tool
    call must be lazily created iff create=True (SPEC §11.3/§16.2), and left
    untouched -- PROJECT_NOT_FOUND, no row -- iff create=False."""
    import uuid

    from memory.errors import ProjectNotFound
    from memory.models import Project

    respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    key = call_tool.make_user()

    for name, expect_create in MCP_CREATE_TABLE.items():
        slug = f"tbl-{uuid.uuid4().hex[:12]}"
        kwargs = {
            "scope": "project", "project_slug": slug,
            **GHOST_EXTRA_KWARGS.get(name, {}),
        }
        if expect_create:
            call_tool(name, key, **kwargs)
        else:
            with pytest.raises(MCPToolError) as exc_info:
                call_tool(name, key, **kwargs)
            assert exc_info.value.code == ProjectNotFound.code, name
        exists = (
            session.query(Project).filter_by(project_slug=slug).count() == 1
        )
        assert exists == expect_create, name


@respx.mock
def test_oversize_content_is_rejected_over_mcp(call_tool, monkeypatch):
    """The size gate used to live only in REST's `_retain`; MCP forwarded an
    oversize body straight to Hindsight. No route is registered on purpose --
    a request that reached Hindsight at all fails via respx's own
    AllMockedAssertionError."""
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("retain", key, scope="user", content="x" * 100)

    assert exc_info.value.code == "CONTENT_TOO_LARGE"


@respx.mock
def test_oversize_recall_query_is_rejected_over_mcp(call_tool, monkeypatch):
    """REST's recall caps body.query (_check_content_size); MCP's twin built
    a bare ScopedRequest that never forwarded query to the check at all. No
    route registered on purpose -- a request that reached Hindsight fails via
    respx's own AllMockedAssertionError."""
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("recall", key, scope="user", query="x" * 100)

    assert exc_info.value.code == "CONTENT_TOO_LARGE"


@respx.mock
def test_oversize_reflect_query_is_rejected_over_mcp(call_tool, monkeypatch):
    """reflect spends model tokens on a server-level credential with no
    per-user cost attribution (SPEC §19.4) -- the same cap REST's reflect
    already carries."""
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("reflect", key, scope="user", query="x" * 100)

    assert exc_info.value.code == "CONTENT_TOO_LARGE"


@respx.mock
def test_oversize_forget_reason_is_rejected_over_mcp(call_tool, monkeypatch):
    """reason is caller free text forwarded verbatim to Hindsight; rejected
    before the memory_id (which does not need to exist) is ever looked up."""
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "forget", key, scope="user", memory_id=GHOST, reason="x" * 100
        )

    assert exc_info.value.code == "CONTENT_TOO_LARGE"


@respx.mock
def test_oversize_list_memories_q_is_rejected_over_mcp(call_tool, monkeypatch):
    """q carries the same embedding-spend risk class as recall's query."""
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("list_memories", key, scope="user", q="x" * 100)

    assert exc_info.value.code == "CONTENT_TOO_LARGE"


@respx.mock
def test_oversize_list_documents_q_is_rejected_over_mcp(call_tool, monkeypatch):
    """q carries the same embedding-spend risk class as recall's query."""
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("list_documents", key, scope="user", q="x" * 100)

    assert exc_info.value.code == "CONTENT_TOO_LARGE"


@respx.mock
def test_a_bogus_update_mode_on_retain_is_rejected_not_blamed_on_hindsight_over_mcp(
    call_tool,
):
    """MCP built a bare ScopedRequest and skipped RetainRequest's own
    validators entirely, so a bogus update_mode reached Hindsight and came
    back as a 502-shaped HINDSIGHT_ERROR blaming the backend for the
    caller's typo. No route registered on purpose -- see the oversize test
    above."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("retain", key, scope="user", content="x", update_mode="bogus")

    assert exc_info.value.code == "INVALID_REQUEST"


@respx.mock
def test_a_non_uuid_operation_id_on_retain_is_rejected_not_blamed_on_hindsight_over_mcp(
    call_tool,
):
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("retain", key, scope="user", content="x", operation_id="retry-1")

    assert exc_info.value.code == "INVALID_REQUEST"


@respx.mock
def test_append_without_a_document_id_over_mcp_is_rejected_not_blamed_on_hindsight(
    call_tool,
):
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("retain", key, scope="user", content="x", update_mode="append")

    assert exc_info.value.code == "INVALID_REQUEST"


def test_list_memories_rejects_a_bogus_state_not_blamed_on_hindsight_over_mcp(
    call_tool,
):
    """MCP built a bare ScopedRequest for list_memories and skipped
    ListMemoriesRequest's own Literal["valid","invalidated"] bound entirely,
    so a bogus state reached Hindsight and came back as a 502-shaped
    HINDSIGHT_ERROR blaming the backend for the caller's typo -- the REST
    twin (test_curation_api.py's equivalent) answers the same input with a
    typed 422. No route registered on purpose: a request that reached
    Hindsight at all fails via respx's own AllMockedAssertionError."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("list_memories", key, scope="user", state="anything-else")

    assert exc_info.value.code == "INVALID_REQUEST"


def test_list_memories_rejects_a_negative_limit_over_mcp(call_tool):
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("list_memories", key, scope="user", limit=-1)

    assert exc_info.value.code == "INVALID_REQUEST"


def test_list_documents_rejects_a_negative_limit_over_mcp(call_tool):
    """Same fix as list_memories, for ListDocumentsRequest's Field(ge=0)."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("list_documents", key, scope="user", limit=-1)

    assert exc_info.value.code == "INVALID_REQUEST"


def test_list_operations_rejects_a_negative_offset_over_mcp(call_tool):
    """Same fix as list_memories, for ListOperationsRequest's Field(ge=0)."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("list_operations", key, scope="user", offset=-1)

    assert exc_info.value.code == "INVALID_REQUEST"


@respx.mock
def test_sync_retain_returns_the_real_upstream_result(call_tool):
    """Pins sync_retain's body against a `return ToolResult(result={})`
    mutation -- no prior test called sync_retain by name at all."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_sync"})
    )
    key = call_tool.make_user()

    result = call_tool("sync_retain", key, scope="user", content="x")

    assert result.result == {"operation_id": "op_sync"}


@respx.mock
def test_sync_retain_is_actually_synchronous_unlike_retain(call_tool):
    """Pins the sync/async distinction itself -- sync_retain's entire reason
    to exist -- against a mutation that flips its `is_async` to True."""
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op"})
    )
    key = call_tool.make_user()

    call_tool("sync_retain", key, scope="user", content="x")
    assert json.loads(route.calls.last.request.read())["async"] is False

    call_tool("retain", key, scope="user", content="y")
    assert json.loads(route.calls.last.request.read())["async"] is True


def test_sync_retain_carries_no_idempotent_hint():
    """sync_retain and retain have identical write semantics -- two calls
    with no document_id write two separate memories -- so neither is
    idempotent. sync_retain used to advertise `idempotentHint=True`, an
    LLM-facing retry hint that would invite a client to retry blindly on a
    timeout and duplicate the write."""
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)

    annotations = mcp._tool_manager.get_tool("sync_retain").annotations
    assert annotations is None or not annotations.idempotent_hint


@respx.mock
def test_forget_invalidates_rather_than_deleting(call_tool):
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    key = call_tool.make_user()

    call_tool("forget", key, scope="user", memory_id=GHOST, reason="wrong")

    assert b'"state":"invalidated"' in route.calls.last.request.read()


@respx.mock
def test_restore_reverts_it(call_tool):
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    key = call_tool.make_user()

    call_tool("restore", key, scope="user", memory_id=GHOST)

    assert b'"state":"valid"' in route.calls.last.request.read()


@respx.mock
def test_correct_replaces_the_text(call_tool):
    _mock_bank()
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    key = call_tool.make_user()

    call_tool("correct", key, scope="user", memory_id=GHOST, content="fixed")

    assert b'"text":"fixed"' in route.calls.last.request.read()


@respx.mock
def test_correct_rejects_blank_content_at_the_boundary_over_mcp(call_tool):
    """MCP built a bare ScopedRequest for correct and skipped CorrectRequest's
    own min_length=1/_not_blank bound entirely, so a blank correct reached
    Hindsight and came back as 409 MEMORY_NOT_CURATABLE -- telling the caller
    the memory is a derived observation when it simply sent nothing (review
    finding I5, reopened as F1). Reverting the MCP `correct` tool's
    body_factory back to a bare ScopedRequest turns this red: the route below
    gets called and no MCPToolError is raised at all. Route registered (not
    omitted) specifically so the assertion that it was NEVER called is a real
    check, not a byproduct of respx.mock's AllMockedAssertionError."""
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("correct", key, scope="user", memory_id=GHOST, content="   ")

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.code != "MEMORY_NOT_CURATABLE"
    assert route.call_count == 0


@respx.mock
def test_list_memories_reaches_the_list_endpoint(call_tool):
    _mock_bank()
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list"
    ).mock(return_value=httpx.Response(200, json={"items": []}))
    key = call_tool.make_user()

    call_tool("list_memories", key, scope="user")

    assert route.call_count == 1


@respx.mock
def test_get_memory_reaches_the_memory_endpoint(call_tool):
    _mock_bank()
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    key = call_tool.make_user()

    call_tool("get_memory", key, scope="user", memory_id=GHOST)

    assert route.call_count == 1


@respx.mock
def test_a_curation_tool_does_not_create_a_project(call_tool, session):
    from memory.errors import ProjectNotFound
    from memory.models import Project

    _mock_bank()
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "list_memories", key, scope="project", project_slug="never-seen"
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert session.query(Project).filter_by(project_slug="never-seen").count() == 0


@respx.mock
def test_idor_a_curation_tool_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    curate = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "correct", alice, scope="project", project_slug="payments",
            memory_id=GHOST, content="mine now",
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert curate.call_count == 0


@respx.mock
def test_document_id_with_colons_and_slashes_reaches_hindsight_verbatim(call_tool):
    _mock_bank()
    doc_id = "github:acme/payments-api:pr:382"
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*"
    ).mock(return_value=httpx.Response(200, json={"id": doc_id}))
    key = call_tool.make_user()

    call_tool("get_document", key, scope="user", document_id=doc_id)

    assert str(route.calls.last.request.url).endswith(f"/documents/{doc_id}")


@respx.mock
def test_list_documents_reaches_the_documents_endpoint(call_tool):
    _mock_bank()
    # The query string is optional in this pattern on purpose: the assertion
    # is "it reached the documents endpoint", and the reduced shape now sends
    # a default `?limit=`. test_list_tools_default_to_a_small_page pins that.
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents(\?.*)?$"
    ).mock(return_value=httpx.Response(200, json={"items": []}))
    key = call_tool.make_user()

    call_tool("list_documents", key, scope="user")

    assert route.call_count == 1


@respx.mock
def test_a_traversal_shaped_document_id_is_refused_with_no_upstream_call(call_tool):
    from memory.errors import DocumentNotFound

    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*").mock(
        return_value=httpx.Response(200, json={"id": "x"})
    )
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "get_document", key, scope="user",
            document_id="../../../../v1/default/banks/OTHER/memories",
        )

    assert exc_info.value.code == DocumentNotFound.code
    assert route.call_count == 0


@respx.mock
def test_delete_document_reaches_the_delete_endpoint(call_tool):
    _mock_bank()
    doc_id = "github:acme/payments-api:pr:382"
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))
    key = call_tool.make_user()

    call_tool("delete_document", key, scope="user", document_id=doc_id)

    assert str(route.calls.last.request.url).endswith(f"/documents/{doc_id}")


@respx.mock
def test_idor_delete_document_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    delete = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "delete_document", alice, scope="project", project_slug="payments",
            document_id="some-doc",
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert delete.call_count == 0


@respx.mock
def test_cancel_operation_reaches_delete_not_the_delete_subpath(call_tool):
    _mock_bank()
    op_id = GHOST
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}$"
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))
    key = call_tool.make_user()

    call_tool("cancel_operation", key, scope="user", operation_id=op_id)

    assert route.call_count == 1
    assert str(route.calls.last.request.url).endswith(f"/operations/{op_id}")
    assert not str(route.calls.last.request.url).endswith("/delete")


@respx.mock
def test_get_operation_reaches_the_operation_endpoint(call_tool):
    _mock_bank()
    op_id = GHOST
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}$"
    ).mock(return_value=httpx.Response(200, json={"status": "completed"}))
    key = call_tool.make_user()

    call_tool("get_operation", key, scope="user", operation_id=op_id)

    assert route.call_count == 1


@respx.mock
def test_list_operations_reaches_the_operations_endpoint(call_tool):
    _mock_bank()
    # Query string optional, same reason as the documents case above.
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations(\?.*)?$"
    ).mock(return_value=httpx.Response(200, json={"items": []}))
    key = call_tool.make_user()

    call_tool("list_operations", key, scope="user")

    assert route.call_count == 1


@respx.mock
def test_idor_cancel_operation_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    cancel = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/[^/]+$"
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "cancel_operation", alice, scope="project", project_slug="payments",
            operation_id=GHOST,
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert cancel.call_count == 0


@respx.mock
def test_a_master_key_is_refused_by_a_real_tool_call(call_tool, master_headers):
    """Same invariant as test_mcp_server.py::test_a_master_key_is_refused_over_mcp,
    proven through a real tool call: `_run` must surface `tool_session`'s
    Forbidden as an MCPToolError, not let it escape raw, exactly like any
    other DomainError raised inside the pipeline."""
    master_key = master_headers["Authorization"].removeprefix("Bearer ")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("recall", master_key, scope="project", project_slug="payments", query="x")

    assert exc_info.value.code == "FORBIDDEN"


@respx.mock
def test_idor_get_memory_cannot_reach_an_unauthorized_bank(call_tool):
    """SPEC §20.1, the MCP twin of test_curation_api.py's equivalent -- `_run`
    resolves the bank on its own line for every tool, so each needs its own
    case. memory_id must be a syntactically valid UUID (GHOST): the client's
    local `_require_uuid` guard would otherwise zero out call_count for a
    malformed id whether or not the bank check ran at all."""
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    get = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}$"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "get_memory", alice, scope="project", project_slug="payments",
            memory_id=GHOST,
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert get.call_count == 0


@respx.mock
def test_idor_forget_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    forget = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}$"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "forget", alice, scope="project", project_slug="payments",
            memory_id=GHOST,
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert forget.call_count == 0


@respx.mock
def test_idor_restore_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    restore = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}$"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "restore", alice, scope="project", project_slug="payments",
            memory_id=GHOST,
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert restore.call_count == 0


@respx.mock
def test_idor_get_document_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    get_doc = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*"
    ).mock(return_value=httpx.Response(200, json={"id": "doc1"}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "get_document", alice, scope="project", project_slug="payments",
            document_id="doc1",
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert get_doc.call_count == 0


@respx.mock
def test_idor_get_operation_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectAccessDenied

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    get_op = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{GHOST}$"
    ).mock(return_value=httpx.Response(200, json={"status": "completed"}))
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool("retain", juan, scope="project", project_slug="payments", content="x")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "get_operation", alice, scope="project", project_slug="payments",
            operation_id=GHOST,
        )

    assert exc_info.value.code == ProjectAccessDenied.code
    assert get_op.call_count == 0


@respx.mock
def test_idor_scenario_z_a_known_secondary_id_from_an_unreachable_bank_is_just_not_found(
    call_tool, session
):
    """SPEC §24 scenario Z: 'Alice knows a memory_id ... from a project she
    cannot access. Supplying it under a scope she CAN access does not grant
    access: resolution happens only inside the already-authorized bank.'
    Unlike the ProjectAccessDenied cases above (Alice names a scope she
    cannot reach), this is Alice naming a scope she CAN reach (her own),
    carrying an id that only means something in someone else's bank -- the
    id is simply absent in hers, so it is an ordinary MEMORY_NOT_FOUND, and
    the victim's bank is never touched."""
    from memory.errors import MemoryNotFound
    from memory.models import User

    call_tool.make_user()  # juan -- only his bank_id is needed below
    juan_id = call_tool.last_user_id
    alice_key = call_tool.make_user()
    alice_id = call_tool.last_user_id
    juan_bank_id = session.get(User, juan_id).bank_id
    alice_bank_id = session.get(User, alice_id).bank_id

    juan_route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/{juan_bank_id}/memories/{GHOST}$"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST, "secret": "juan's"}))
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/{alice_bank_id}/memories/{GHOST}$"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("get_memory", alice_key, scope="user", memory_id=GHOST)

    assert exc_info.value.code == MemoryNotFound.code
    assert juan_route.call_count == 0


EXPECTED_TOOLS = {
    "retain", "sync_retain", "recall", "reflect",
    "list_memories", "get_memory", "forget", "correct", "restore",
    "list_documents", "get_document", "delete_document",
    "get_operation", "list_operations", "cancel_operation",
}

# SPEC §11.6 and §11.7. Each is excluded for a stated reason: whole-bank
# destruction an LLM would reach for when it decides memory is "stale"; bank
# configuration that is policy for every user of a project; shared, persistent
# state that steers future agents; a "dry run" whose name invites the model to
# treat it as free when it costs exactly the same as the real thing.
FORBIDDEN_TOOLS = {
    "clear_memories", "delete_bank", "get_bank", "update_bank", "get_bank_stats",
    "list_banks", "create_bank", "dry_run_refresh", "dry-run-refresh",
    "list_tags", "retry_operation", "delete_operation",
    "create_mental_model", "get_mental_model", "list_mental_models",
    "update_mental_model", "refresh_mental_model", "clear_mental_model",
    "delete_mental_model",
    "create_directive", "list_directives", "delete_directive",
    "update_project", "transfer_project", "create_project",
    "create_user", "create_group", "create_key",
    "list_users", "list_keys", "revoke_key",
}


@pytest.mark.anyio
async def test_the_advertised_tool_surface_is_exactly_the_spec_set():
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    advertised = {t.name for t in await mcp.list_tools()}

    assert advertised == EXPECTED_TOOLS
    assert advertised & FORBIDDEN_TOOLS == set()


@pytest.mark.anyio
async def test_no_tool_input_schema_exposes_bank_tenant_or_user_id():
    """SPEC §11.1: 'The LLM never supplies bank_id, tenant_id, its
    authenticated user_id, ownership, or any authorization data.' Nothing
    previously asserted this over the actual advertised schemas -- this
    covers all fifteen permanently, so a future tool cannot reintroduce one
    of these as a parameter without a test failing."""
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    forbidden = {"bank_id", "tenant_id", "user_id"}

    tools = await mcp.list_tools()
    # Assert the count FIRST. Without it this loop iterates an empty list and
    # passes green, while the docstring above claims it "covers all fifteen
    # permanently" -- a register() regression returning nothing would satisfy
    # it exactly as well as a correct surface does.
    assert len(tools) == len(MCP_IS_WRITE_TABLE), [t.name for t in tools]

    for tool in tools:
        properties = tool.input_schema.get("properties", {})
        assert forbidden.isdisjoint(properties), (tool.name, sorted(properties))


def test_an_unauthenticated_oversize_retain_is_refused_before_validation(app):
    """`tool_session` is the only thing that reads the Authorization header;
    `body_factory` used to run before it, so an unauthenticated caller's
    oversize content was validated (and rejected with the configured
    MEMORY_MAX_CONTENT_BYTES value) before authentication ever ran. REST
    resolves current_principal before any handler body runs -- MCP must
    match that ordering (2026-08-23 review, R3-I-6/M-2)."""
    from memory.mcp.tools import REGISTRY, MCPToolError

    class NoAuth:
        headers: ClassVar = {}

    with pytest.raises(MCPToolError) as exc_info:
        REGISTRY["retain"](scope="user", content="x" * 300_000, ctx=NoAuth())

    assert exc_info.value.code == "UNAUTHORIZED"
    assert "256000" not in str(exc_info.value), (
        "the configured content limit leaked to an unauthenticated caller"
    )


@respx.mock
def test_a_reserved_metadata_key_under_project_scope_creates_no_project(
    call_tool, session
):
    """SPEC §13.4: INVALID_METADATA and NOTHING IS WRITTEN. MCP used to commit
    the project row before `provenance.build` ran, so a refused retain still
    permanently created and owned the project it named -- unrecoverable,
    since invariant 8 makes a slug unique across live AND retired names. The
    existing regression test (`test_a_reserved_metadata_key_is_refused_and_
    nothing_is_retained`) uses scope="user", which has no row to create, so
    it could not see this (2026-08-23 review, R3-I-2)."""
    from memory.models import Project

    key = call_tool.make_user()
    slug = "reserved-key-probe"

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "retain", key, scope="project", project_slug=slug,
            content="x", metadata={"user_id": "someone"},
        )

    assert exc_info.value.code == "INVALID_METADATA"
    assert (
        session.query(Project).filter_by(project_slug=slug).count() == 0
    ), "the refused retain committed a project row anyway"


def test_oversize_metadata_under_project_scope_creates_no_project(
    call_tool, session, monkeypatch
):
    """2026-08-23 review, finding 3: `provenance.build`'s metadata size cap
    ran only inside `call`, AFTER `tc.db.commit()` -- `body_factory` moved
    the reserved-key check (`provenance.check_reserved`) earlier for exactly
    this reason (SPEC §13.4) but left the size cap behind, so an oversize
    metadata still permanently squatted the project slug it named
    (invariant 8: slugs are unique across live AND retired names, never
    recoverable). Reproduced live as `mcp-squat` staying unreclaimable while
    REST's twin correctly left no project row. Mirrors
    test_a_reserved_metadata_key_under_project_scope_creates_no_project.
    """
    from memory.config import get_settings
    from memory.models import Project

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()

    key = call_tool.make_user()
    slug = "mcp-oversize-metadata-squat"

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "retain", key, scope="project", project_slug=slug,
            content="x", metadata={"note": "y" * 300},
        )

    assert exc_info.value.code == "CONTENT_TOO_LARGE"
    assert (
        session.query(Project).filter_by(project_slug=slug).count() == 0
    ), "the refused retain committed a project row anyway"


@respx.mock
def test_a_malformed_upstream_body_logs_once_not_twice(call_tool, caplog):
    """2026-08-23 review, finding 6: the inner `except ValidationError`
    handler for a non-JSON-object upstream body fires INSIDE the outer
    `try`, so `except Exception` used to catch the re-raised MCPToolError
    too, log "unhandled MCP tool error" a SECOND time, and re-raise an
    equivalent INTERNAL_ERROR. The inner handler is load-bearing -- it
    stops `except ValidationError` mislabelling this as INVALID_REQUEST --
    only the duplicate log line was the bug.
    """
    import logging

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json=["not", "an", "object"])
    )
    key = call_tool.make_user()

    with (
        caplog.at_level(logging.ERROR, logger="memory.mcp"),
        pytest.raises(MCPToolError) as exc_info,
    ):
        call_tool("recall", key, scope="user", query="hi")

    assert exc_info.value.code == "INTERNAL_ERROR"
    messages = [r.message for r in caplog.records]
    assert messages.count("upstream response was not a JSON object") == 1
    assert messages.count("unhandled MCP tool error") == 0


def test_invalid_request_names_the_offending_field(call_tool):
    """`_validation_message` used to join only `e['msg']`, so a multi-field
    failure read like two unattributed sentences with no indication of WHICH
    argument was wrong. `loc` names the caller's own field -- never server
    state -- so including it is safe and makes INVALID_REQUEST actionable."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("retain", key, scope="not-a-real-scope", content="x")

    assert exc_info.value.code == "INVALID_REQUEST"
    assert "scope" in str(exc_info.value)


@respx.mock
def test_list_tools_default_to_a_small_page(call_tool):
    """Hindsight's own default is 100 rows. A first look at a bank should not
    spend an agent's context on two orders of magnitude more than it needs,
    and `total` in the envelope keeps the rest one paged call away."""
    _mock_bank()
    key = call_tool.make_user()
    for tool, path in (
        ("list_memories", "memories/list"),
        ("list_documents", "documents"),
        ("list_operations", "operations"),
    ):
        route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/{path}(\?.*)?$").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        call_tool(tool, key, scope="user")

        assert "limit=20" in str(route.calls.last.request.url), tool


@respx.mock
def test_an_explicit_limit_is_never_overridden(call_tool):
    """The default replaces "unspecified" only -- it caps nothing the caller
    asked for, at any size the PageLimit bound allows."""
    _mock_bank()
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list(\?.*)?$"
    ).mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
    key = call_tool.make_user()

    call_tool("list_memories", key, scope="user", limit=500)

    assert "limit=500" in str(route.calls.last.request.url)


@respx.mock
def test_verbose_restores_the_upstream_default_page(call_tool):
    """verbose is the escape hatch to the old behaviour whole, and the old
    behaviour was to send no limit at all and let Hindsight decide."""
    _mock_bank()
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list(\?.*)?$"
    ).mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
    key = call_tool.make_user()

    call_tool("list_memories", key, scope="user", verbose=True)

    assert "limit=" not in str(route.calls.last.request.url)


@respx.mock
def test_recall_asks_hindsight_not_to_build_the_entity_map(call_tool):
    """Disabling include.entities upstream saves assembling the map, not just
    shipping it -- which dropping the key on the way out would not."""
    _mock_bank()
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall"
    ).mock(return_value=httpx.Response(200, json={"results": []}))
    key = call_tool.make_user()

    call_tool("recall", key, scope="user", query="deps")
    assert json.loads(route.calls.last.request.content)["include"] == {"entities": None}

    call_tool("recall", key, scope="user", query="deps", verbose=True)
    assert "include" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_verbose_returns_the_upstream_payload_untouched(call_tool):
    _mock_bank()
    upstream = {
        "results": [
            {
                "id": "m1",
                "text": "we use uv",
                "chunk_id": "c1",
                "tags": [],
                "entities": ["uv"],
                "occurred_start": "2026-01-15T10:30:00Z",
                "occurred_end": "2026-01-15T10:30:00Z",
                "scores": {"final": 0.8123, "reranker": 0.42},
            }
        ],
        "entities": {"uv": {"canonical_name": "uv"}},
    }
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json=upstream)
    )
    key = call_tool.make_user()

    assert call_tool("recall", key, scope="user", query="deps", verbose=True).result == upstream

    reduced = call_tool("recall", key, scope="user", query="deps").result
    assert reduced == {
        "results": [
            {
                "id": "m1",
                "text": "we use uv",
                "occurred_start": "2026-01-15T10:30:00Z",
                "scores": {"final": 0.81},
            }
        ]
    }


@respx.mock
def test_the_envelope_omits_the_slug_fields_when_no_rename_was_followed(call_tool):
    """Three nulls on nearly every call, serialized twice per response by the
    SDK (structured output plus the text block mirroring it). They are still
    emitted when they carry something -- test_a_retired_slug_* covers that."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    key = call_tool.make_user()

    dumped = call_tool("recall", key, scope="user", query="deps").model_dump()

    assert dumped == {"result": {"results": []}}
