import json

import httpx
import pytest
import respx

BASE = "http://hindsight.test"


def _create_user(client, master_headers) -> dict[str, object]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "key": key, "headers": {"Authorization": f"Bearer {key}"}}


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, object]:
    return _create_user(client, master_headers)


@pytest.fixture
def alice(client, master_headers, tenant) -> dict[str, object]:
    return _create_user(client, master_headers)


@pytest.fixture
def user_key(client, master_headers, tenant) -> tuple[str, str]:
    user = _create_user(client, master_headers)
    return user["user_id"], user["key"]


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _mock_hindsight() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/config$").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_retain_reaches_the_callers_own_bank(client, user_key, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True, "operation_id": "op-1"})
    )
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "we use uv"},
        headers=_headers(key),
    )

    assert response.status_code == 200
    assert route.called


@respx.mock
def test_retain_response_never_contains_the_bank_id(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(
            200, json={"success": True, "bank_id": "user_leaked", "operation_id": "op-1"}
        )
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x"},
        headers=_headers(key),
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)


@respx.mock
def test_two_users_reach_two_different_banks(client, master_headers, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    banks = []
    for _ in range(2):
        user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
            "user_id"
        ]
        key = client.post(
            f"/v1/users/{user_id}/keys", json={}, headers=master_headers
        ).json()["key"]
        client.post(
            "/v1/memory/retain",
            json={"scope": "user", "content": "x"},
            headers=_headers(key),
        )
        banks.append(str(route.calls.last.request.url))

    assert banks[0] != banks[1]


@respx.mock
def test_user_key_cannot_target_another_user(client, master_headers, user_key, tenant):
    _mock_hindsight()
    other = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "user_id": other, "content": "x"},
        headers=_headers(key),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@respx.mock
def test_master_key_must_name_the_target_user(client, master_headers, tenant):
    _mock_hindsight()

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x"},
        headers=master_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SCOPE"


@respx.mock
def test_master_key_reaches_a_named_user_bank(client, master_headers, user_key, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    user_id, _ = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "user_id": user_id, "content": "x"},
        headers=master_headers,
    )

    assert response.status_code == 200
    assert route.called


@pytest.fixture
def two_users(client, master_headers, tenant):
    return [_create_user(client, master_headers) for _ in range(2)]


@respx.mock
def test_project_scope_reaches_a_project_bank(client, two_users, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan = two_users[0]

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 200
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_user_and_project_scope_use_different_banks(client, two_users, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan = two_users[0]

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x"},
        headers=_headers(juan["key"]),
    )
    user_url = str(route.calls.last.request.url)
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=_headers(juan["key"]),
    )

    assert user_url != str(route.calls.last.request.url)


@respx.mock
def test_a_stranger_cannot_reach_someone_elses_project(client, two_users, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan, alice = two_users
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=_headers(juan["key"]),
    )

    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "payments-api", "query": "x"},
        headers=_headers(alice["key"]),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"


def test_project_scope_without_a_slug_is_unavailable(client, two_users, tenant):
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "query": "x"},
        headers=_headers(two_users[0]["key"]),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROJECT_CONTEXT_UNAVAILABLE"


@respx.mock
def test_retain_against_a_retired_slug_forwards_and_carries_the_notice(client, two_users, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan = two_users[0]

    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=_headers(juan["key"]),
    )
    original_bank_url = str(route.calls.last.request.url)

    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-service"},
        headers=_headers(juan["key"]),
    )

    body = client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "y"},
        headers=_headers(juan["key"]),
    ).json()

    # call_count, not just the last URL: if the second retain never reached
    # Hindsight at all (e.g. it 409'd before forwarding), calls.last still
    # points at the FIRST call and the URL assertion above would pass on that
    # failure path too.
    assert route.call_count == 2
    assert str(route.calls.last.request.url) == original_bank_url
    assert body["resolved_from"] == "payments-api"
    assert body["project_slug"] == "payments-service"
    assert body["notice"] == "PROJECT_RENAMED"


def test_oversize_content_is_rejected(client, user_key, tenant, monkeypatch):
    from memory.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MEMORY_MAX_CONTENT_BYTES", "10")
    get_settings.cache_clear()
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x" * 100},
        headers=_headers(key),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


def test_oversize_git_locator_on_retain_is_a_typed_422(client, user_key, tenant):
    """Pins ScopedRequest.git_locator's max_length=512 bound on the memory
    data plane -- the only prior oversize-git_locator test is against the
    projects router's own CreateProjectRequest, a different model that
    happens to share the same bound."""
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "git_locator": "g" * 5000},
        headers=_headers(key),
    )

    assert response.status_code == 422


@respx.mock
def test_recall_returns_the_upstream_payload(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(200, json={"memories": [{"content": "we use uv"}]})
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "deps"},
        headers=_headers(key),
    ).json()

    assert body["result"]["memories"][0]["content"] == "we use uv"


