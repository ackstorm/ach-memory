import hashlib
import json
from typing import ClassVar

import httpx
import pytest
import respx

from memory.mcp.tools import MCPToolError
from tests.conftest import IDENTITY_HEADER, create_user

BASE = "http://hindsight.test"
GHOST = "22222222-2222-2222-2222-222222222222"
MM_GHOST = "mm_" + "0" * 32
MM_REQUIRED_TAGS = ["schema:ach-retain-v1", "validity:indefinite"]


def _retain_kwargs(**overrides) -> dict:
    """The minimum typed-retain shape every retain/sync_retain call needs
    (memory_type/basis/trigger/evidence), merged with per-test overrides."""
    kwargs = {
        "memory_type": "fact",
        "basis": "human_explicit",
        "trigger": "agent_proactive",
        "evidence": [{"kind": "user_quote", "raw": "Source excerpt."}],
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture
def call_tool(app, client, session, tenant):
    """Invoke a registered tool the way the SDK would, with real headers.

    Goes through the same registry the transport uses, so a tool that is not
    registered raises here exactly as it would over the wire.
    """
    from memory.mcp import tools as tool_module

    def _make_user() -> str:
        """A distinct caller, as an external identity provider asserts one.

        Still returns a single opaque string -- every call site below passes
        it straight back as the caller's credential -- but the string is now
        the IdP's subject, not a key this service minted. There is no
        `POST /v1/users` and no key endpoint left to call.
        """
        user = create_user(client, session)
        _call.last_user_id = user["user_id"]
        return user["subject"]

    def _seed_project(key: str, slug: str) -> None:
        """Typed retain is existing-only (create=False); several tests here
        need an already-owned project to exercise curation/IDOR behavior
        against, so they create it directly rather than relying on retain's
        old lazy-creation side effect."""
        response = client.post(
            "/v1/projects",
            json={"project_slug": slug},
            headers=_headers_for(key),
        )
        assert response.status_code == 201, response.text

    def _call(tool_name: str, key: str, **kwargs):
        # `tool_name`, not `name`: a mental-model tool has its own `name`
        # kwarg (the model's display name), which collided with this
        # fixture's own lookup parameter under **kwargs expansion.
        class _Ctx:
            headers: ClassVar = _headers_for(key)

        return tool_module.REGISTRY[tool_name](ctx=_Ctx(), **kwargs)

    _call.make_user = _make_user
    _call.seed_project = _seed_project
    _call.headers = _headers_for
    return _call


def _headers_for(key: str) -> dict[str, str]:
    """The one place a caller token becomes headers, for MCP and REST alike."""
    return {IDENTITY_HEADER: key}


@respx.mock
def test_retain_reaches_the_callers_own_bank(call_tool, session):
    """Asserts the CALLER's real bank id, not just the `user_` scope prefix --
    a substring match would stay green even if the request were routed to a
    different user's `user_`-prefixed bank."""
    from memory.models import User

    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    key = call_tool.make_user()
    bank_id = session.get(User, call_tool.last_user_id).bank_id

    result = call_tool("retain", key, scope="user", content="uv, not pip", **_retain_kwargs())

    assert result.result["status"] == "accepted"
    assert result.result["document_id"].startswith("ach-retain-")
    assert f"banks/{bank_id}/" in str(route.calls.last.request.url)


@pytest.mark.parametrize("tool", ["retain", "sync_retain"])
@respx.mock
def test_retain_tools_always_use_the_fixed_ach_exact_v1_shape(call_tool, tool):
    """v0.4.0: exact typed retain always selects the frozen `ach-exact-v1`
    strategy and server-derived tags. Never overridable by any MCP argument,
    and evidence never reaches the wire payload."""
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/.+").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    key = call_tool.make_user()

    call_tool(
        tool,
        key,
        scope="user",
        content="uv, not pip",
        **_retain_kwargs(memory_type="convention", basis="agent_verified"),
    )

    item = json.loads(route.calls.last.request.read())["items"][0]
    assert item["tags"] == [
        "type:convention",
        "basis:agent_verified",
        "schema:ach-retain-v1",
        "validity:indefinite",
    ]
    assert item["strategy"] == "ach-exact-v1"
    assert "evidence" not in item
    assert "observation_scopes" not in item


@respx.mock
def test_retain_appends_caller_tags_to_the_derived_ones(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    key = call_tool.make_user()

    call_tool(
        "retain",
        key,
        scope="user",
        content="uv, not pip",
        tags=["Repo:Group/App"],
        **_retain_kwargs(),
    )

    item = json.loads(route.calls.last.request.read())["items"][0]
    assert item["tags"] == [
        "type:fact",
        "basis:human_explicit",
        "schema:ach-retain-v1",
        "validity:indefinite",
        "repo:group/app",
    ]


@respx.mock
def test_retain_refuses_a_reserved_tag_namespace(call_tool):
    """The derived four are server-owned. A caller must not be able to forge
    a type:, basis:, schema: or validity: tag."""
    _mock_bank()
    key = call_tool.make_user()
    with pytest.raises(MCPToolError):
        call_tool(
            "retain",
            key,
            scope="user",
            content="x",
            tags=["schema:ach-retain-v1"],
            **_retain_kwargs(),
        )


@respx.mock
def test_a_tool_never_returns_a_bank_id(call_tool, session):
    """Both the literal `bank_id` key and its use as a chunk_id substring
    (measured against a live server, SPEC inv. 29 -- see
    test_curation_api.py::test_bank_id_embedded_in_chunk_id_is_redacted) must
    be gone from a tool's result. The mock uses the CALLER's real bank_id --
    not a placeholder string -- because `_strip_bank_id`'s substring redaction
    only ever matches the bank_id it is handed, exactly as a live Hindsight
    response would.

    Recall has one shape, the reduced one, which drops `chunk_id` outright;
    the substring redaction on the payload where the field survives is
    covered by the REST test named above. This one pins that neither the key
    nor the substring leaks through the tool.
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

    result = call_tool("recall", key, scope="user", query="deps")
    assert bank_id not in str(result.model_dump())
    assert "chunk_id" not in str(result.result)


@respx.mock
def test_mental_model_tools_never_return_a_physical_or_upstream_id(call_tool, session):
    """SPEC §7.2: public callers address a model by logical scope and
    model_key; upstream mental-model ids and physical bank ids are
    implementation details MCP must never surface, same as REST."""
    from memory.models import User

    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201,
            json={
                "mental_model_id": "mm-upstream-secret",
                "operation_id": "6f8636ea-bba9-46b0-a253-a9b42a5c8bd2",
            },
        )
    )
    key = call_tool.make_user()
    bank_id = session.get(User, call_tool.last_user_id).bank_id

    created = call_tool(
        "create_mental_model",
        key,
        scope="user",
        name="n",
        source_query="q",
        source_tags=["repo:group/app"],
        max_tokens=512,
        trigger={"mode": "delta"},
    )

    assert created.result["model_key"].startswith("mm_")
    for result in (created.result, str(created.model_dump())):
        text = str(result)
        assert "mm-upstream-secret" not in text
        assert bank_id not in text
        assert "upstream_model_id" not in text
        assert "bank_id" not in text


@respx.mock
def test_mcp_refresh_and_delete_forward_the_callers_operation_id_to_the_ledger(call_tool, session):
    """The bug this closes: `refresh_mental_model`/`delete_mental_model`'s MCP
    body factories built a bare `ScopedRequest`, discarding the caller's
    `operation_id` before it ever reached the service layer's idempotency
    ledger -- unlike `update_mental_model`, which already threaded it
    through `UpdateMentalModelRequest`."""
    from memory.models import MentalModelMutation

    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201, json={"mental_model_id": "mm-upstream-1", "operation_id": "op-create"}
        )
    )
    key = call_tool.make_user()

    created = call_tool(
        "create_mental_model",
        key,
        scope="user",
        name="n",
        source_query="q",
        source_tags=["repo:group/app"],
        max_tokens=512,
        trigger={"mode": "delta"},
    )
    model_key = created.result["model_key"]

    respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1/refresh$"
    ).mock(return_value=httpx.Response(200, json={"operation_id": "op-refresh"}))
    refresh_operation_id = "22222222-2222-4222-8222-222222222222"
    call_tool(
        "refresh_mental_model",
        key,
        scope="user",
        model_key=model_key,
        operation_id=refresh_operation_id,
    )

    mutation = (
        session.query(MentalModelMutation).filter_by(model_key=model_key, action="refresh").one()
    )
    assert mutation.operation_id == refresh_operation_id

    respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1$").mock(
        return_value=httpx.Response(200, json={"deleted": True})
    )
    delete_operation_id = "33333333-3333-4333-8333-333333333333"
    call_tool(
        "delete_mental_model",
        key,
        scope="user",
        model_key=model_key,
        operation_id=delete_operation_id,
    )

    mutation = (
        session.query(MentalModelMutation).filter_by(model_key=model_key, action="delete").one()
    )
    assert mutation.operation_id == delete_operation_id


@respx.mock
def test_a_tool_cannot_reach_another_users_project(call_tool):
    """`recall` is one of the collection reads that map an absent project to
    empty (decision 3): resolution hides the existing project behind the same
    empty result as an absent slug, never a distinguishing error -- the
    oracle guard Task 5 exists for."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    result = call_tool("recall", alice, scope="project", project_slug="payments", query="x")

    assert result.result == {"hits": [], "truncated": False}


@respx.mock
def test_project_scope_retain_forwards_a_retired_slug_to_the_same_bank(call_tool, client):
    """Mirrors test_memory_api.py's equivalent: after a rename, a retain
    against the OLD slug must still forward to the same project bank. The
    typed response (v0.4.0) carries no resolved_from/notice fields -- it is
    built entirely from ACH's own DB row -- so only the forwarding itself is
    asserted here, not a surfaced rename notice."""
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    key = call_tool.make_user()
    headers = _headers_for(key)
    assert (
        client.post(
            "/v1/projects", json={"project_slug": "payments-api"}, headers=headers
        ).status_code
        == 201
    )

    call_tool(
        "retain",
        key,
        scope="project",
        project_slug="payments-api",
        content="x",
        **_retain_kwargs(),
    )
    original_bank_url = str(route.calls.last.request.url)

    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-service"},
        headers=headers,
    )

    call_tool(
        "retain",
        key,
        scope="project",
        project_slug="payments-api",
        content="y",
        **_retain_kwargs(),
    )

    assert str(route.calls.last.request.url) == original_bank_url


@respx.mock
def test_a_first_touch_retain_says_it_created_the_project(call_tool):
    """MCP twin of test_memory_api.py's: QA F-10, a typo'd project_slug
    silently minted a bank. Creation stays lazy (decision 1) but the
    envelope now announces it, once."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    key = call_tool.make_user()

    first = call_tool(
        "retain", key, scope="project", project_slug="brand-new-slug-2",
        content="x", **_retain_kwargs(),
    )
    assert first.notice == "PROJECT_CREATED"

    # Each call mints its own operation_id, so this is a second real write,
    # not an idempotent replay of the first.
    second = call_tool(
        "retain", key, scope="project", project_slug="brand-new-slug-2",
        content="y", **_retain_kwargs(),
    )
    assert second.notice is None


@respx.mock
def test_an_unexpected_exception_is_sanitized_and_never_echoed(call_tool, session, monkeypatch):
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


@respx.mock
def test_reflect_sends_caller_tags_upstream(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "uv"})
    )
    key = call_tool.make_user()

    call_tool("reflect", key, scope="user", query="deps?", tags_filter=["Repo:Group/App"])

    body = json.loads(route.calls.last.request.read())
    # Grouped, and carrying the server's own scope alongside the caller's.
    # Sending the caller's tags ALONE is what let `reflect` reason over the
    # whole bank while `recall` stayed inside the retained corpus.
    assert "tags" not in body and "tags_match" not in body
    assert body["tag_groups"] == [
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
        {"tags": ["repo:group/app"], "match": "all_strict"},
    ]
    assert body["fact_types"] == ["world", "observation"]


@respx.mock
def test_reflect_without_caller_tags_still_scopes_to_the_retained_corpus(call_tool):
    """An untagged reflect is not an unscoped one.

    This used to assert that no tag keys were sent at all, which was true and
    was the bug: with nothing sent, upstream applied no filter and `reflect`
    reasoned over the entire bank -- `experience` facts and anything not
    written by this service's retain path included -- while `recall` on the
    same bank stayed inside `schema:ach-retain-v1`/world+observation.
    """
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "uv"})
    )
    key = call_tool.make_user()

    call_tool("reflect", key, scope="user", query="deps?")

    body = json.loads(route.calls.last.request.read())
    assert "tags" not in body and "tags_match" not in body
    # The server's group only: no caller tags means no caller group, not an
    # empty one, which upstream would treat differently again.
    assert body["tag_groups"] == [{"tags": ["schema:ach-retain-v1"], "match": "all_strict"}]
    assert body["fact_types"] == ["world", "observation"]


@respx.mock
def test_reflect_refuses_a_reserved_tag_namespace(call_tool):
    _mock_bank()
    key = call_tool.make_user()
    with pytest.raises(MCPToolError):
        call_tool("reflect", key, scope="user", query="deps?", tags_filter=["schema:x"])


def _mock_bank() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


def _seed_tracked_memory(call_tool, key: str, mem_id: str) -> None:
    """Create an ACH-tracked `RetainedRecord` resolving to `mem_id`, via
    `sync_retain`. Assumes `_mock_bank()` already ran; registers the retain
    routes itself."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/.+").mock(
        return_value=httpx.Response(
            200, json={"status": "completed", "result": {"memory_id": mem_id}}
        )
    )
    result = call_tool("sync_retain", key, scope="user", content="seed claim", **_retain_kwargs())
    assert result.result["status"] == "completed"


