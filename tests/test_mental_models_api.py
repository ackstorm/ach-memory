import json

import httpx
import pytest
import respx

from memory.errors import MentalModelNotFound
from memory.hindsight.client import HindsightClient

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


# Hindsight-minted shape (measured live, hindsight-api 0.9.1, 2026-08-22):
# "mm-" + 32 hex chars -- NOT a UUID. The old value here
# ("22222222-2222-...") was a real UUID and hid the bug this file's
# malformed-id test used to assert backwards: `_require_uuid` rejected every
# ACTUAL mental_model_id locally before the round trip, so get/update/
# delete/refresh/clear 404'd forever in production while every mocked test
# still passed, because no mock here ever used a real-shaped id.
MM_ID = "mm-de7d4702fed04d5086bb45d43140d424"


@respx.mock
def test_create_mental_model(client, juan, tenant):
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$"
    ).mock(return_value=httpx.Response(201, json={"id": MM_ID, "name": "n"}))

    response = client.post(
        "/v1/mental-models",
        json={"scope": "user", "name": "n", "source_query": "how do we deploy?"},
        headers=juan["headers"],
    )

    assert response.status_code == 201
    assert response.json()["result"] == {"id": MM_ID, "name": "n"}
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"name": "n", "source_query": "how do we deploy?"}


def test_create_mental_model_rejects_tags_the_caller_supplies(client, juan, tenant):
    """`tags` is Hindsight's in-bank visibility scope, not a field on
    CreateMentalModelRequest. ScopedRequest's `extra="forbid"` means a
    caller-supplied `tags` is a 422, not a silent drop."""
    response = client.post(
        "/v1/mental-models",
        json={
            "scope": "user",
            "name": "n",
            "source_query": "q",
            "tags": ["some-scope"],
        },
        headers=juan["headers"],
    )

    assert response.status_code == 422, response.text


@respx.mock
def test_create_mental_model_omits_trigger_when_not_supplied(client, juan, tenant):
    """SPEC §14.5: an omitted trigger sends no `trigger` key at all -- not
    `{}`, not a default. Hindsight's own defaults (`refresh_cron=null`,
    `refresh_after_consolidation=false`) then mean no automatic refresh ever
    happens, the cheapest and safest behavior."""
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$"
    ).mock(return_value=httpx.Response(201, json={"id": MM_ID}))

    client.post(
        "/v1/mental-models",
        json={"scope": "user", "name": "n", "source_query": "q"},
        headers=juan["headers"],
    )

    sent = json.loads(route.calls.last.request.content)
    assert "trigger" not in sent
    assert sent == {"name": "n", "source_query": "q"}


@respx.mock
def test_create_mental_model_passes_trigger_verbatim(client, juan, tenant):
    """A caller who sets `refresh_after_consolidation: true` is choosing real,
    unattributable spend (§19.4) -- the wrapper neither defaults nor validates
    the shape, it just forwards exactly what was sent."""
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$"
    ).mock(return_value=httpx.Response(201, json={"id": MM_ID}))
    trigger = {"refresh_after_consolidation": True, "refresh_cron": "0 3 * * *"}

    client.post(
        "/v1/mental-models",
        json={
            "scope": "user",
            "name": "n",
            "source_query": "q",
            "trigger": trigger,
        },
        headers=juan["headers"],
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["trigger"] == trigger


@respx.mock
def test_create_mental_model_forwards_max_tokens(client, juan, tenant):
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$"
    ).mock(return_value=httpx.Response(201, json={"id": MM_ID}))

    client.post(
        "/v1/mental-models",
        json={"scope": "user", "name": "n", "source_query": "q", "max_tokens": 2000},
        headers=juan["headers"],
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == 2000


@respx.mock
def test_list_mental_models_forwards_query_params(client, juan, tenant):
    # `(\?|$)` and no trailing id segment: an unanchored regex here also
    # matches `.../mental-models/{id}` -- the exact overlap trap the task
    # brief calls out.
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"mental_models": []}))

    response = client.get(
        "/v1/mental-models",
        params={"scope": "user", "detail": "full", "limit": 5, "offset": 0},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params) == {
        "detail": "full",
        "limit": "5",
        "offset": "0",
    }


