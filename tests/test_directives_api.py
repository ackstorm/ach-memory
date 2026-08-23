import json

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


DIR_ID = "11111111-1111-1111-1111-111111111111"


@respx.mock
def test_create_directive(client, juan, tenant):
    _mock_bank()
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives$"
    ).mock(return_value=httpx.Response(201, json={"id": DIR_ID, "name": "n"}))

    response = client.post(
        "/v1/directives",
        json={"scope": "user", "name": "n", "content": "Always use uv."},
        headers=juan["headers"],
    )

    assert response.status_code == 201
    assert response.json()["result"] == {"id": DIR_ID, "name": "n"}
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"name": "n", "content": "Always use uv."}


@respx.mock
def test_create_directive_never_sends_tags_even_if_the_caller_supplies_them(
    client, juan, tenant
):
    """SPEC §14/§13.6: Hindsight's directive `tags` is an in-bank visibility
    scope this service does not model. Not just undocumented on the request
    model -- pydantic's default `extra="ignore"` means a caller-supplied
    `tags` is silently dropped by parsing, but this pins that it can never
    ride along to Hindsight even if a future model change adds the field."""
    _mock_bank()
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives$"
    ).mock(return_value=httpx.Response(201, json={"id": DIR_ID}))

    client.post(
        "/v1/directives",
        json={
            "scope": "user",
            "name": "n",
            "content": "c",
            "tags": ["some-scope"],
        },
        headers=juan["headers"],
    )

    sent = json.loads(route.calls.last.request.content)
    assert "tags" not in sent


@respx.mock
def test_create_directive_forwards_priority_and_is_active(client, juan, tenant):
    _mock_bank()
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives$"
    ).mock(return_value=httpx.Response(201, json={"id": DIR_ID}))

    client.post(
        "/v1/directives",
        json={
            "scope": "user",
            "name": "n",
            "content": "c",
            "priority": 5,
            "is_active": False,
        },
        headers=juan["headers"],
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent == {"name": "n", "content": "c", "priority": 5, "is_active": False}


@respx.mock
def test_create_directive_materializes_a_never_touched_bank_first(client, juan, tenant):
    """Regression for the defect this file's fixtures never caught: a bank
    with no prior retain/sync_retain has never been ensure_bank'd, and
    Hindsight's directive-create route does NOT auto-create a bank the way
    mental-model-create does -- measured live, POST /v1/directives against
    such a bank 500s upstream (folded into 502 HINDSIGHT_ERROR). Before the
    fix this test's `upsert` mock is registered but never called, since
    create_directive never called ensure_bank; the assertion on
    upsert.call_count is what actually fails, not a respx mock-miss."""
    upsert = respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )
    create = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives$"
    ).mock(return_value=httpx.Response(201, json={"id": DIR_ID}))

    response = client.post(
        "/v1/directives",
        json={"scope": "user", "name": "n", "content": "Always use uv."},
        headers=juan["headers"],
    )

    assert response.status_code == 201
    assert upsert.call_count == 1
    assert create.call_count == 1


@respx.mock
def test_list_directives_forwards_query_params(client, juan, tenant):
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"directives": []}))

    response = client.get(
        "/v1/directives",
        params={"scope": "user", "active_only": "true", "limit": 5, "offset": 0},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params) == {
        "active_only": "true",
        "limit": "5",
        "offset": "0",
    }


@respx.mock
def test_list_directives_omits_unset_params(client, juan, tenant):
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"directives": []}))

    response = client.get(
        "/v1/directives", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params) == {}


@respx.mock
def test_get_directive(client, juan, tenant):
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/{DIR_ID}"
    ).mock(return_value=httpx.Response(200, json={"id": DIR_ID, "name": "n"}))

    response = client.get(
        f"/v1/directives/{DIR_ID}", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"id": DIR_ID, "name": "n"}


@respx.mock
def test_a_missing_directive_is_a_404(client, juan, tenant):
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/{DIR_ID}"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    response = client.get(
        f"/v1/directives/{DIR_ID}", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DIRECTIVE_NOT_FOUND"