def test_create_is_keyword_only_on_run():
    """`create=False` guards against permanently squatting a project slug
    (see resolve_project_bank's docstring); eleven more tools copy `_run`'s
    shape next, and a bare positional `True`/`False` in this slot is the
    exact shape a copy-paste error reintroduces silently."""
    import inspect

    from memory.mcp.memory_tools import _run

    assert inspect.signature(_run).parameters["create"].kind is inspect.Parameter.KEYWORD_ONLY


# One table, both flags, all fifteen tools. Each tool declares `create` and
# `is_write` independently at its own `_run(...)` call site (or, for
# list_documents/list_operations, its `_list_documents`/`_list_operations`
# helper) -- there is no shared literal a single test could pin, so each of
# the fifteen needs its own case here. Before this table only `list_memories`
# (create=False) and `retain` (is_write=True, via the shared rate-limit
# tests) were pinned; the other 13 create=False flags and 7 is_write flags
# were each individually deletable with the full suite staying green.
GHOST_EXTRA_KWARGS: dict[str, dict] = {
    "retain": {"content": "x", **_retain_kwargs()},
    "sync_retain": {"content": "x", **_retain_kwargs()},
    "recall": {"query": "x"},
    "memory_history": {"memory_id": GHOST},
    "reflect": {"query": "x"},
    "get_memory": {"memory_id": GHOST},
    "forget": {"memory_id": GHOST},
    "correct": {"memory_id": GHOST, "content": "x"},
    "restore": {"memory_id": GHOST},
    "get_document": {"document_id": "doc1"},
    "delete_document": {"document_id": "doc1"},
    "get_operation": {"operation_id": GHOST},
    "cancel_operation": {"operation_id": GHOST},
    "create_mental_model": {
        "name": "n",
        "source_query": "q",
        "source_tags": ["repo:group/app"],
        "max_tokens": 512,
        "trigger": {"mode": "delta"},
    },
    "get_mental_model": {"model_key": MM_GHOST},
    "update_mental_model": {"model_key": MM_GHOST, "name": "n2"},
    "refresh_mental_model": {"model_key": MM_GHOST},
    "delete_mental_model": {"model_key": MM_GHOST},
}