@respx.mock
def test_list_mental_models_omits_unset_params(client, juan, tenant):
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"mental_models": []}))

    response = client.get(
        "/v1/mental-models", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert dict(route.calls.last.request.url.params) == {}


@respx.mock
def test_list_does_not_hit_a_specific_mental_model_route(client, juan, tenant):
    """Registers both the list route and a specific-id route with disjoint
    mocked bodies; asserts on which one actually got called (per the task
    brief: never trust that *a* mock matched, trust the URL that was hit)."""
    list_route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"mental_models": []}))
    get_route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(return_value=httpx.Response(200, json={"id": MM_ID}))

    response = client.get(
        "/v1/mental-models", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"mental_models": []}
    assert list_route.call_count == 1
    assert get_route.call_count == 0


@respx.mock
def test_get_mental_model(client, juan, tenant):
    list_route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"mental_models": []}))
    get_route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(return_value=httpx.Response(200, json={"id": MM_ID, "name": "n"}))

    response = client.get(
        f"/v1/mental-models/{MM_ID}", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"id": MM_ID, "name": "n"}
    assert get_route.call_count == 1
    assert list_route.call_count == 0


@respx.mock
def test_a_missing_mental_model_is_a_404(client, juan, tenant):
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    response = client.get(
        f"/v1/mental-models/{MM_ID}", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MENTAL_MODEL_NOT_FOUND"


@respx.mock
@pytest.mark.parametrize(
    "mental_model_id",
    [
        "%2e%2e",
        "%252e%252e%255Cmemories",
        "model%2523fragment",
    ],
)
def test_get_mental_model_rejects_dot_segment_traversal_locally(
    client, juan, tenant, mental_model_id
):
    """mental_model_id is Hindsight-minted as `mm-<32 hex>`, NOT a UUID
    (measured live) -- this file used to assert the opposite premise here,
    which meant `_require_uuid` silently 404'd every real mental_model_id
    before the round trip ever happened (get/update/delete/refresh/clear all
    broken in production; only create/list worked). The actual hazard a
    caller-adjacent id needs guarding against is URL traversal, same as
    document_id: "%2e%2e", not a literal "..", because httpx (both real
    callers of this kind and TestClient here) resolves a literal dot-segment
    client-side before the request ever reaches our route; percent-encoding
    is how a raw HTTP client would still deliver one. No respx route is
    registered on purpose, so any outbound call fails via respx's own
    AllMockedAssertionError, not just the assertion below."""
    response = client.get(
        f"/v1/mental-models/{mental_model_id}",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MENTAL_MODEL_NOT_FOUND"


@respx.mock
@pytest.mark.parametrize(
    "mental_model_id",
    [
        "%2e%2e%2fmemories",
        "%2E%2E%2Fmemories",
        "%252e%252e%252fmemories",
        "%252e%252e%255cmemories",
        "model%253ffilter",
    ],
)
def test_hindsight_client_rejects_encoded_mental_model_syntax_before_request(
    mental_model_id,
):
    """No respx route is registered: an outbound request fails this test."""
    hindsight = HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")

    with pytest.raises(MentalModelNotFound):
        hindsight.get_mental_model("bank_1", mental_model_id)


@respx.mock
def test_get_mental_model_with_a_non_hex32_id_reaches_hindsight(client, juan, tenant):
    """The corrected other half of the test above: an id that merely does not
    match Hindsight's `mm-<32 hex>` shape (but contains no traversal/control
    characters) is NOT rejected locally -- it is forwarded to Hindsight like
    any other id, and Hindsight's own 404 comes back as MENTAL_MODEL_NOT_FOUND.
    Before the fix this raised locally instead, and the route mocked below was
    never called."""
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/not-a-real-id$"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    response = client.get(
        "/v1/mental-models/not-a-real-id",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MENTAL_MODEL_NOT_FOUND"
    assert route.call_count == 1


@respx.mock
def test_update_mental_model(client, juan, tenant):
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(return_value=httpx.Response(200, json={"id": MM_ID}))

    response = client.patch(
        f"/v1/mental-models/{MM_ID}",
        json={"scope": "user", "source_query": "new query"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"source_query": "new query"}


@respx.mock
def test_delete_mental_model(client, juan, tenant):
    route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.request(
        "DELETE",
        f"/v1/mental-models/{MM_ID}",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.call_count == 1


@respx.mock
def test_refresh_uses_the_refresh_suffix_not_the_bare_mental_model_path(
    client, juan, tenant
):
    """Same overlap trap as operations' cancel/delete-suffix test: an
    implementation that dropped the `/refresh` suffix would still find a
    mock to match if the bare-path mock were registered without a `$`
    anchor. Both routes are registered here with disjoint bodies so a
    regression is caught by which URL was actually hit, not by "a" mock
    matching."""
    bare = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(return_value=httpx.Response(200, json={"id": MM_ID}))
    refresh = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/refresh$"
    ).mock(return_value=httpx.Response(200, json={"status": "refreshing"}))

    response = client.post(
        f"/v1/mental-models/{MM_ID}/refresh",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"status": "refreshing"}
    assert refresh.call_count == 1
    assert bare.call_count == 0


@respx.mock
def test_clear_uses_the_clear_suffix_not_the_refresh_suffix(client, juan, tenant):
    refresh = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/refresh$"
    ).mock(return_value=httpx.Response(200, json={"status": "refreshing"}))
    clear = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/clear$"
    ).mock(return_value=httpx.Response(200, json={"status": "cleared"}))

    response = client.post(
        f"/v1/mental-models/{MM_ID}/clear",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"status": "cleared"}
    assert clear.call_count == 1
    assert refresh.call_count == 0


def test_no_dry_run_refresh_surface_exists(client):
    """SPEC §11.7: dry-run-refresh costs exactly the same as a real refresh,
    so it is never wired on any surface, REST included."""
    schema = client.get("/openapi.json").json()
    assert not any(
        "dry-run" in path or "dry_run" in path for path in schema["paths"]
    )
    refresh_op = schema["paths"]["/v1/mental-models/{mental_model_id}/refresh"][
        "post"
    ]
    params = {p["name"] for p in refresh_op.get("parameters", [])}
    assert "dry_run" not in params


@respx.mock
def test_bank_id_is_stripped_from_a_mental_model_response(client, juan, tenant):
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": MM_ID,
                "bank_id": "user_leaked",
                "meta": {"bank_id": "user_leaked_nested"},
            },
        )
    )

    body = client.get(
        f"/v1/mental-models/{MM_ID}", params={"scope": "user"}, headers=juan["headers"]
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)
    assert "user_leaked_nested" not in str(body)


