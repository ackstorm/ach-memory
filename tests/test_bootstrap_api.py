import httpx
import pytest
import respx

from memory import mental_model_service
from memory.mental_model_service import reconcile_builtin

BASE = "http://hindsight.test"


@pytest.fixture
def juan(new_user) -> dict:
    """One ordinary external user, provisioned the way a real first-time caller
    is -- nobody mints a user any more, so there is no route to call and no key
    to hand back.

    `new_user` pre-warms the bank through `POST /v1/bootstrap` while the `app`
    fixture's `reconcile_builtin` stub is still in place, so it registers no
    model and makes no upstream call the counters below could see.
    """
    return new_user()


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
def test_bootstrap_pre_warms_a_project_the_caller_already_owns(
    client, juan, session, monkeypatch
):
    """Bootstrap creates nothing now: `retain` is the one place allowed to mint
    an unknown slug, and it is the place carrying the audited per-caller hourly
    ceiling. What is left is the pre-warm -- an EXISTING project gets its
    retain strategy and its `project-context` built-in."""
    from memory.models import Project, ProjectSlug

    # Created while `reconcile_builtin` is still the app fixture's stub, so
    # the counter below sees only what bootstrap itself does.
    created = client.post(
        "/v1/projects", json={"project_slug": "Acme"}, headers=juan["headers"]
    )
    assert created.status_code == 201, created.text

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
    assert body["project_status"] == "ready"
    # user-context + project-context, one upstream create each.
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


@respx.mock
def test_bootstrap_reports_a_group_owner_as_a_group(client, new_user):
    """`type` was hardcoded to "user" while `owner_type` could be "group", and
    `Literal["user"]` validated it because the literal satisfied the literal.
    So a group-owned project reported the GROUP's id under type "user", and
    the caller had nothing to tell the two apart by."""
    member = new_user(groups=("platform-team",))
    created = client.post(
        "/v1/projects",
        json={"project_slug": "team-owned", "owner": {"type": "group", "id": "platform-team"}},
        headers=member["headers"],
    )
    assert created.status_code == 201, created.text

    response = client.post(
        "/v1/bootstrap", json={"project_slug": "team-owned"}, headers=member["headers"]
    )

    assert response.status_code == 200, response.text
    owner = response.json()["project_owner"]
    assert owner == {"type": "group", "id": "platform-team"}
    assert owner["id"] != member["user_id"]