@respx.mock
@pytest.mark.parametrize(
    "malformed_id",
    # "%2e%2e" (not a literal "..") on purpose: httpx -- used by both real
    # callers of this kind and by TestClient here -- applies RFC 3986
    # dot-segment removal client-side while building the request, so a
    # literal ".." in an f-string URL never survives to reach our route at
    # all (confirmed empirically: it collapses the request to `/v1` before
    # dispatch, a framework-level protection this test cannot exercise).
    # Percent-encoding is exactly how a raw HTTP client (curl, a hand-rolled
    # request) would still deliver a literal ".." to our server, so this is
    # the case that actually depends on `_require_uuid` at the client layer.
    ["%2e%2e", "not-a-uuid"],
)
def test_get_directive_rejects_a_non_uuid_id_locally(client, juan, tenant, malformed_id):
    """directive_id is Hindsight-minted (UUID), never caller-managed like
    document_id -- without local validation, a raw ".." reaching Hindsight's
    URL merge would resolve one level up onto the bank itself (GET there
    leaks bank metadata; DELETE there is delete_bank). No respx route is
    registered on purpose: any outbound call at all fails this test via
    respx's AllMockedAssertionError, not just the assertion below."""
    response = client.get(
        f"/v1/directives/{malformed_id}", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DIRECTIVE_NOT_FOUND"


@respx.mock
def test_update_directive(client, juan, tenant):
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/{DIR_ID}"
    ).mock(return_value=httpx.Response(200, json={"id": DIR_ID, "content": "new"}))

    response = client.patch(
        f"/v1/directives/{DIR_ID}",
        json={"scope": "user", "content": "new"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"content": "new"}


@respx.mock
def test_delete_directive(client, juan, tenant):
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/{DIR_ID}$"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.request(
        "DELETE",
        f"/v1/directives/{DIR_ID}",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.call_count == 1


@respx.mock
def test_bank_id_is_stripped_from_a_directive_response(client, juan, tenant):
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/{DIR_ID}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": DIR_ID,
                "bank_id": "user_leaked",
                "meta": {"bank_id": "user_leaked_nested"},
            },
        )
    )

    body = client.get(
        f"/v1/directives/{DIR_ID}", params={"scope": "user"}, headers=juan["headers"]
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)
    assert "user_leaked_nested" not in str(body)


def test_directives_route_on_an_unknown_slug_creates_no_project(
    client, juan, tenant, session
):
    """A directive route is maintenance on something that already exists
    (SPEC §11.3), never first-touch creation -- an unknown slug must 404 and
    leave nothing behind."""
    from memory.models import Project

    response = client.get(
        "/v1/directives",
        params={"scope": "project", "project_slug": "typo-slug"},
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


def test_list_directives_rejects_a_negative_limit(client, juan, tenant):
    response = client.get(
        "/v1/directives",
        params={"scope": "user", "limit": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_list_directives_rejects_a_negative_offset(client, juan, tenant):
    response = client.get(
        "/v1/directives",
        params={"scope": "user", "offset": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


# --- IDOR: SPEC §20.1 -- an already-authorized bank is a precondition for
# every route below, not something a directive_id can substitute for.
# `payments-api` is owned by juan; alice has no access to it. Each test
# was verified by mutation: stubbing `memory.projects.authorize` to return
# unconditionally (skip authorization) turns every one of these from a 403
# into the mocked 2xx below, failing the assertion, not the mock-matched
# check -- proving each test is load-bearing on its own authorization call
# rather than merely on a route existing.


@respx.mock
def test_idor_create_directive_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    create = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives$"
    ).mock(return_value=httpx.Response(201, json={"id": DIR_ID}))

    response = client.post(
        "/v1/directives",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "name": "n",
            "content": "c",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert create.call_count == 0


@respx.mock
def test_idor_update_directive_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    update = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": DIR_ID}))

    response = client.patch(
        f"/v1/directives/{DIR_ID}",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "content": "hijacked",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert update.call_count == 0


@respx.mock
def test_idor_delete_directive_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    delete = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.request(
        "DELETE",
        f"/v1/directives/{DIR_ID}",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert delete.call_count == 0


@respx.mock
def test_idor_list_directives_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    listed = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"directives": []}))

    response = client.get(
        "/v1/directives",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert listed.call_count == 0


@respx.mock
def test_idor_get_directive_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    get = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives/[^/]+"
    ).mock(return_value=httpx.Response(200, json={"id": DIR_ID}))

    response = client.get(
        f"/v1/directives/{DIR_ID}",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert get.call_count == 0


# --- SPEC §14's authorization table has three rows -- owner, group member,
# master key -- and every test above only ever exercised owner (juan) vs
# stranger (alice). The group-member row is exactly what a well-meaning
# "tighten this to the owner" refactor would break without a test noticing;
# both were verified live before writing these (see plan5-final-report.md).


@respx.mock
def test_a_group_member_who_is_not_the_owner_can_manage_directives(
    client, juan, master_headers, tenant
):
    bob = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    bob_key = client.post(
        f"/v1/users/{bob}/keys", json={}, headers=master_headers
    ).json()["key"]
    client.post("/v1/groups", json={"id": "grp_payments"}, headers=master_headers)
    client.put(f"/v1/groups/grp_payments/members/{bob}", headers=master_headers)
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "group", "id": "grp_payments"},
        headers=juan["headers"],
    )
    create = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives$"
    ).mock(return_value=httpx.Response(201, json={"id": DIR_ID}))

    response = client.post(
        "/v1/directives",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "name": "n",
            "content": "c",
        },
        headers={"Authorization": f"Bearer {bob_key}"},
    )

    assert response.status_code == 201
    assert create.call_count == 1


@respx.mock
def test_a_master_key_can_manage_directives_on_any_bank(client, juan, master_headers, tenant):
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    create = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/directives$"
    ).mock(return_value=httpx.Response(201, json={"id": DIR_ID}))

    response = client.post(
        "/v1/directives",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "name": "n",
            "content": "c",
        },
        headers=master_headers,
    )

    assert response.status_code == 201
    assert create.call_count == 1