MCP_IS_WRITE_TABLE: dict[str, bool] = {
    "retain": True,
    "sync_retain": True,
    "recall": False,
    "memory_history": False,
    "reflect": True,
    "list_memories": False,
    "get_memory": False,
    "forget": True,
    "correct": True,
    "restore": True,
    "list_documents": False,
    "get_document": False,
    "delete_document": True,
    "get_operation": False,
    "list_operations": False,
    "cancel_operation": True,
    "start_working_session": True,
    "set_working_state": True,
    "clear_working_state": True,
    "load_context": False,
    "create_mental_model": True,
    "list_mental_models": False,
    "get_mental_model": False,
    "update_mental_model": True,
    "refresh_mental_model": True,
    "delete_mental_model": True,
    # transfer resolves no Hindsight bank, so it has no _resolve_bank
    # is_write flag -- `projects.transfer` applies the ceiling in the domain
    # instead, which is what makes this True on REST and MCP alike.
    "transfer": True,
}

MCP_CREATE_TABLE: dict[str, bool] = {
    # retain/sync_retain are the one place allowed to lazily create a project
    # (lazy-provisioning plan, decision 1); every other tool stays existing-
    # only, including recall/reflect despite also being is_write=True above.
    "retain": True,
    "sync_retain": True,
    "recall": False,
    "memory_history": False,
    "reflect": False,
    "list_memories": False,
    "get_memory": False,
    "forget": False,
    "correct": False,
    "restore": False,
    "list_documents": False,
    "get_document": False,
    "delete_document": False,
    "get_operation": False,
    "list_operations": False,
    "cancel_operation": False,
    "start_working_session": False,
    "set_working_state": False,
    "clear_working_state": False,
    "load_context": False,
    # Every mental-model tool resolves with create=False (SPEC §7: maintenance
    # over an existing bank, never first-touch project creation).
    "create_mental_model": False,
    "list_mental_models": False,
    "get_mental_model": False,
    "update_mental_model": False,
    "refresh_mental_model": False,
    "delete_mental_model": False,
    "transfer": False,
}

# The COLLECTION reads that map an absent project to their own empty shape
# rather than PROJECT_NOT_FOUND (decision 3) -- readOnlyHint is NOT the
# selector, recall/reflect are both readOnlyHint=False. load_context is a
# member too but is exercised separately (test_context_service.py): its
# resolution doesn't go through _run/_model_run/_read_run at all.
#
# No get-by-id is in here, on purpose. memory_history, get_memory,
# get_document, get_operation and get_mental_model used to soften to a bare
# `{}` -- the one shape a caller cannot branch on -- and now raise
# PROJECT_NOT_FOUND like every other create=False tool, which is what the
# `else` branch of test_mcp_create_flags_match_the_security_table asserts.
READ_TOOLS_EMPTY_RESULT: dict[str, dict] = {
    "recall": {"hits": [], "truncated": False},
    "list_memories": {"items": []},
    "reflect": {"text": "", "usage": {}},
    "list_documents": {"items": []},
    "list_operations": {"items": []},
    "list_mental_models": {"models": [], "unknown_upstream_count": 0},
}
READ_TOOLS = tuple(READ_TOOLS_EMPTY_RESULT)

# Working State tools take no `scope`/generic project kwargs at all -- their
# shape is entirely different from every Hindsight-routed tool -- so the two
# security-table tests below use this as a COMPLETE kwargs override rather
# than merging onto the generic {"scope": ...} base the way GHOST_EXTRA_KWARGS
# merges on top of it for everything else.
WORKING_STATE_KWARGS: dict[str, dict] = {
    "start_working_session": {"workspace_id": "ws_" + "0" * 32, "session_id": "s1"},
    "set_working_state": {
        "workspace_id": "ws_" + "0" * 32,
        "session_id": "s1",
        "session_epoch": 0,
        "checkpoint_seq": 0,
        "objective": "x",
    },
}
CONTEXT_KWARGS = {
    "clear_working_state": {
        "workspace_id": "ws_" + "0" * 32,
        "session_id": "s1",
        "session_epoch": 0,
        "checkpoint_seq": 0,
    },
    "load_context": {"workspace_id": None},
}
# transfer takes no `scope` either -- a project, unlike a memory bank, is
# never user/project-scoped, only addressed directly by slug.
PROJECT_KWARGS = {
    "transfer": {"owner_type": "user", "owner_id": "usr_ghost"},
}


