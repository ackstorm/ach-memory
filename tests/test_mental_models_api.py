"""SPEC §7: mental models addressed by ACH `model_key`, never a raw upstream id.

Every route here still forwards to the same Hindsight mental-model endpoints
as before (unchanged upstream contract) -- what changed is ACH's own
addressing and response shape, so these tests mock the identical Hindsight
URL patterns the old raw-ID proxy tests used, and assert on the new
model_key-based public contract instead.
"""

import json
import uuid

import httpx
import pytest
import respx

from memory.models import User

BASE = "http://hindsight.test"
CREATE_OPERATION_ID = "4d4a6f25-2d09-40e1-95d7-75cfb9eb7f1b"

REQUIRED_TAGS = ["schema:ach-retain-v1", "validity:indefinite"]
TRIGGER = {"mode": "delta", "refresh_after_consolidation": True, "min_refresh_interval_seconds": 300}


@pytest.fixture
def juan(new_user) -> dict:
    """An ordinary external user. Nothing mints a user any more -- `new_user()`
    provisions one the way a real first-time caller arrives."""
    return new_user()


@pytest.fixture
def alice(new_user) -> dict:
    """A second, unrelated ordinary user -- the IDOR tests' attacker."""
    return new_user()


def _make_project(client, headers, slug: str) -> None:
    response = client.post("/v1/projects", json={"project_slug": slug}, headers=headers)
    assert response.status_code == 201, response.text


def _create_body(**overrides) -> dict:
    body = {
        "scope": "user",
        "name": "review-context",
        "source_query": "Summarize review conventions.",
        "source_tags": ["repo:group/app"],
        "max_tokens": 256,
        "trigger": TRIGGER,
        "operation_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _mock_create(mm_id: str = "mm-upstream-1"):
    return respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201,
            json={"mental_model_id": mm_id, "operation_id": CREATE_OPERATION_ID},
        )
    )


# ---------------------------------------------------------------------------
# Create: logical key, hidden upstream id, closed contract
# ---------------------------------------------------------------------------


@respx.mock
def test_model_create_returns_logical_key_not_upstream_id(client, juan):
    _make_project(client, juan["headers"], "acme")
    _mock_create()

    response = client.post(
        "/v1/mental-models",
        json=_create_body(scope="project", project_slug="acme"),
        headers=juan["headers"],
    )

    assert response.status_code == 201, response.text
    assert response.json()["model_key"].startswith("mm_")
    assert "upstream_model_id" not in response.text
    assert "bank_id" not in response.text


@respx.mock
def test_create_forwards_the_internal_locator_name_not_the_display_name(client, juan):
    route = _mock_create()

    client.post("/v1/mental-models", json=_create_body(name="my-display-name"), headers=juan["headers"])

    sent = json.loads(route.calls.last.request.content)
    assert sent["name"].startswith("ach:mm_")
    assert sent["name"] != "my-display-name"


def test_create_refuses_a_caller_that_names_a_server_owned_tag(client, juan):
    """The required pair is composed server-side; a caller naming either half
    is refused the same way any reserved namespace is, on this surface and on
    MCP, because both run the one `memory.tags` gate."""
    response = client.post(
        "/v1/mental-models",
        json=_create_body(source_tags=["schema:ach-retain-v1"]),
        headers=juan["headers"],
    )

    # INVALID_TAG, not a bare 422: the one shared `memory.tags` gate raises a
    # typed DomainError, so REST and MCP report a reserved namespace with the
    # same SPEC §18 code instead of two different shapes.
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_TAG"


@respx.mock
def test_create_composes_the_required_pair_into_what_it_sends_upstream(client, juan):
    """The model must select on the pair even though the caller never typed
    it -- otherwise a narrowed model would also draw on expiring claims."""
    route = _mock_create()
    response = client.post(
        "/v1/mental-models",
        json=_create_body(source_tags=["repo:group/app"]),
        headers=juan["headers"],
    )

    assert response.status_code == 201, response.text
    sent = json.loads(route.calls.last.request.content)
    assert sorted(sent["tags"]) == sorted([*REQUIRED_TAGS, "repo:group/app"])
    assert response.json()["source_tags"] == sorted(
        [*REQUIRED_TAGS, "repo:group/app"]
    )