@respx.mock
def test_nested_bank_id_is_stripped_from_recall(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(
            200,
            json={
                "memories": [
                    {"content": "we use uv", "bank_id": "user_leaked_nested"}
                ],
                "meta": {"inner": {"bank_id": "user_leaked_deep"}},
            },
        )
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "deps"},
        headers=_headers(key),
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked_nested" not in str(body)
    assert "user_leaked_deep" not in str(body)
    assert body["result"]["memories"][0]["content"] == "we use uv"


@respx.mock
def test_retain_project_row_survives_a_failed_hindsight_call(
    client, two_users, tenant, session
):
    """Pins the commit-before-upstream-call ordering in _retain (see the
    comment above db.commit() in memory.api.memory). A first-touch project
    row is the only record of a freshly allocated bank_id; committing it
    before the Hindsight retain call means a 500 there still leaves the
    project (and its bank_id) reachable for a retry, instead of orphaning
    the bank Hindsight auto-creates on that retain.
    """
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    juan = two_users[0]

    response = client.post(
        "/v1/memory/retain",
        json={
            "scope": "project",
            "project_slug": "first-touch-retain",
            "content": "x",
        },
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 502

    from memory.models import Project

    project = (
        session.query(Project)
        .filter_by(tenant_id=tenant, project_slug="first-touch-retain")
        .one_or_none()
    )
    assert project is not None
    assert project.bank_id


@respx.mock
def test_recall_project_row_survives_a_failed_hindsight_call(
    client, two_users, tenant, session
):
    """Same guarantee as test_retain_project_row_survives_a_failed_hindsight_call,
    for recall: it is a write path too (resolve_project_bank can lazily
    create the project), and its upstream call can fail the same way."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    juan = two_users[0]

    response = client.post(
        "/v1/memory/recall",
        json={
            "scope": "project",
            "project_slug": "first-touch-recall",
            "query": "x",
        },
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 502

    from memory.models import Project

    project = (
        session.query(Project)
        .filter_by(tenant_id=tenant, project_slug="first-touch-recall")
        .one_or_none()
    )
    assert project is not None
    assert project.bank_id


@respx.mock
def test_user_id_is_ignored_under_project_scope(client, two_users, tenant):
    """scope=project always resolves to the named project's bank; a user_id
    riding alongside it must never redirect to that user's own bank."""
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    juan, alice = two_users

    response = client.post(
        "/v1/memory/retain",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "user_id": alice["user_id"],
            "content": "x",
        },
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 200
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_retain_sends_extraction_metadata_and_a_context_line(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.post(
        "/v1/memory/retain",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "content": "x",
            "metadata": {"agent": "codex", "source": "interactive-coding"},
        },
        headers=juan["headers"],
    )

    item = json.loads(route.calls.last.request.read())["items"][0]
    assert item["metadata"]["agent"] == "codex"
    assert item["metadata"]["project_slug"] == "payments-api"
    assert item["context"] == "interactive-coding via codex"


@respx.mock
def test_retain_never_sends_audit_only_fields_to_hindsight(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.post(
        "/v1/memory/retain",
        json={
            "scope": "user",
            "content": "x",
            "metadata": {
                "source": "pull-request",
                "os": "linux",
                "arch": "arm64",
                "client_version": "1.2",
                "client_name": "codex-cli",
            },
        },
        headers=juan["headers"],
    )

    metadata = json.loads(route.calls.last.request.read())["items"][0]["metadata"]
    assert metadata["source"] == "pull-request"
    for audit_only_key in ("os", "arch", "client_version", "client_name"):
        assert audit_only_key not in metadata


@respx.mock
def test_a_reserved_metadata_key_is_refused_and_nothing_is_retained(
    client, juan, tenant
):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "metadata": {"user_id": "usr_someone"}},
        headers=juan["headers"],
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_METADATA"
    assert route.call_count == 0


@respx.mock
def test_a_reserved_metadata_key_under_project_scope_writes_nothing_to_db_or_upstream(
    client, juan, tenant, session
):
    """SPEC §13.4's "nothing is written" has to hold at the DB level too, not
    just the HTTP one -- scope=user writes no Project row either way, so that
    half of the invariant needs a scope=project case against a fresh slug."""
    bank_put = respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )

    response = client.post(
        "/v1/memory/retain",
        json={
            "scope": "project",
            "project_slug": "brand-new",
            "content": "x",
            "metadata": {"user_id": "usr_someone"},
        },
        headers=juan["headers"],
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_METADATA"
    assert not bank_put.called

    from memory.models import Project

    project = (
        session.query(Project)
        .filter_by(tenant_id=tenant, project_slug="brand-new")
        .one_or_none()
    )
    assert project is None


@respx.mock
def test_a_custom_operation_id_is_passed_through(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op_mine"})
    )
    op_id = "11111111-1111-1111-1111-111111111111"

    client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "operation_id": op_id},
        headers=juan["headers"],
    )

    assert json.loads(route.calls.last.request.read())["operation_id"] == op_id