def test_the_security_tables_cover_every_registered_tool():
    """An eighteenth tool landing in REGISTRY without an entry in both tables
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
    call_tool(
        "retain", key, scope="user", content="warmup", **_retain_kwargs()
    )  # consumes the slot

    for name, expect_write in MCP_IS_WRITE_TABLE.items():
        if name == "load_context":
            continue
        if name in WORKING_STATE_KWARGS or name in CONTEXT_KWARGS or name in PROJECT_KWARGS:
            # project_slug need not exist: ratelimit.check() runs before any
            # project resolution, so RATE_LIMITED fires first regardless.
            # transfer is the exception in placement, not in effect: its
            # check lives in `projects.transfer`, AFTER resolution, so a ghost
            # slug raises PROJECT_NOT_FOUND before the ceiling is reached.
            kwargs = {
                "project_slug": "wst-ratelimit",
                **(
                    WORKING_STATE_KWARGS.get(name)
                    or CONTEXT_KWARGS.get(name)
                    or PROJECT_KWARGS[name]
                ),
            }
        else:
            kwargs = {"scope": "user", **GHOST_EXTRA_KWARGS.get(name, {})}
        if expect_write and name != "transfer":
            with pytest.raises(MCPToolError) as exc_info:
                call_tool(name, key, **kwargs)
            assert exc_info.value.code == "RATE_LIMITED", name
        elif name in ("memory_history", "get_mental_model", "transfer"):
            # get_mental_model 404s on the registry lookup before it would
            # reach Hindsight for content -- a ghost model_key genuinely 404s.
            # transfer's own ceiling lives in the domain, after resolution, so
            # a ghost slug reaches PROJECT_NOT_FOUND first; the code is
            # asserted directly rather than by absence.
            with pytest.raises(MCPToolError) as exc_info:
                call_tool(name, key, **kwargs)
            if name == "transfer":
                assert exc_info.value.code == "PROJECT_NOT_FOUND", name
            else:
                # The point of these two is that they raise for their OWN
                # reason and never because the write ceiling caught them.
                # `code == code` asserted nothing here.
                assert exc_info.value.code != "RATE_LIMITED", name
        else:
            call_tool(name, key, **kwargs)  # must NOT raise RATE_LIMITED


@respx.mock
def test_mcp_create_flags_match_the_security_table(call_tool, session):
    """Verified by mutation: flipping any single tool's `create` makes exactly
    that tool's case fail here. A fresh, never-seen project_slug per tool
    call must be lazily created iff create=True (SPEC §11.3/§16.2), and left
    untouched otherwise -- empty result for one of the collection reads
    (decision 3), PROJECT_NOT_FOUND for every other create=False tool, the
    five get-by-id reads included."""
    import uuid

    from memory.errors import ProjectNotFound
    from memory.models import ProjectSlug

    respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    key = call_tool.make_user()

    for name, expect_create in MCP_CREATE_TABLE.items():
        if name == "load_context":
            continue
        slug = f"tbl-{uuid.uuid4().hex[:12]}"
        if name in WORKING_STATE_KWARGS or name in CONTEXT_KWARGS or name in PROJECT_KWARGS:
            kwargs = {
                "project_slug": slug,
                **(
                    WORKING_STATE_KWARGS.get(name)
                    or CONTEXT_KWARGS.get(name)
                    or PROJECT_KWARGS[name]
                ),
            }
        else:
            kwargs = {
                "scope": "project",
                "project_slug": slug,
                **GHOST_EXTRA_KWARGS.get(name, {}),
            }
        if expect_create:
            call_tool(name, key, **kwargs)
        elif name in READ_TOOLS:
            result = call_tool(name, key, **kwargs)
            assert result.result == READ_TOOLS_EMPTY_RESULT[name], name
        else:
            with pytest.raises(MCPToolError) as exc_info:
                call_tool(name, key, **kwargs)
            assert exc_info.value.code == ProjectNotFound.code, name
        exists = session.query(ProjectSlug).filter_by(slug=slug).count() == 1
        assert exists == expect_create, name


@respx.mock
@pytest.mark.parametrize("tool", READ_TOOLS)
def test_a_read_tool_on_an_absent_project_is_empty_not_an_error(tool, call_tool):
    """An agent does not know whether today is its first day. Its first call
    is load_context or recall, never retain, so an error here teaches it to
    stop calling. Empty is also true: a project with no memories has nothing
    to say."""
    respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    key = call_tool.make_user()
    kwargs = {
        "scope": "project",
        "project_slug": "never-seen",
        **GHOST_EXTRA_KWARGS.get(tool, {}),
    }

    result = call_tool(tool, key, **kwargs)

    assert result.result == READ_TOOLS_EMPTY_RESULT[tool]


@respx.mock
@pytest.mark.parametrize("tool", READ_TOOLS)
def test_a_read_tool_never_leaks_project_access_denied(tool, call_tool):
    """The oracle guard. A foreign project and an absent one must be
    indistinguishable; ProjectAccessDenied carries owner_type and would
    distinguish them. Only _authorize_resolution's collapse may be
    observed."""
    _mock_bank()
    respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
        return_value=httpx.Response(200, json={})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")
    kwargs = {
        "scope": "project",
        "project_slug": "payments",
        **GHOST_EXTRA_KWARGS.get(tool, {}),
    }

    result = call_tool(tool, alice, **kwargs)

    assert result.result == READ_TOOLS_EMPTY_RESULT[tool]


def test_transfer_moves_ownership(call_tool):
    juan = call_tool.make_user()
    juan_id = call_tool.last_user_id
    call_tool.seed_project(juan, "payments")
    alice = call_tool.make_user()
    alice_id = call_tool.last_user_id

    result = call_tool(
        "transfer",
        juan,
        project_slug="payments",
        owner_type="user",
        owner_id=alice_id,
    )

    assert result.result == {
        "project_slug": "payments",
        "owner_type": "user",
        "owner_id": alice_id,
    }
    # And the new owner, not the old one, can now reach it.
    second = call_tool(
        "transfer",
        alice,
        project_slug="payments",
        owner_type="user",
        owner_id=juan_id,
    )
    assert second.result["owner_id"] == juan_id


def test_transfer_is_metered_like_every_other_write(call_tool, monkeypatch):
    """Transfer resolves no Hindsight bank, so it never passes through
    `_resolve_bank` -- the choke point where SPEC §20's ceiling is applied to
    everything else. `projects.transfer` carries it in the domain instead, so
    a caller cannot dodge the limit by picking this one tool, on either
    surface."""
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()

    juan = call_tool.make_user()
    call_tool.seed_project(juan, "payments")
    call_tool.seed_project(juan, "billing")
    call_tool.make_user()
    alice_id = call_tool.last_user_id

    # Two transfers by ONE credential: the ceiling is per-credential, so both
    # have to come from the same caller for the second to be refused.
    call_tool(
        "transfer",
        juan,
        project_slug="payments",
        owner_type="user",
        owner_id=alice_id,
    )
    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "transfer",
            juan,
            project_slug="billing",
            owner_type="user",
            owner_id=alice_id,
        )

    assert exc_info.value.code == "RATE_LIMITED"


def test_transfer_on_a_foreign_project_is_indistinguishable_from_absent(call_tool):
    """The tool must resolve the slug first, then authorize -- exactly as
    the REST route does. Calling projects.authorize directly would raise
    ProjectAccessDenied, carrying owner_type, and break Task 5's invariant
    on the one surface that most invites the shortcut."""
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as foreign:
        call_tool(
            "transfer",
            alice,
            project_slug="payments",
            owner_type="user",
            owner_id=call_tool.last_user_id,
        )
    with pytest.raises(MCPToolError) as absent:
        call_tool(
            "transfer",
            alice,
            project_slug="does-not-exist",
            owner_type="user",
            owner_id=call_tool.last_user_id,
        )

    assert foreign.value.code == absent.value.code == "PROJECT_NOT_FOUND"
    # Same shape, not the same slug (the two calls deliberately target
    # different ones) -- the invariant is that neither error leaks anything
    # ProjectAccessDenied would carry (owner_type, most of all).
    assert set(foreign.value.details) == set(absent.value.details) == {"project_slug"}


@respx.mock
def test_oversize_content_is_rejected_over_mcp(call_tool):
    """v0.4.0's canonical-claim ceiling (normalize_claim, 4096 bytes) is
    fixed by spec, not MEMORY_MAX_CONTENT_BYTES-configurable. No route is
    registered on purpose -- a request that reached Hindsight at all fails
    via respx's own AllMockedAssertionError."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("retain", key, scope="user", content="x" * 5000, **_retain_kwargs())

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
        call_tool("forget", key, scope="user", memory_id=GHOST, reason="x" * 100)

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
def test_a_non_uuid_operation_id_on_retain_is_rejected_not_blamed_on_hindsight_over_mcp(
    call_tool,
):
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "retain",
            key,
            scope="user",
            content="x",
            operation_id="retry-1",
            **_retain_kwargs(),
        )

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
def test_sync_retain_returns_a_completed_typed_response(call_tool):
    """Pins sync_retain's result shape against a `return ToolResult(result={})`
    mutation -- no prior test called sync_retain by name at all."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/.+").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    key = call_tool.make_user()

    result = call_tool("sync_retain", key, scope="user", content="x", **_retain_kwargs())

    assert result.result["status"] == "completed"


@respx.mock
def test_sync_retain_polls_the_operation_unlike_retain(call_tool):
    """Pins the sync/async distinction itself -- sync_retain's entire reason
    to exist. Both send an async retain upstream (SPEC §6.2: ACH MUST NOT
    use an upstream synchronous path that ignores operation_id); only
    sync_retain then polls get_operation to a terminal status before
    returning."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    operation_route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/.+").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    key = call_tool.make_user()

    call_tool("retain", key, scope="user", content="x", **_retain_kwargs())
    assert operation_route.call_count == 0

    call_tool("sync_retain", key, scope="user", content="y", **_retain_kwargs())
    assert operation_route.call_count == 1


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
    route = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    key = call_tool.make_user()

    call_tool("forget", key, scope="user", memory_id=GHOST, reason="wrong")

    assert b'"state":"invalidated"' in route.calls.last.request.read()