def test_create_rejects_caller_supplied_tags_field(client, juan):
    """`tags` is Hindsight's in-bank visibility scope, not part of this
    closed request -- extra="forbid" makes it a 422, not a silent drop."""
    body = _create_body()
    body["tags"] = ["some-scope"]

    response = client.post("/v1/mental-models", json=body, headers=juan["headers"])

    assert response.status_code == 422, response.text


def test_create_requires_operation_id(client, juan):
    body = _create_body()
    del body["operation_id"]

    response = client.post("/v1/mental-models", json=body, headers=juan["headers"])

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("max_tokens", [100, 100000])
def test_max_tokens_outside_the_upstream_bound_is_a_422(client, juan, max_tokens):
    response = client.post(
        "/v1/mental-models", json=_create_body(max_tokens=max_tokens), headers=juan["headers"]
    )

    assert response.status_code == 422, response.text


def test_an_unknown_trigger_mode_is_a_422(client, juan):
    response = client.post(
        "/v1/mental-models",
        json=_create_body(trigger={"mode": "incremental"}),
        headers=juan["headers"],
    )

    assert response.status_code == 422, response.text


def test_a_negative_refresh_interval_is_a_422(client, juan):
    body = _create_body()
    body["trigger"] = {"mode": "delta", "min_refresh_interval_seconds": -5}
    response = client.post("/v1/mental-models", json=body, headers=juan["headers"])
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# List / get: registry metadata, no project creation on read
# ---------------------------------------------------------------------------


@respx.mock
def test_create_then_get_round_trips_by_model_key(client, juan):
    _mock_create()
    created = client.post("/v1/mental-models", json=_create_body(), headers=juan["headers"]).json()
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{CREATE_OPERATION_ID}$"
    ).mock(
        return_value=httpx.Response(
            200, json={"operation_id": CREATE_OPERATION_ID, "status": "completed"}
        )
    )
    # Registered before the broad list mock below: respx matches in
    # registration order.
    model_get = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1$"
    ).mock(return_value=httpx.Response(200, json={"id": "mm-upstream-1", "content": "ready text"}))
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    response = client.get(
        f"/v1/mental-models/{created['model_key']}", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert response.json()["model_key"] == created["model_key"]
    assert response.json()["name"] == created["name"]
    # QA F-18: the get delivers the text once the observed refresh has landed.
    assert response.json()["delivery_state"] == "ready"
    assert response.json()["content"] == "ready text"
    assert model_get.call_count == 1


@respx.mock
def test_get_withholds_content_while_the_refresh_is_still_pending(client, juan):
    _mock_create()
    created = client.post("/v1/mental-models", json=_create_body(), headers=juan["headers"]).json()
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/{CREATE_OPERATION_ID}$"
    ).mock(
        return_value=httpx.Response(
            200, json={"operation_id": CREATE_OPERATION_ID, "status": "pending"}
        )
    )
    model_get = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1$"
    ).mock(return_value=httpx.Response(200, json={"id": "mm-upstream-1", "content": "not yet"}))

    response = client.get(
        f"/v1/mental-models/{created['model_key']}", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 200
    assert response.json()["delivery_state"] == "withheld"
    assert response.json()["content"] is None
    assert model_get.called is False


def test_get_an_unregistered_key_is_a_404(client, juan):
    response = client.get(
        "/v1/mental-models/mm_does_not_exist", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MENTAL_MODEL_NOT_FOUND"


def test_mental_models_route_on_an_unknown_slug_creates_no_project(client, juan, tenant, session):
    from memory.models import Project, ProjectSlug

    response = client.get(
        "/v1/mental-models",
        params={"scope": "project", "project_slug": "typo-slug"},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert session.get(ProjectSlug, (tenant, "typo-slug")) is None
    assert session.query(Project).count() == 0


@respx.mock
def test_list_reports_unknown_upstream_models_without_serializing_them(client, juan):
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "mm-legacy", "name": "some-legacy-model"}]}
        )
    )

    response = client.get("/v1/mental-models", params={"scope": "user"}, headers=juan["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["models"] == []
    assert body["unknown_upstream_count"] == 1
    assert "some-legacy-model" not in response.text


# ---------------------------------------------------------------------------
# Update / delete / refresh
# ---------------------------------------------------------------------------


@respx.mock
def test_update_forwards_only_the_changed_field_upstream(client, juan):
    _mock_create()
    created = client.post("/v1/mental-models", json=_create_body(), headers=juan["headers"]).json()
    route = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1$").mock(
        return_value=httpx.Response(200, json={"id": "mm-upstream-1"})
    )
    # A source_query change is one of the three source-affecting fields, so
    # it also requires and submits a refresh (SPEC §6.4) -- unlike a
    # display-name-only update, which never reaches this route.
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1/refresh$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op-update-refresh"})
    )

    response = client.patch(
        f"/v1/mental-models/{created['model_key']}",
        json={"scope": "user", "source_query": "new query", "operation_id": str(uuid.uuid4())},
        headers=juan["headers"],
    )

    assert response.status_code == 200, response.text
    assert response.json()["source_query"] == "new query"
    assert response.json()["delivery_state"] == "withheld"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"source_query": "new query"}


