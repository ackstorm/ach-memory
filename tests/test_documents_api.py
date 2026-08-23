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
    respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/config").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_list_documents(client, juan, tenant):
    _mock_bank()
    # `(\?|$)` rather than a bare `$`: the real request always carries a query
    # string (limit/offset are sent with defaults), so an unqualified `$`
    # anchor can never match a live URL -- this still refuses to match
    # `/documents/{id}` (the get/delete routes), which is the anchor's actual
    # job.
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"documents": []}))

    response = client.post(
        "/v1/memory/documents/list",
        json={"scope": "user", "limit": 5},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params) == {"limit": "5", "offset": "0"}


@respx.mock
def test_a_document_id_is_not_namespaced_by_the_caller(client, juan, tenant):
    """SPEC §11.4: two authorized agents deliberately writing to the same
    logical source is the point. The id reaches Hindsight verbatim."""
    _mock_bank()
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/github:acme/api:pr:382"
    ).mock(return_value=httpx.Response(200, json={"id": "github:acme/api:pr:382"}))

    response = client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": "github:acme/api:pr:382"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.calls.last.request.url.path.endswith(
        "/documents/github:acme/api:pr:382"
    )


@respx.mock
def test_delete_document_is_reachable_by_any_authorized_caller(
    client, juan, master_headers, tenant
):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post("/v1/groups", json={"id": "grp_pay"}, headers=master_headers)
    client.put(
        f"/v1/groups/grp_pay/members/{juan['user_id']}", headers=master_headers
    )
    client.post(
        "/v1/projects",
        json={"project_slug": "shared", "owner": {"type": "group", "id": "grp_pay"}},
        headers=master_headers,
    )
    # `$`-anchored, not a bare prefix match: an unanchored regex here also
    # matches `.../documents/doc_1/delete` (a neighbouring Hindsight endpoint
    # v1 must not reach), so appending a suffix to paths.document() would
    # leave this test green -- same trap as operations'
    # test_cancel_uses_the_bare_delete_path_not_the_delete_suffix.
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/doc_1$"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.post(
        "/v1/memory/documents/delete",
        json={"scope": "project", "project_slug": "shared", "document_id": "doc_1"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.call_count == 1
    assert route.calls.last.request.url.path.endswith("/documents/doc_1")


@respx.mock
def test_a_missing_document_is_a_404(client, juan, tenant):
    _mock_bank()
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/ghost").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    response = client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": "ghost"},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


@respx.mock
def test_idor_a_document_id_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    delete = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.post(
        "/v1/memory/documents/delete",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "document_id": "doc_1",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert delete.call_count == 0


@respx.mock
def test_idor_get_document_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    """SPEC §20.1: `get_document` resolves the bank on its own line, same as
    `delete_document` -- a future edit to one is invisible to the other, so
    each needs its own explicit IDOR test. Verified by mutation: stubbing
    `get_document`'s `_bank()` call away (skipping authorization) leaves this
    failing on the assertion below, not passing."""
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": "doc_1"}))

    response = client.post(
        "/v1/memory/documents/get",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "document_id": "doc_1",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert get.call_count == 0


@respx.mock
def test_idor_list_documents_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    """Same invariant as the other document routes, for `list_documents`.
    Verified by mutation: stubbing `list_documents`'s `_bank()` call away
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"documents": []}))

    response = client.post(
        "/v1/memory/documents/list",
        json={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert listed.call_count == 0


def test_documents_route_on_an_unknown_slug_creates_no_project(
    client, juan, tenant, session
):
    """A document route is maintenance on something that already exists (SPEC
    §11.3), never first-touch creation (SPEC §16.2 -- retain/recall/reflect
    only). An unknown slug must 404 and leave nothing behind, not squat the
    slug for whoever asked first."""
    from memory.models import Project

    response = client.post(
        "/v1/memory/documents/list",
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


def test_list_documents_rejects_a_negative_limit(client, juan, tenant):
    response = client.post(
        "/v1/memory/documents/list",
        json={"scope": "user", "limit": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_list_documents_rejects_a_negative_offset(client, juan, tenant):
    response = client.post(
        "/v1/memory/documents/list",
        json={"scope": "user", "offset": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


@respx.mock
def test_bank_id_is_stripped_from_a_document_response(client, juan, tenant):
    _mock_bank()
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/doc_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "doc_1",
                "bank_id": "user_leaked",
                "meta": {"bank_id": "user_leaked_nested"},
            },
        )
    )

    body = client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": "doc_1"},
        headers=juan["headers"],
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)
    assert "user_leaked_nested" not in str(body)


TRAVERSAL_DOCUMENT_IDS = [
    "..",
    "../../../../../v1/default/banks/project_VICTIM/memories",
    "doc 1?x=y",
    "doc#frag",
    "",
    # Control characters: httpx raises InvalidURL while merging a path segment
    # containing one, and InvalidURL does NOT inherit httpx.HTTPError -- it
    # walks past HindsightClient._request's handler to the app's catch-all,
    # surfacing as a 500 INTERNAL_ERROR instead of this refusal. Measured live.
    "a\rb",
    "a\nb",
    "a\tb",
]


@respx.mock
@pytest.mark.parametrize("document_id", TRAVERSAL_DOCUMENT_IDS)
def test_delete_document_traversal_never_reaches_hindsight(client, juan, tenant, document_id):
    """SPEC §11.7 keeps clear_memories/delete_bank off this surface entirely
    -- admin API + master key only. httpx applies RFC 3986 dot-segment
    removal when merging a path onto base_url, so an unvalidated ".."
    resolves to the bank itself (DELETE there is delete_bank, not
    delete_document) and a longer traversal escapes the bank altogether onto
    another tenant's bank. No respx route is registered on purpose: ANY
    outbound HTTP call at all -- to any host, any path -- fails this test via
    respx's own AllMockedAssertionError, not just the assertions below."""
    response = client.post(
        "/v1/memory/documents/delete",
        json={"scope": "user", "document_id": document_id},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


@respx.mock
@pytest.mark.parametrize("document_id", TRAVERSAL_DOCUMENT_IDS)
def test_get_document_traversal_never_reaches_hindsight(client, juan, tenant, document_id):
    """Same guard, GET side: `documents/get` with document_id=".." would
    otherwise resolve to GET on the bank itself (get_bank), relaying another
    tenant's bank metadata back to the caller."""
    response = client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": document_id},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


@respx.mock
@pytest.mark.parametrize(
    "document_id",
    [
        "github:acme/payments-api:pr:382",
        "session:550e8400-e29b-41d4-a716-446655440000",
        "file:docs/architecture.md",
    ],
)
def test_blessed_document_ids_still_reach_hindsight_verbatim(client, juan, tenant, document_id):
    """SPEC §11.4 blesses colons and slashes in a document_id -- the
    traversal guard in paths.document() must reject only an actual `.`/`..`
    segment, a leading `/`, or a `?`/`#`, never a legitimate id that merely
    contains a slash or colon."""
    _mock_bank()
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/documents/.+$").mock(
        return_value=httpx.Response(200, json={"id": document_id})
    )

    response = client.post(
        "/v1/memory/documents/get",
        json={"scope": "user", "document_id": document_id},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.calls.last.request.url.path.endswith(f"/documents/{document_id}")