@respx.mock
def test_restore_reverts_it(call_tool):
    _mock_bank()
    route = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    key = call_tool.make_user()

    call_tool("restore", key, scope="user", memory_id=GHOST)

    assert b'"state":"valid"' in route.calls.last.request.read()


@respx.mock
def test_correct_replaces_the_text(call_tool):
    _mock_bank()
    route = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
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
    route = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("correct", key, scope="user", memory_id=GHOST, content="   ")

    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.code != "MEMORY_NOT_CURATABLE"
    assert route.call_count == 0


@respx.mock
def test_mcp_correct_rejects_secret(call_tool):
    """Exercises both surfaces of `correct`'s canonical boundary: an
    authorized untracked legacy Hindsight memory, and a tracked ACH source.
    Neither may reach Hindsight with a secret-shaped claim."""
    token_shaped_secret = "".join(("g", "hp", "_", "A" * 36))
    _mock_bank()
    key = call_tool.make_user()

    untracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("correct", key, scope="user", memory_id=GHOST, content=token_shaped_secret)
    assert exc_info.value.code == "CONTENT_REJECTED_BY_SANITIZER"
    assert untracked_route.call_count == 0

    tracked_id = "33333333-3333-3333-3333-333333333333"
    _seed_tracked_memory(call_tool, key, tracked_id)
    tracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{tracked_id}"
    ).mock(return_value=httpx.Response(200, json={"id": tracked_id}))

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("correct", key, scope="user", memory_id=tracked_id, content=token_shaped_secret)
    assert exc_info.value.code == "CONTENT_REJECTED_BY_SANITIZER"
    assert tracked_route.call_count == 0


@respx.mock
def test_mcp_correct_rejects_canonical_oversize(call_tool):
    oversize = "x" * 4097
    _mock_bank()
    key = call_tool.make_user()

    untracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("correct", key, scope="user", memory_id=GHOST, content=oversize)
    assert exc_info.value.code == "CONTENT_TOO_LARGE"
    assert untracked_route.call_count == 0

    tracked_id = "33333333-3333-3333-3333-333333333333"
    _seed_tracked_memory(call_tool, key, tracked_id)
    tracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{tracked_id}"
    ).mock(return_value=httpx.Response(200, json={"id": tracked_id}))

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("correct", key, scope="user", memory_id=tracked_id, content=oversize)
    assert exc_info.value.code == "CONTENT_TOO_LARGE"
    assert tracked_route.call_count == 0


@respx.mock
def test_mcp_correct_accepts_exact_canonical_limit(call_tool):
    exact = "x" * 4096
    _mock_bank()
    key = call_tool.make_user()

    untracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))

    call_tool("correct", key, scope="user", memory_id=GHOST, content=exact)
    sent = untracked_route.calls.last.request.read()
    assert len(sent.split(b'"text":"')[1].rsplit(b'"', 1)[0]) == 4096

    tracked_id = "33333333-3333-3333-3333-333333333333"
    _seed_tracked_memory(call_tool, key, tracked_id)
    tracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{tracked_id}"
    ).mock(return_value=httpx.Response(200, json={"id": tracked_id}))

    call_tool("correct", key, scope="user", memory_id=tracked_id, content=exact)
    sent = tracked_route.calls.last.request.read()
    assert len(sent.split(b'"text":"')[1].rsplit(b'"', 1)[0]) == 4096


@respx.mock
def test_mcp_correct_uses_normalized_claim(call_tool):
    raw = "Stable\t\tclaim.  \r\n"
    canonical = b'"Stable claim.\\n"'
    _mock_bank()
    key = call_tool.make_user()

    untracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}"
    ).mock(return_value=httpx.Response(200, json={"id": GHOST}))

    call_tool("correct", key, scope="user", memory_id=GHOST, content=raw)
    assert canonical in untracked_route.calls.last.request.read()

    tracked_id = "33333333-3333-3333-3333-333333333333"
    _seed_tracked_memory(call_tool, key, tracked_id)
    tracked_route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{tracked_id}"
    ).mock(return_value=httpx.Response(200, json={"id": tracked_id}))

    call_tool("correct", key, scope="user", memory_id=tracked_id, content=raw)
    assert canonical in tracked_route.calls.last.request.read()


@respx.mock
def test_list_memories_reaches_the_list_endpoint(call_tool):
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    key = call_tool.make_user()

    call_tool("list_memories", key, scope="user")

    assert route.call_count == 1


@respx.mock
def test_list_memories_forwards_a_tag_filter_as_an_and_over_mcp(call_tool):
    """QA F-09, MCP twin of the REST test: the tag reaches Hindsight as a
    `tags=` param ANDed via `tags_match=all`."""
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list.*").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    key = call_tool.make_user()

    call_tool("list_memories", key, scope="user", tags_filter=["repo:x"])

    params = route.calls.last.request.url.params
    assert params.get_list("tags") == ["repo:x"]
    assert params["tags_match"] == "all"