def test_a_non_uuid_operation_id_on_retain_is_rejected_not_blamed_on_hindsight(
    client, juan, tenant
):
    """Hindsight 422s a non-UUID operation id, which surfaced as a 502 --
    blaming the backend for the caller's typo. Rejected at the boundary
    instead, the same way an oversize git_locator is."""
    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "operation_id": "retry-1"},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_a_bogus_update_mode_on_retain_is_rejected_not_blamed_on_hindsight(
    client, juan, tenant
):
    """Hindsight 422s an update_mode outside {"replace", "append"} (measured
    against a live server's MemoryItem.update_mode enum), which surfaced as a
    502 -- blaming the backend for the caller's typo, same class as the
    non-UUID operation_id case above."""
    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "update_mode": "bogus"},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_append_without_a_document_id_is_rejected_not_blamed_on_hindsight(
    client, juan, tenant
):
    """SPEC §11.4 blesses update_mode="append" for interactive coding
    sessions. Measured against a live server: Hindsight 400s with
    "update_mode='append' requires a document_id" for the omission, which
    surfaced as a 502 with a fixed message and no way for a spec-following
    caller to learn why. No respx route is registered on purpose: a request
    that reached Hindsight at all would fail this test via respx's own
    AllMockedAssertionError."""
    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "update_mode": "append"},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_append_sends_update_mode_verbatim_to_hindsight(client, user_key, respx_mock):
    """Pins the wire body, not that a call happened.

    The predecessor asserted `route.called` against a mocked 200 and so stayed
    green for the entire period `append` could not work at all (Plan 6, review
    Critical C1). A mock cannot tell you the backend accepts something; that
    is what tests/test_append_integration.py is for.
    """
    route = respx_mock.post(url__regex=r".*/memories$").respond(
        200, json={"success": True, "items_count": 1, "async": False}
    )
    _, key = user_key

    response = client.post(
        "/v1/memory/sync_retain",
        json={
            "scope": "user",
            "content": "second line",
            "document_id": "session:abc",
            "update_mode": "append",
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    sent = json.loads(route.calls.last.request.content)
    assert sent["items"][0]["update_mode"] == "append"
    assert sent["items"][0]["document_id"] == "session:abc"


@respx.mock
def test_retain_with_a_traversal_document_id_is_rejected_not_created(
    client, juan, tenant
):
    """SPEC §12.2: delete_document is the only hard-delete lever an agent
    has. A document_id that get/delete would refuse (".", "..", a leading
    "/", control characters, ...) must not be creatable either, or the
    document it names is listed but permanently unaddressable -- accepted as
    a known wart until this fix, and worse than the note suggested: live,
    "sync_retain" with document_id=".." actually created a document that
    documents/get and documents/delete then 404'd on forever.

    No POST .../memories route is registered on purpose: if the guard in
    HindsightClient.retain() failed to stop this before the round trip,
    respx's own AllMockedAssertionError would fire (surfacing as a 500, not
    the 404 asserted below), not the fixture silently accepting a leaked
    call. _mock_hindsight() stubs the bank upsert route unconditionally, but
    this retain path no longer reaches it -- the traversal guard must raise
    before any Hindsight round trip at all.
    """
    _mock_hindsight()

    response = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "x", "document_id": ".."},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


@respx.mock
def test_reflect_reaches_the_reflect_endpoint_of_the_right_bank(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "use uv"})
    )

    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"answer": "use uv"}
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_reflect_is_denied_on_someone_elses_project(client, juan, alice, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "leaked"})
    )
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    setup = client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    assert setup.status_code == 200

    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"


@respx.mock
def test_bank_id_is_stripped_from_reflect(client, juan, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(
            200, json={"answer": "use uv", "bank_id": "user_leaked_reflect"}
        )
    )

    body = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=juan["headers"],
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked_reflect" not in str(body)
    assert body["result"]["answer"] == "use uv"


@respx.mock
def test_reflect_project_row_survives_a_failed_hindsight_call(
    client, two_users, tenant, session
):
    """Same guarantee as test_retain_project_row_survives_a_failed_hindsight_call,
    for reflect: it is a write path too (resolve_project_bank can lazily
    create the project), and its upstream call can fail the same way."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    juan = two_users[0]

    response = client.post(
        "/v1/memory/reflect",
        json={
            "scope": "project",
            "project_slug": "first-touch-reflect",
            "query": "x",
        },
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 502

    from memory.models import Project

    project = (
        session.query(Project)
        .filter_by(tenant_id=tenant, project_slug="first-touch-reflect")
        .one_or_none()
    )
    assert project is not None
    assert project.bank_id


@respx.mock
def test_reflect_against_a_retired_slug_forwards_and_pins_resolved_from(
    client, juan, tenant
):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "use uv"})
    )

    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-service"},
        headers=juan["headers"],
    )

    body = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=juan["headers"],
    ).json()

    assert body["resolved_from"] == "payments-api"
    assert body["project_slug"] == "payments-service"
    assert body["notice"] == "PROJECT_RENAMED"
