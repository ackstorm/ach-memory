import httpx
import pytest
import respx

BASE = "http://hindsight.test"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": _headers(key)}


@pytest.fixture
def alice(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": _headers(key)}


def _mock_bank() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_list_operations_filters_by_status(client, juan, tenant):
    _mock_bank()
    # `(\?|$)` rather than a bare `$`: list_operations always sends limit/offset
    # defaults, so the request always carries a query string and an unqualified
    # `$` anchor can never match a live URL -- same trap as ListDocumentsRequest
    # in test_documents_api.py.
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"operations": []}))

    response = client.post(
        "/v1/memory/operations/list",
        json={"scope": "user", "status": "pending"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params)["status"] == "pending"


@respx.mock
def test_get_operation_returns_its_status(client, juan, tenant):
    # operation_id must be a syntactically valid UUID: the client rejects a
    # non-UUID operation_id locally, so a bare "op_1" would never reach the
    # mocked route.
    op_id = "33333333-3333-3333-3333-333333333333"
    _mock_bank()
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}").mock(
        return_value=httpx.Response(200, json={"id": op_id, "status": "completed"})
    )

    response = client.post(
        "/v1/memory/operations/get",
        json={"scope": "user", "operation_id": op_id},
        headers=juan["headers"],
    )

    assert response.json()["result"]["status"] == "completed"


@respx.mock
def test_cancel_uses_the_bare_delete_path_not_the_delete_suffix(client, juan, tenant):
    """DELETE .../operations/{id} cancels. .../{id}/delete removes a terminal
    operation and v1 does not expose it (SPEC §11.5)."""
    op_id = "33333333-3333-3333-3333-333333333333"
    _mock_bank()
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}$"
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))

    response = client.post(
        "/v1/memory/operations/cancel",
        json={"scope": "user", "operation_id": op_id},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.calls.last.request.url.path.endswith(f"/operations/{op_id}")


@respx.mock
def test_a_missing_operation_is_a_404(client, juan, tenant):
    """Measured against a live server: GET on an absent operation is a 200
    with {"status": "not_found"}, never an upstream 404 -- the mock here
    matches the real server, not the assumption the previous version of this
    test encoded (a 404 Hindsight never actually sends for this route)."""
    # A syntactically valid but absent UUID: "ghost" would now be rejected by
    # the client's local UUID guard before the request is ever sent.
    absent_id = "00000000-0000-0000-0000-000000000000"
    _mock_bank()
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{absent_id}"
    ).mock(
        return_value=httpx.Response(
            200, json={"operation_id": absent_id, "status": "not_found"}
        )
    )

    response = client.post(
        "/v1/memory/operations/get",
        json={"scope": "user", "operation_id": absent_id},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "OPERATION_NOT_FOUND"


@respx.mock
def test_idor_an_operation_id_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    # A valid UUID on purpose: the client rejects a non-UUID operation_id
    # locally before any HTTP call, so a malformed id would 404 even with the
    # bank check removed entirely -- proving nothing about authorization. A
    # syntactically valid id that is never looked up is the only way this test
    # actually exercises the bank check (confirmed by temporarily stubbing
    # memory.projects.authorize() to a no-op: the test then fails).
    op_id = "44444444-4444-4444-4444-444444444444"
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    cancel = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"status": "cancelled"}))

    response = client.post(
        "/v1/memory/operations/cancel",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "operation_id": op_id,
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert cancel.call_count == 0


@respx.mock
def test_idor_get_operation_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    """SPEC §20.1: `get_operation` resolves the bank on its own line, same as
    `cancel_operation` -- a future edit to one is invisible to the other, so
    each needs its own explicit IDOR test. Verified by mutation: stubbing
    `get_operation`'s `_bank()` call away (skipping authorization) leaves
    this failing on the assertion below, not passing."""
    op_id = "44444444-4444-4444-4444-444444444444"
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    get = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": op_id}))

    response = client.post(
        "/v1/memory/operations/get",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "operation_id": op_id,
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert get.call_count == 0


@respx.mock
def test_idor_list_operations_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    """Same invariant as the other operation routes, for `list_operations`.
    Verified by mutation: stubbing `list_operations`'s `_bank()` call away
    leaves this failing on the assertion below, not passing."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    listed = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"operations": []}))

    response = client.post(
        "/v1/memory/operations/list",
        json={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert listed.call_count == 0


def test_operations_route_on_an_unknown_slug_creates_no_project(
    client, juan, tenant, session
):
    """A operation route is maintenance on something that already exists
    (SPEC §11.3), never first-touch creation (SPEC §16.2 -- retain/recall/
    reflect only). An unknown slug must 404 and leave nothing behind, not
    squat the slug for whoever asked first. Mirrors
    test_documents_route_on_an_unknown_slug_creates_no_project in
    test_documents_api.py -- the operations router copied the shape but not
    this test, so create=False on this router went unguarded."""
    from memory.models import Project

    response = client.post(
        "/v1/memory/operations/list",
        json={"scope": "project", "project_slug": "typo-slug"},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"

    project = (
        session.query(Project)
        .filter_by(tenant_id=tenant, project_slug="typo-slug")
        .one_or_none()
    )
    assert project is None


@respx.mock
def test_bank_id_is_stripped_from_an_operation_response(client, juan, tenant):
    """Mirrors test_bank_id_is_stripped_from_a_document_response in
    test_documents_api.py -- _strip_bank_id works today but was unguarded on
    this router."""
    op_id = "44444444-4444-4444-4444-444444444444"
    _mock_bank()
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{op_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": op_id,
                "bank_id": "user_leaked",
                "meta": {"bank_id": "user_leaked_nested"},
            },
        )
    )

    body = client.post(
        "/v1/memory/operations/get",
        json={"scope": "user", "operation_id": op_id},
        headers=juan["headers"],
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)
    assert "user_leaked_nested" not in str(body)