def test_list_memories_refuses_a_reserved_tag_filter_over_mcp(call_tool):
    """No route registered on purpose: a reserved tag must be refused at the
    boundary, and a request that reached Hindsight fails via respx itself."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("list_memories", key, scope="user", tags_filter=["type:constraint"])

    assert exc_info.value.code == "INVALID_TAG"


@respx.mock
def test_get_memory_reaches_the_memory_endpoint(call_tool):
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    key = call_tool.make_user()

    call_tool("get_memory", key, scope="user", memory_id=GHOST)

    assert route.call_count == 1


@respx.mock
def test_a_read_tool_does_not_create_a_project(call_tool, session):
    """list_memories is one of the collection reads that map an absent
    project to empty (decision 3) -- but empty is not the same as lazy
    creation (out of scope): the project still must not exist afterward."""
    from memory.models import Project, ProjectSlug

    _mock_bank()
    key = call_tool.make_user()

    result = call_tool("list_memories", key, scope="project", project_slug="never-seen")

    assert result.result == {"items": []}
    assert session.query(ProjectSlug).filter_by(slug="never-seen").count() == 0
    assert session.query(Project).count() == 0


@respx.mock
def test_idor_a_curation_tool_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    curate = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/[^/]+").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "correct",
            alice,
            scope="project",
            project_slug="payments",
            memory_id=GHOST,
            content="mine now",
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert curate.call_count == 0


@respx.mock
def test_document_id_with_colons_and_slashes_reaches_hindsight_verbatim(call_tool):
    _mock_bank()
    doc_id = "github:acme/payments-api:pr:382"
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*").mock(
        return_value=httpx.Response(200, json={"id": doc_id})
    )
    key = call_tool.make_user()

    call_tool("get_document", key, scope="user", document_id=doc_id)

    assert str(route.calls.last.request.url).endswith(f"/documents/{doc_id}")


@respx.mock
def test_list_documents_reaches_the_documents_endpoint(call_tool):
    _mock_bank()
    # The query string is optional in this pattern on purpose: the assertion
    # is "it reached the documents endpoint", and the reduced shape now sends
    # a default `?limit=`. test_list_tools_default_to_a_small_page pins that.
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents(\?.*)?$").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
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
            "get_document",
            key,
            scope="user",
            document_id="../../../../v1/default/banks/OTHER/memories",
        )

    assert exc_info.value.code == DocumentNotFound.code
    assert route.call_count == 0


@respx.mock
def test_delete_document_reaches_the_delete_endpoint(call_tool):
    _mock_bank()
    doc_id = "github:acme/payments-api:pr:382"
    route = respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*").mock(
        return_value=httpx.Response(200, json={"deleted": True})
    )
    key = call_tool.make_user()

    call_tool("delete_document", key, scope="user", document_id=doc_id)

    assert str(route.calls.last.request.url).endswith(f"/documents/{doc_id}")


@respx.mock
def test_idor_delete_document_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    delete = respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*").mock(
        return_value=httpx.Response(200, json={"deleted": True})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "delete_document",
            alice,
            scope="project",
            project_slug="payments",
            document_id="some-doc",
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert delete.call_count == 0


@respx.mock
def test_cancel_operation_reaches_delete_not_the_delete_subpath(call_tool):
    _mock_bank()
    op_id = GHOST
    route = respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}$").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )
    key = call_tool.make_user()

    call_tool("cancel_operation", key, scope="user", operation_id=op_id)

    assert route.call_count == 1
    assert str(route.calls.last.request.url).endswith(f"/operations/{op_id}")
    assert not str(route.calls.last.request.url).endswith("/delete")


@respx.mock
def test_get_operation_reaches_the_operation_endpoint(call_tool):
    _mock_bank()
    op_id = GHOST
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}$").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    key = call_tool.make_user()

    call_tool("get_operation", key, scope="user", operation_id=op_id)

    assert route.call_count == 1


@respx.mock
def test_list_operations_reaches_the_operations_endpoint(call_tool):
    _mock_bank()
    # Query string optional, same reason as the documents case above.
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations(\?.*)?$").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    key = call_tool.make_user()

    call_tool("list_operations", key, scope="user")

    assert route.call_count == 1


@respx.mock
def test_idor_cancel_operation_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    cancel = respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/[^/]+$").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "cancel_operation",
            alice,
            scope="project",
            project_slug="payments",
            operation_id=GHOST,
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert cancel.call_count == 0


@respx.mock
def test_an_operator_cannot_reach_another_users_project_over_mcp(call_tool, master_headers):
    """Same invariant as
    test_mcp_server.py::test_an_operator_reaches_mcp_as_an_ordinary_user_with_no_authority,
    proven through a real tool call rather than on the Principal.

    The operator is no longer refused -- authority is configuration over an
    ordinary identity now, so refusing would lock them out of their own
    memory over MCP. What must not happen is the thing the refusal existed to
    stop: `_resolve_bank` bypasses ownership for `is_master` (§7), so an
    operator who kept their authority here would resolve juan's project bank
    and read it. They get the same empty result any stranger gets.
    """
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"hits": [{"id": "juan's"}]})
    )
    juan = call_tool.make_user()
    call_tool.seed_project(juan, "payments")
    operator = master_headers[IDENTITY_HEADER]

    result = call_tool("recall", operator, scope="project", project_slug="payments", query="x")

    assert result.result == {"hits": [], "truncated": False}


@respx.mock
def test_idor_get_memory_cannot_reach_an_unauthorized_bank(call_tool):
    """SPEC §20.1, the MCP twin of test_curation_api.py's equivalent -- `_run`
    resolves the bank on its own line for every tool, so each needs its own
    case. memory_id must be a syntactically valid UUID (GHOST): the client's
    local `_require_uuid` guard would otherwise zero out call_count for a
    malformed id whether or not the bank check ran at all. get_memory is a
    get-by-id, so an absent project is PROJECT_NOT_FOUND -- and a foreign
    project must be indistinguishable from an absent one: the same error
    here too, never a call to the foreign bank."""
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    get = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}$").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "get_memory",
            alice,
            scope="project",
            project_slug="payments",
            memory_id=GHOST,
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert get.call_count == 0


@respx.mock
def test_idor_forget_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    forget = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}$").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "forget",
            alice,
            scope="project",
            project_slug="payments",
            memory_id=GHOST,
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert forget.call_count == 0


@respx.mock
def test_idor_restore_cannot_reach_an_unauthorized_bank(call_tool):
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    restore = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}$").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "restore",
            alice,
            scope="project",
            project_slug="payments",
            memory_id=GHOST,
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert restore.call_count == 0


@respx.mock
def test_idor_get_document_cannot_reach_an_unauthorized_bank(call_tool):
    """get_document is a get-by-id, so an absent project is
    PROJECT_NOT_FOUND; a foreign project is indistinguishable from an absent
    one, so the same error here too, never a call to the foreign bank."""
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    get_doc = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.*").mock(
        return_value=httpx.Response(200, json={"id": "doc1"})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "get_document",
            alice,
            scope="project",
            project_slug="payments",
            document_id="doc1",
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert get_doc.call_count == 0


@respx.mock
def test_idor_get_operation_cannot_reach_an_unauthorized_bank(call_tool):
    """get_operation is a get-by-id, so an absent project is
    PROJECT_NOT_FOUND; a foreign project is indistinguishable from an absent
    one, so the same error here too, never a call to the foreign bank."""
    from memory.errors import ProjectNotFound

    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    get_op = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{GHOST}$").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    juan, alice = call_tool.make_user(), call_tool.make_user()
    call_tool.seed_project(juan, "payments")

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "get_operation",
            alice,
            scope="project",
            project_slug="payments",
            operation_id=GHOST,
        )

    assert exc_info.value.code == ProjectNotFound.code
    assert get_op.call_count == 0


@respx.mock
def test_idor_scenario_z_a_known_secondary_id_from_an_unreachable_bank_is_just_not_found(
    call_tool, session
):
    """SPEC §24 scenario Z: 'Alice knows a memory_id ... from a project she
    cannot access. Supplying it under a scope she CAN access does not grant
    access: resolution happens only inside the already-authorized bank.'
    Unlike the project-scope not-found cases above (Alice names a scope she
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
    respx.get(url__regex=rf"{BASE}/v1/default/banks/{alice_bank_id}/memories/{GHOST}$").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("get_memory", alice_key, scope="user", memory_id=GHOST)

    assert exc_info.value.code == MemoryNotFound.code
    assert juan_route.call_count == 0


EXPECTED_TOOLS = {
    "retain",
    "sync_retain",
    "recall",
    "reflect",
    "list_memories",
    "get_memory",
    "memory_history",
    "forget",
    "correct",
    "restore",
    "list_documents",
    "get_document",
    "delete_document",
    "get_operation",
    "list_operations",
    "cancel_operation",
    "start_working_session",
    "set_working_state",
    "clear_working_state",
    "load_context",
    "create_mental_model",
    "list_mental_models",
    "get_mental_model",
    "update_mental_model",
    "refresh_mental_model",
    "delete_mental_model",
    "transfer",
}

# Moves whenever a tool's description, schema or annotations change. Last
# moved by `workspace_id` gaining a description on the three Working State
# tools. It is the one field a caller cannot author -- it is derived from the
# filesystem -- and it carried a bare `^ws_[0-9a-f]{32}$` with no account of
# where such a value comes from. Measured 2026-09-11: a caller that was not
# our own stdio proxy had no way to satisfy it.
# One field's description; no validation changed.
# Last moved by two changes landing together on 2026-09-11: load_context
# returning ToolResult (QA F-24) -- it gains the outputSchema every other tool
# already advertised, so the SDK emits structuredContent for it -- and the
# trigger description on create/update_mental_model (QA F-19/F-22), which
# callers had been guessing at.
# Then by the get_mental_model description (F-18): the tool now delivers
# content once the refresh has landed, and said only "metadata" before.
# Then by list_memories.tags_filter and the schema descriptions from QA
# F-04/F-09/F-11, 2026-09-11.
# Then by memory_history's provenance/curation half (QA F-13/F-14): its
# description names what it now returns, and its outputSchema gains the
# `provenance` and `curation` fields.
# Then by three changes landing together: recall loses its never-read
# `verbose` parameter (QA F-08), list_memories' description names
# `tags_filter` (QA F-09), and get_operation's description covers curation
# operation ids (QA F-15).
TOOL_CONTRACT_SHA256 = "494e7971385cb732f91705840232ec2aa839d9f832a1ede39d2bfe2791ec3a9e"


def test_tool_registration_is_stable_after_module_split():
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    tools = mcp._tool_manager.list_tools()
    names = {tool.name for tool in tools}

    assert len(tools) == 27
    assert {
        "retain",
        "sync_retain",
        "recall",
        "reflect",
        "start_working_session",
        "set_working_state",
    }.issubset(names)


@pytest.mark.anyio
async def test_serialized_tool_contract_is_stable_after_module_split():
    """Pin descriptions, schemas and annotations before moving registration."""
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    tools = await mcp.list_tools()
    contract = [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "annotations": None
            if tool.annotations is None
            else tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=False),
        }
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    serialized = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()

    assert len(tools) == 27
    assert hashlib.sha256(serialized).hexdigest() == TOOL_CONTRACT_SHA256