def test_update_requires_operation_id(client, juan):
    response = client.patch(
        "/v1/mental-models/mm_whatever", json={"scope": "user", "name": "x"}, headers=juan["headers"]
    )

    assert response.status_code == 422, response.text


@respx.mock
def test_delete_by_model_key_is_204_and_removes_only_its_mapped_model(client, juan):
    _mock_create()
    created = client.post("/v1/mental-models", json=_create_body(), headers=juan["headers"]).json()
    delete_route = respx.delete(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1$"
    ).mock(return_value=httpx.Response(200, json={"deleted": True}))

    response = client.request(
        "DELETE",
        f"/v1/mental-models/{created['model_key']}",
        params={"scope": "user", "operation_id": str(uuid.uuid4())},
        headers=juan["headers"],
    )

    assert response.status_code == 204
    assert delete_route.call_count == 1

    again = client.get(
        f"/v1/mental-models/{created['model_key']}", params={"scope": "user"}, headers=juan["headers"]
    )
    assert again.status_code == 404


def test_delete_requires_operation_id(client, juan):
    response = client.request(
        "DELETE", "/v1/mental-models/mm_whatever", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 422, response.text


def test_builtin_cannot_be_deleted_by_custom_route(client, juan, session, tenant):
    from memory import model_registry
    from memory.builtin_models import USER_CONTEXT
    from memory.retained_records import LogicalBankRef

    user = session.get(User, juan["user_id"])
    bank = LogicalBankRef(tenant, "user", user.id, None, user.bank_id)
    definition = USER_CONTEXT
    model_registry.register_model(
        session,
        bank,
        origin="builtin",
        model_key=definition.key,
        name="User context",
        source_query=definition.source_query,
        source_tags=list(definition.source_tags),
        tags_match=definition.tags_match,
        max_tokens=definition.max_tokens,
        trigger=dict(definition.trigger),
        builtin_key=definition.key,
        definition_version=definition.version,
        delivery_state="ready",
    )
    session.commit()

    response = client.delete(
        "/v1/mental-models/user-context",
        headers=juan["headers"],
        params={"scope": "user", "operation_id": str(uuid.uuid4())},
    )

    assert response.status_code == 409


@respx.mock
def test_refresh_withholds_delivery_until_the_operation_completes(client, juan):
    _mock_create()
    created = client.post("/v1/mental-models", json=_create_body(), headers=juan["headers"]).json()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-upstream-1/refresh$").mock(
        return_value=httpx.Response(200, json={"operation_id": "op-123"})
    )

    response = client.post(
        f"/v1/mental-models/{created['model_key']}/refresh",
        params={"scope": "user", "operation_id": str(uuid.uuid4())},
        headers=juan["headers"],
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivery_state"] == "withheld"


def test_refresh_requires_operation_id(client, juan):
    response = client.post(
        "/v1/mental-models/mm_whatever/refresh", params={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# IDOR: SPEC §20.1, verified by call_count the same way as test_directives_api.py
# ---------------------------------------------------------------------------


@respx.mock
def test_idor_create_cannot_reach_an_unauthorized_project_bank(client, juan, alice):
    _make_project(client, juan["headers"], "payments-api")
    create = _mock_create()

    response = client.post(
        "/v1/mental-models",
        json=_create_body(scope="project", project_slug="payments-api"),
        headers=alice["headers"],
    )

    assert response.status_code == 404
    assert create.call_count == 0


@respx.mock
def test_idor_get_cannot_reach_an_unauthorized_project_bank(client, juan, alice):
    _make_project(client, juan["headers"], "payments-api")
    get = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+$").mock(
        return_value=httpx.Response(200, json={"id": "mm-1"})
    )

    response = client.get(
        "/v1/mental-models/mm_whatever",
        params={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 404
    assert get.call_count == 0


@respx.mock
def test_idor_update_cannot_reach_an_unauthorized_project_bank(client, juan, alice):
    _make_project(client, juan["headers"], "payments-api")
    update = respx.patch(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+$").mock(
        return_value=httpx.Response(200, json={"id": "mm-1"})
    )

    response = client.patch(
        "/v1/mental-models/mm_whatever",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "source_query": "hijacked",
            "operation_id": str(uuid.uuid4()),
        },
        headers=alice["headers"],
    )

    assert response.status_code == 404
    assert update.call_count == 0


@respx.mock
def test_idor_delete_cannot_reach_an_unauthorized_project_bank(client, juan, alice):
    _make_project(client, juan["headers"], "payments-api")
    delete = respx.delete(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/[^/]+$").mock(
        return_value=httpx.Response(200, json={"deleted": True})
    )

    response = client.request(
        "DELETE",
        "/v1/mental-models/mm_whatever",
        params={
            "scope": "project", "project_slug": "payments-api", "operation_id": str(uuid.uuid4())
        },
        headers=alice["headers"],
    )

    assert response.status_code == 404
    assert delete.call_count == 0


# ---------------------------------------------------------------------------
# SPEC §14's authorization table: owner, IdP-asserted group member, operator
# ---------------------------------------------------------------------------


@respx.mock
def test_a_group_member_who_is_not_the_owner_can_manage_mental_models(client, new_user):
    """Membership is whatever the caller's IdP asserts on THIS request -- there
    is no `group_members` row and no POST /v1/groups any more. Both callers
    here hold `grp_payments` because their identity token says so: the owner
    to be allowed to transfer the project to that group, bob to reach it.
    """
    owner = new_user(groups=("grp_payments",))
    bob = new_user(groups=("grp_payments",))
    _make_project(client, owner["headers"], "payments-api")
    transferred = client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "group", "id": "grp_payments"},
        headers=owner["headers"],
    )
    assert transferred.status_code == 200, transferred.text
    create = _mock_create()

    response = client.post(
        "/v1/mental-models",
        json=_create_body(scope="project", project_slug="payments-api"),
        headers=bob["headers"],
    )

    assert response.status_code == 201, response.text
    assert create.call_count == 1


@respx.mock
def test_a_non_member_cannot_manage_a_groups_mental_models(client, new_user):
    """The other half of the IdP-group rule: a caller whose token does NOT
    assert `grp_payments` gets the same not-found the discovery boundary gives
    any unauthorized caller, and never reaches Hindsight. Without this, the
    test above would pass just as well if group membership were ignored
    entirely."""
    owner = new_user(groups=("grp_payments",))
    outsider = new_user()
    _make_project(client, owner["headers"], "payments-api")
    client.patch(
        "/v1/projects/payments-api/owner",
        json={"type": "group", "id": "grp_payments"},
        headers=owner["headers"],
    )
    create = _mock_create()

    response = client.post(
        "/v1/mental-models",
        json=_create_body(scope="project", project_slug="payments-api"),
        headers=outsider["headers"],
    )

    assert response.status_code == 404, response.text
    assert create.call_count == 0


@respx.mock
def test_an_operator_can_manage_mental_models_on_any_bank(client, juan, master_headers):
    _make_project(client, juan["headers"], "payments-api")
    create = _mock_create()

    response = client.post(
        "/v1/mental-models",
        json=_create_body(scope="project", project_slug="payments-api"),
        headers=master_headers,
    )

    assert response.status_code == 201, response.text
    assert create.call_count == 1
