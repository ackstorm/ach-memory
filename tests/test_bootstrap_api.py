import httpx
import pytest
import respx

from memory import mental_model_service
from memory.mental_model_service import reconcile_builtin

BASE = "http://hindsight.test"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(f"/v1/users/{user_id}/keys", json={}, headers=master_headers).json()["key"]
    return {"user_id": user_id, "headers": _headers(key)}


def _mock_create(counter: list[int]):
    def _respond(request: httpx.Request) -> httpx.Response:
        counter[0] += 1
        return httpx.Response(
            201,
            json={
                "mental_model_id": f"mm-upstream-{counter[0]}",
                "operation_id": f"op-upstream-{counter[0]}",
            },
        )

    return respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        side_effect=_respond
    )


@respx.mock
def test_bootstrap_creates_the_user_builtin(client, juan, monkeypatch):
    # The app fixture stubs reconcile_builtin out by default for unrelated
    # route tests -- this test is specifically about what it does.
    monkeypatch.setattr(mental_model_service, "reconcile_builtin", reconcile_builtin)
    counter = [0]
    _mock_create(counter)
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    response = client.post("/v1/bootstrap", json={}, headers=juan["headers"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_model"]["model_key"] == "user-context"
    assert body["project_model"] is None
    assert "bank_id" not in response.text
    assert "upstream_model_id" not in response.text


@respx.mock
def test_bootstrap_is_idempotent(client, juan, monkeypatch):
    monkeypatch.setattr(mental_model_service, "reconcile_builtin", reconcile_builtin)
    counter = [0]
    _mock_create(counter)
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    client.post("/v1/bootstrap", json={}, headers=juan["headers"])
    client.post("/v1/bootstrap", json={}, headers=juan["headers"])

    assert counter[0] == 1


@respx.mock
def test_bootstrap_with_a_project_slug_creates_it_owned_by_the_caller(
    client, juan, session, monkeypatch
):
    from memory.models import Project, ProjectSlug

    monkeypatch.setattr(mental_model_service, "reconcile_builtin", reconcile_builtin)
    counter = [0]
    _mock_create(counter)
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models(\?|$)").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    response = client.post(
        "/v1/bootstrap", json={"project_slug": "Acme"}, headers=juan["headers"]
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_slug"] == "acme"
    assert body["project_owner"] == {"type": "user", "id": juan["user_id"]}
    assert body["project_model"]["model_key"] == "project-context"
    assert counter[0] == 2

    slug_row = session.get(ProjectSlug, (session.query(Project).first().tenant_id, "acme"))
    assert slug_row is not None


def test_bootstrap_rejects_an_unknown_field(client, juan):
    response = client.post(
        "/v1/bootstrap", json={"scope": "user"}, headers=juan["headers"]
    )

    assert response.status_code == 422, response.text


@respx.mock
def test_bootstrap_opt_out_creates_no_models(client, juan):
    create = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(
            201,
            json={
                "mental_model_id": "mm-should-not-be-called",
                "operation_id": "op-should-not-be-called",
            },
        )
    )

    response = client.post(
        "/v1/bootstrap", json={"builtins_enabled": False}, headers=juan["headers"]
    )

    assert response.status_code == 200, response.text
    assert response.json()["user_model"] is None
    assert create.call_count == 0