@respx.mock
def test_create_mental_model_rejects_always_in_context(call_tool):
    """Standing delivery is not a caller's choice: the argument is gone,
    and passing it is an unknown-field error, not a silent no-op."""
    key = call_tool.make_user()
    with pytest.raises(TypeError):
        call_tool(
            "create_mental_model",
            key,
            scope="user",
            name="Ops",
            source_query="?",
            source_tags=["repo:group/app"],
            max_tokens=512,
            trigger={},
            always_in_context=True,
        )


@pytest.mark.anyio
async def test_no_model_tool_advertises_always_in_context():
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    for tool in await mcp.list_tools():
        assert "always_in_context" not in json.dumps(tool.input_schema), tool.name
        assert "always_in_context" not in (tool.description or ""), tool.name


@pytest.mark.anyio
async def test_load_context_accepts_an_empty_input_object():
    """Standing context is useful without a project and must not require nulls."""
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    tool = next(item for item in await mcp.list_tools() if item.name == "load_context")

    assert tool.input_schema.get("required", []) == []


# SPEC §11.6 and §11.7. Each is excluded for a stated reason: whole-bank
# destruction an LLM would reach for when it decides memory is "stale"; bank
# configuration that is policy for every user of a project; shared, persistent
# state that steers future agents; a "dry run" whose name invites the model to
# treat it as free when it costs exactly the same as the real thing.
FORBIDDEN_TOOLS = {
    "clear_memories",
    "delete_bank",
    "get_bank",
    "update_bank",
    "get_bank_stats",
    "list_banks",
    "create_bank",
    "dry_run_refresh",
    "dry-run-refresh",
    "list_tags",
    "retry_operation",
    "delete_operation",
    # clear_mental_model/list_mental_model_history: dropped entirely from the
    # v0.4.0 governed lifecycle (SPEC §7), REST included -- not merely absent
    # from MCP. create/get/list/update/refresh/delete_mental_model are now
    # part of EXPECTED_TOOLS instead of forbidden.
    "clear_mental_model",
    "list_mental_model_history",
    "create_directive",
    "list_directives",
    "delete_directive",
    "update_project",
    "transfer_project",
    "create_project",
    "create_user",
    "create_group",
    "create_key",
    "list_users",
    "list_keys",
    "revoke_key",
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


def test_instructions_carry_the_static_mcp_contract_to_every_caller():
    """Direct HTTP clients still need the service identity and safety floor.

    The mutable read/write policy ships as host policy; the capped MCP
    instruction field carries only the universal safety contract.
    """
    from memory.mcp.server import build_mcp

    text = (build_mcp().instructions or "").lower()

    assert "durable" in text, "must name the service's purpose"
    assert "scope" in text and "project_slug" in text, "must describe addressing"
    assert "english" in text, "retrieval reranks in English only"
    assert "credentials" in text, "must forbid storing secrets"
    # Any MCP client gets this string, not only a coding agent.
    assert "coding agent" not in text


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
        REGISTRY["retain"](scope="user", content="x" * 300_000, ctx=NoAuth(), **_retain_kwargs())

    assert exc_info.value.code == "UNAUTHORIZED"
    assert "256000" not in str(exc_info.value), (
        "the configured content limit leaked to an unauthenticated caller"
    )


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

    with caplog.at_level(logging.ERROR, logger="memory.mcp"):
        result = call_tool("recall", key, scope="user", query="hi")

    assert result.result["hits"] == ()
    messages = [r.message for r in caplog.records]
    assert messages.count("upstream response was not a JSON object") == 0
    assert messages.count("unhandled MCP tool error") == 0


def test_invalid_request_names_the_offending_field(call_tool):
    """`_validation_message` used to join only `e['msg']`, so a multi-field
    failure read like two unattributed sentences with no indication of WHICH
    argument was wrong. `loc` names the caller's own field -- never server
    state -- so including it is safe and makes INVALID_REQUEST actionable."""
    key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool("retain", key, scope="not-a-real-scope", content="x", **_retain_kwargs())

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
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list(\?.*)?$").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    key = call_tool.make_user()

    call_tool("list_memories", key, scope="user", limit=500)

    assert "limit=500" in str(route.calls.last.request.url)


@respx.mock
def test_verbose_restores_the_upstream_default_page(call_tool):
    """verbose is the escape hatch to the old behaviour whole, and the old
    behaviour was to send no limit at all and let Hindsight decide."""
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list(\?.*)?$").mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0})
    )
    key = call_tool.make_user()

    call_tool("list_memories", key, scope="user", verbose=True)

    assert "limit=" not in str(route.calls.last.request.url)


@respx.mock
def test_recall_asks_hindsight_not_to_build_the_entity_map(call_tool):
    """Disabling include.entities upstream saves assembling the map, not just
    shipping it -- which dropping the key on the way out would not."""
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    key = call_tool.make_user()

    call_tool("recall", key, scope="user", query="deps")
    assert json.loads(route.calls.last.request.content)["include"]["entities"] is None


@respx.mock
def test_recall_passes_caller_tags_through_to_the_client(call_tool):
    _mock_bank()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    key = call_tool.make_user()

    call_tool("recall", key, scope="user", query="deps", tags_filter=["Repo:Group/App"])

    sent = json.loads(route.calls.last.request.content)
    assert sent["tag_groups"] == [
        {"tags": ["schema:ach-retain-v1"], "match": "all_strict"},
        {"tags": ["repo:group/app"], "match": "all_strict"},
    ]
    assert "tags" not in sent, "tags and tag_groups are mutually exclusive upstream"