def test_mental_models_route_on_an_unknown_slug_creates_no_project(
    client, juan, tenant, session
):
    from memory.models import Project

    response = client.get(
        "/v1/mental-models",
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


def test_list_mental_models_rejects_a_negative_limit(client, juan, tenant):
    response = client.get(
        "/v1/mental-models",
        params={"scope": "user", "limit": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_list_mental_models_rejects_a_negative_offset(client, juan, tenant):
    response = client.get(
        "/v1/mental-models",
        params={"scope": "user", "offset": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_max_tokens_below_the_upstream_floor_is_a_422(client, juan, tenant):
    """Upstream is FastAPI: its schema rejection is a 422, which _request
    turns into a 502 blaming the backend. Bound it here (finding I6). Deleting
    the `ge=256, le=8192` bound on `CreateMentalModelRequest.max_tokens` turns
    this red: pydantic accepts 100 and the 422 never fires."""
    response = client.post(
        "/v1/mental-models",
        json={"scope": "user", "name": "x", "source_query": "y", "max_tokens": 100},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_max_tokens_above_the_upstream_ceiling_is_a_422(client, juan, tenant):
    """The ceiling half of the same bound, which only the update route pinned.

    Deleting `le=8192` from `CreateMentalModelRequest.max_tokens` turns this
    red: pydantic accepts 100000, it reaches upstream, and hindsight-api's own
    `Field(ge=256, le=8192)` rejects it as a 422 that we now surface as
    UPSTREAM_REJECTED -- a worse message for the same caller mistake, and a
    round trip spent to learn it.
    """
    response = client.post(
        "/v1/mental-models",
        json={"scope": "user", "name": "x", "source_query": "y", "max_tokens": 100000},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_max_tokens_below_the_upstream_floor_on_update_is_a_422(client, juan, tenant):
    """Sibling of the create-route test above, on PATCH (review finding F4):
    the code applies `ge=256, le=8192` to `UpdateMentalModelRequest.max_tokens`
    too, but only the create route had a test, so a caller tidying the update
    model back to `int | None = None` regressed I6 on PATCH with every other
    test green. Deleting the `ge=256, le=8192` bound on
    `UpdateMentalModelRequest.max_tokens` turns this red: pydantic accepts 100
    and the 422 never fires."""
    response = client.patch(
        f"/v1/mental-models/{MM_ID}",
        json={"scope": "user", "max_tokens": 100},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_max_tokens_above_the_upstream_ceiling_on_update_is_a_422(client, juan, tenant):
    """Same sibling gap as the floor test above, for the `le=8192` ceiling
    (review finding F4). Deleting `le=8192` on
    `UpdateMentalModelRequest.max_tokens` turns this red: pydantic accepts
    9000 and the 422 never fires."""
    response = client.patch(
        f"/v1/mental-models/{MM_ID}",
        json={"scope": "user", "max_tokens": 9000},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_an_unknown_trigger_mode_on_update_is_a_422(client, juan, tenant):
    """Sibling of the create-route test below (review finding F4): the code
    types `UpdateMentalModelRequest.trigger` the same way as create's, but
    only create had a test, so reverting `trigger` on the update model back
    to a free-form dict regressed I6 on PATCH with every other test green.
    Deleting the `Literal["full", "delta"]` annotation on
    `MentalModelTrigger.mode` (or reverting `UpdateMentalModelRequest.trigger`
    back to a free-form dict) turns this red."""
    response = client.patch(
        f"/v1/mental-models/{MM_ID}",
        json={"scope": "user", "trigger": {"mode": "incremental"}},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_an_unknown_trigger_mode_is_a_422(client, juan, tenant):
    """Upstream types `trigger.mode` as Literal["full","delta"]; an unknown
    value 422s upstream and this repeats that at the boundary. Deleting the
    `Literal["full", "delta"]` annotation on `MentalModelTrigger.mode` (or
    reverting `trigger` back to a free-form dict) turns this red."""
    response = client.post(
        "/v1/mental-models",
        json={
            "scope": "user", "name": "x", "source_query": "y",
            "trigger": {"mode": "incremental"},
        },
        headers=juan["headers"],
    )

    assert response.status_code == 422


# --- IDOR: SPEC §20.1, verified by mutation the same way as
# test_directives_api.py -- stubbing `memory.projects.authorize` to return
# unconditionally turns each 403 below into the mocked 2xx, failing the
# assertion rather than "no mock matched".


@respx.mock
def test_idor_create_mental_model_cannot_reach_an_unauthorized_bank(
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$"
    ).mock(return_value=httpx.Response(201, json={"id": MM_ID}))

    response = client.post(
        "/v1/mental-models",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "name": "n",
            "source_query": "q",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert create.call_count == 0


@respx.mock
def test_idor_update_mental_model_cannot_reach_an_unauthorized_bank(
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+$"
    ).mock(return_value=httpx.Response(200, json={"id": MM_ID}))

    response = client.patch(
        f"/v1/mental-models/{MM_ID}",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "source_query": "hijacked",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert update.call_count == 0


@respx.mock
def test_idor_delete_mental_model_cannot_reach_an_unauthorized_bank(
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+$"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.request(
        "DELETE",
        f"/v1/mental-models/{MM_ID}",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert delete.call_count == 0


@respx.mock
def test_idor_refresh_mental_model_cannot_reach_an_unauthorized_bank(
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
    refresh = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+/refresh$"
    ).mock(return_value=httpx.Response(200, json={"status": "refreshing"}))

    response = client.post(
        f"/v1/mental-models/{MM_ID}/refresh",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert refresh.call_count == 0


@respx.mock
def test_idor_clear_mental_model_cannot_reach_an_unauthorized_bank(
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
    clear = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+/clear$"
    ).mock(return_value=httpx.Response(200, json={"status": "cleared"}))

    response = client.post(
        f"/v1/mental-models/{MM_ID}/clear",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert clear.call_count == 0


@respx.mock
def test_idor_list_mental_models_cannot_reach_an_unauthorized_bank(
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)"
    ).mock(return_value=httpx.Response(200, json={"mental_models": []}))

    response = client.get(
        "/v1/mental-models",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert listed.call_count == 0


@respx.mock
def test_idor_get_mental_model_cannot_reach_an_unauthorized_bank(
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+$"
    ).mock(return_value=httpx.Response(200, json={"id": MM_ID}))

    response = client.get(
        f"/v1/mental-models/{MM_ID}",
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
def test_a_group_member_who_is_not_the_owner_can_manage_mental_models(
    client, juan, master_headers, tenant
):
    bob = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    bob_key = client.post(
        f"/v1/users/{bob}/keys", json={}, headers=master_headers
    ).json()["key"]
    client.post("/v1/groups", json={"id": "grp_payments"}, headers=master_headers)
    client.put(f"/v1/groups/grp_payments/members/{bob}", headers=master_headers)
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$"
    ).mock(return_value=httpx.Response(201, json={"id": MM_ID}))

    response = client.post(
        "/v1/mental-models",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "name": "n",
            "source_query": "q",
        },
        headers={"Authorization": f"Bearer {bob_key}"},
    )

    assert response.status_code == 201
    assert create.call_count == 1


@respx.mock
def test_a_master_key_can_manage_mental_models_on_any_bank(
    client, juan, master_headers, tenant
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
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$"
    ).mock(return_value=httpx.Response(201, json={"id": MM_ID}))

    response = client.post(
        "/v1/mental-models",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "name": "n",
            "source_query": "q",
        },
        headers=master_headers,
    )

    assert response.status_code == 201
    assert create.call_count == 1


# Measured live against hindsight-api 0.9.1 on 2026-08-28: history is a BARE
# ARRAY, newest first, three keys per entry. The genesis entry carries the
# literal placeholder Hindsight writes at create time, before the first
# generation lands.
HISTORY = [
    {
        "previous_content": "the version before the current one",
        "previous_reflect_response": {"text": "t", "based_on": {"world": []}},
        "changed_at": "2026-08-27T17:35:56.489829+00:00",
    },
    {
        "previous_content": "Generating content...",
        "previous_reflect_response": None,
        "changed_at": "2026-08-27T17:26:17.552227+00:00",
    },
]


@respx.mock
def test_history_is_forwarded_as_a_bare_array_newest_first(client, juan, tenant):
    """The array IS the contract (SPEC §14.5).

    Wrapping it as `{"items": [...]}` to rhyme with this service's own list
    routes would put a reshaping layer in front of the one structure whose
    entire meaning is its ordering -- and an off-by-one there renders a
    version that is wrong but entirely plausible.
    """
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/history$"
    ).mock(return_value=httpx.Response(200, json=HISTORY))

    response = client.get(
        f"/v1/mental-models/{MM_ID}/history",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert isinstance(result, list), "a dict here means someone added an envelope"
    assert result == HISTORY
    assert result[0]["changed_at"] > result[1]["changed_at"], "newest first"


@respx.mock
def test_history_uses_the_history_suffix_not_the_bare_mental_model_path(
    client, juan, tenant
):
    """Same overlap trap as refresh: both routes are GET, and an unanchored
    bare-path mock would swallow a call that dropped the suffix. Disjoint
    bodies, so the regression shows up as which URL was hit."""
    bare = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}$"
    ).mock(return_value=httpx.Response(200, json={"id": MM_ID}))
    history = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/history$"
    ).mock(return_value=httpx.Response(200, json=HISTORY))

    response = client.get(
        f"/v1/mental-models/{MM_ID}/history",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert history.call_count == 1
    assert bare.call_count == 0


@respx.mock
def test_history_of_a_model_that_never_changed_is_an_empty_array_not_an_error(
    client, juan, tenant
):
    """A model created and never refreshed has no previous versions. That is
    an answer, not a failure -- a 404 or a 502 here would make "never changed"
    indistinguishable from "no such model"."""
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/history$"
    ).mock(return_value=httpx.Response(200, json=[]))

    response = client.get(
        f"/v1/mental-models/{MM_ID}/history",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"] == []


@respx.mock
def test_history_redacts_the_bank_id_inside_the_array(client, juan, tenant):
    """§16.4 redaction is absolute and must not depend on the response being
    an object: the bank_id reaches this route embedded in entries, one list
    level deeper than every other route redacts."""
    seen: list[str] = []

    def _echo_the_bank_id(request: httpx.Request) -> httpx.Response:
        bank_id = request.url.path.split("/")[4]
        seen.append(bank_id)
        return httpx.Response(
            200,
            json=[
                {
                    "previous_content": "c",
                    "previous_reflect_response": None,
                    "changed_at": "2026-08-27T17:35:56.489829+00:00",
                    "chunk_id": f"{bank_id}:0",
                }
            ],
        )

    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/history$"
    ).mock(side_effect=_echo_the_bank_id)

    response = client.get(
        f"/v1/mental-models/{MM_ID}/history",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    body = json.dumps(response.json()["result"])
    assert seen and seen[0] not in body
    assert "REDACTED" in body


@respx.mock
def test_history_of_an_unknown_mental_model_is_a_404(client, juan, tenant):
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/{MM_ID}/history$"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    response = client.get(
        f"/v1/mental-models/{MM_ID}/history",
        params={"scope": "user"},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MENTAL_MODEL_NOT_FOUND"