@respx.mock
def test_recall_refuses_a_reserved_tag_namespace(call_tool):
    _mock_bank()
    key = call_tool.make_user()
    with pytest.raises(MCPToolError):
        call_tool("recall", key, scope="user", query="deps", tags_filter=["schema:x"])


@respx.mock
def test_recall_reduces_the_upstream_payload(call_tool):
    """recall has no verbose escape hatch (QA F-08 -- the flag was declared and
    never read): the reduced shape is the only shape."""
    _mock_bank()
    upstream = {
        "results": [
            {
                "id": "m1",
                "text": "we use uv",
                "type": "world",
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

    reduced = call_tool("recall", key, scope="user", query="deps").result
    assert reduced["hits"][0]["memory_id"] == "m1"
    assert reduced["hits"][0]["text"] == "we use uv"
    assert "chunk_id" not in str(reduced)


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

    assert dumped["result"]["hits"] == ()


def test_the_mcp_mount_issues_no_session(app):
    """Stateless, and it has to stay that way to run more than one replica.

    A stateful streamable-HTTP mount keeps the session in the serving
    process's memory and hands the client an Mcp-Session-Id to send back. With
    a second replica behind the Gateway and no sticky routing, that id lands
    on a pod that never saw the session and the call fails. Nothing here needs
    it: `tool_session` authenticates every call from that call's own headers.
    """
    from fastapi.testclient import TestClient

    # Entered as a context manager on purpose: the session manager is started
    # by the host app's lifespan (create_app), which a bare TestClient never
    # runs -- the mount then raises "Task group is not initialized".
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp/",
            headers={
                # DNS-rebinding protection allows 127.0.0.1 and localhost only
                # (config default), and TestClient's own "testserver" host is
                # a 421.
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "server/discover",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "test",
                            "version": "0",
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    assert "mcp-session-id" not in {k.lower() for k in response.headers}


# ---------------------------------------------------------------------------
# start_working_session / set_working_state
# ---------------------------------------------------------------------------

WST_WS = "ws_" + "a" * 32


def _wst_project(client, headers, slug: str = "acme-api") -> None:
    client.post("/v1/projects", json={"project_slug": slug}, headers=headers)


@pytest.mark.anyio
async def test_working_state_tool_descriptions_state_their_constraints():
    from memory.mcp.server import build_mcp
    from memory.mcp.tools import register

    mcp = build_mcp()
    register(mcp)
    tools = {t.name: t for t in await mcp.list_tools()}

    start_description = tools["start_working_session"].description.lower()
    assert "ordering" in start_description
    assert "handoff" in start_description

    set_description = tools["set_working_state"].description.lower()
    assert "ephemeral" in set_description
    assert "no durable memory" in set_description or "never durable memory" in set_description
    assert "must not call it proactively" in set_description


def test_start_working_session_allocates_a_positive_epoch(call_tool, client, master_headers):
    key = call_tool.make_user()
    headers = _headers_for(key)
    _wst_project(client, headers)

    result = call_tool(
        "start_working_session",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
    )

    assert result.result["session_epoch"] > 0
    assert result.result["session_id"] == "sess-1"
    assert result.result["workspace_id"] == WST_WS
    assert result.result["project_slug"] == "acme-api"


def test_starting_the_same_session_twice_over_mcp_is_idempotent(call_tool, client, master_headers):
    key = call_tool.make_user()
    headers = _headers_for(key)
    _wst_project(client, headers)

    first = call_tool(
        "start_working_session",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
    )
    second = call_tool(
        "start_working_session",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
    )

    assert first.result["session_epoch"] == second.result["session_epoch"]


def test_set_working_state_returns_its_stored_fields(call_tool, client, master_headers):
    key = call_tool.make_user()
    headers = _headers_for(key)
    _wst_project(client, headers)
    epoch = call_tool(
        "start_working_session",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
    ).result["session_epoch"]

    result = call_tool(
        "set_working_state",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
        session_epoch=epoch,
        checkpoint_seq=1,
        objective="ship the feature",
    )

    assert result.result["objective"] == "ship the feature"
    assert result.result["session_epoch"] == epoch
    assert result.result["checkpoint_seq"] == 1
    assert result.result["changed"] is True


def test_set_working_state_rejects_a_stale_pair(call_tool, client, master_headers):
    key = call_tool.make_user()
    headers = _headers_for(key)
    _wst_project(client, headers)
    epoch = call_tool(
        "start_working_session",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
    ).result["session_epoch"]
    call_tool(
        "set_working_state",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
        session_epoch=epoch,
        checkpoint_seq=2,
        objective="second",
    )

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "set_working_state",
            key,
            project_slug="acme-api",
            workspace_id=WST_WS,
            session_id="sess-1",
            session_epoch=epoch,
            checkpoint_seq=1,
            objective="first",
        )

    assert exc_info.value.code == "WORKING_STATE_STALE"


def test_set_working_state_for_a_missing_project_creates_no_project(
    call_tool, client, master_headers, session
):
    from memory.models import Project

    key = call_tool.make_user()
    before = session.query(Project).count()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "start_working_session",
            key,
            project_slug="no-such-project",
            workspace_id=WST_WS,
            session_id="sess-1",
        )

    assert exc_info.value.code == "PROJECT_NOT_FOUND"
    assert session.query(Project).count() == before


def test_another_user_is_denied_writing_this_project(call_tool, client, master_headers):
    owner_key = call_tool.make_user()
    _wst_project(client, _headers_for(owner_key))
    stranger_key = call_tool.make_user()

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "start_working_session",
            stranger_key,
            project_slug="acme-api",
            workspace_id=WST_WS,
            session_id="sess-1",
        )

    assert exc_info.value.code == "PROJECT_NOT_FOUND"


def test_set_working_state_rejects_a_blank_objective_over_mcp(call_tool, client, master_headers):
    """The MCP tool takes bare scalar arguments, not a pre-validated
    WorkingStateWrite -- confirms the shared model's bounds still apply at
    this boundary, not just over REST."""
    key = call_tool.make_user()
    headers = _headers_for(key)
    _wst_project(client, headers)
    epoch = call_tool(
        "start_working_session",
        key,
        project_slug="acme-api",
        workspace_id=WST_WS,
        session_id="sess-1",
    ).result["session_epoch"]

    with pytest.raises(MCPToolError) as exc_info:
        call_tool(
            "set_working_state",
            key,
            project_slug="acme-api",
            workspace_id=WST_WS,
            session_id="sess-1",
            session_epoch=epoch,
            checkpoint_seq=1,
            objective="   ",
        )

    assert exc_info.value.code == "INVALID_REQUEST"


@respx.mock
def test_get_operation_describes_an_ach_curation_operation(call_tool, session):
    """MCP twin of the REST case in test_operations_api.py (QA F-15): a
    curation operation id is answered from ACH's own ledger."""
    from memory.models import CurationOperation

    _mock_bank()
    key = call_tool.make_user()
    _seed_tracked_memory(call_tool, key, GHOST)
    respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{GHOST}").mock(
        return_value=httpx.Response(200, json={"id": GHOST})
    )
    forgotten = call_tool("forget", key, scope="user", memory_id=GHOST, reason="obsolete")
    op_id = forgotten.result["operation_id"]
    assert op_id == session.query(CurationOperation).one().operation_id
    upstream_calls = respx.calls.call_count

    result = call_tool("get_operation", key, scope="user", operation_id=op_id)

    assert result.result["type"] == "forget"
    assert result.result["status"] == "completed"
    assert result.result["memory_id"] == GHOST
    assert respx.calls.call_count == upstream_calls
