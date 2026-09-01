"""Explicit provisioning of `ach-memory-profile-v1`, one model per bank kind.

Task 3 gives this model existence and a correct query/budget/trigger; it does
not read or interpret anything the model produces (Task 4's job) and it wires
no read path to any of this -- see `provision_profile`'s docstring and
`test_provision_profile_is_wired_only_from_the_admin_route` below.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from memory import profiles

BASE = "http://hindsight.test"

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "memory"


class FakeClient:
    """Records calls; returns whatever the test seeded.

    `refresh_mental_model` and `dry_run_refresh_mental_model` raise instead
    of no-op-ing: provisioning must never reach either, and a silent no-op
    method would let that regress unnoticed.
    """

    def __init__(self, models=None):
        self.models = models if models is not None else []
        self.created: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []

    def list_mental_models(self, bank_id, **kwargs):
        return {"mental_models": self.models}

    def create_mental_model(self, bank_id, **kwargs):
        self.created.append((bank_id, kwargs))
        return {"id": "mm-new"}

    def update_mental_model(self, bank_id, mental_model_id, **kwargs):
        self.updated.append((bank_id, mental_model_id, kwargs))
        return {"id": mental_model_id}

    def refresh_mental_model(self, *args, **kwargs):
        raise AssertionError("provisioning must never trigger a refresh")

    def dry_run_refresh_mental_model(self, *args, **kwargs):
        raise AssertionError("provisioning must never trigger a dry-run refresh")


def _model(
    *,
    source_query: str | None = None,
    max_tokens: int | None = None,
    trigger: dict | None = None,
) -> dict:
    return {
        "id": "mm-1",
        "name": profiles.PROFILE_MODEL_NAME,
        "source_query": (
            profiles.PROFILE_USER_QUERY if source_query is None else source_query
        ),
        "max_tokens": (
            profiles.USER_PROFILE_MAX_TOKENS if max_tokens is None else max_tokens
        ),
        "trigger": profiles._profile_trigger("user") if trigger is None else trigger,
    }


# ---------------------------------------------------------------------------
# Create / idempotent reconcile
# ---------------------------------------------------------------------------


def test_provisioning_creates_the_model_when_the_bank_has_none():
    client = FakeClient(models=[])

    assert profiles.provision_profile(client, "user_1", "user") == "created"

    (bank_id, kwargs) = client.created[0]
    assert bank_id == "user_1"
    assert kwargs["name"] == profiles.PROFILE_MODEL_NAME
    assert kwargs["source_query"] == profiles.PROFILE_USER_QUERY
    assert kwargs["max_tokens"] == profiles.USER_PROFILE_MAX_TOKENS
    assert kwargs["tags"] == []
    assert kwargs["trigger"] == {
        "mode": "full",
        "refresh_after_consolidation": False,
        "response_schema": profiles.user_response_schema(),
        "keep_trace": True,
    }


def test_provisioning_a_project_bank_uses_the_project_query_budget_and_schema():
    client = FakeClient(models=[])

    assert profiles.provision_profile(client, "project_1", "project") == "created"

    (_, kwargs) = client.created[0]
    assert kwargs["source_query"] == profiles.PROFILE_PROJECT_QUERY
    assert kwargs["max_tokens"] == profiles.PROJECT_PROFILE_MAX_TOKENS
    assert kwargs["trigger"]["response_schema"] == profiles.project_response_schema()
    assert kwargs["tags"] == []


def test_a_model_already_in_line_is_not_patched():
    """Idempotent: re-running after a deploy that changed nothing writes
    nothing, for the same reason `brief.provision_section` is idempotent."""
    client = FakeClient(models=[_model()])

    assert profiles.provision_profile(client, "user_1", "user") == "reconciled"

    assert client.updated == []


# ---------------------------------------------------------------------------
# Reconcile: source query drift
# ---------------------------------------------------------------------------


def test_a_changed_source_query_updates_the_model_in_place():
    client = FakeClient(models=[_model(source_query="an older query")])

    profiles.provision_profile(client, "user_1", "user")

    (bank_id, model_id, kwargs) = client.updated[0]
    assert (bank_id, model_id) == ("user_1", "mm-1")
    assert kwargs["source_query"] == profiles.PROFILE_USER_QUERY
    assert "trigger" not in kwargs
    assert "max_tokens" not in kwargs


# ---------------------------------------------------------------------------
# Reconcile: max_tokens drift
# ---------------------------------------------------------------------------


def test_a_changed_max_tokens_updates_the_model_in_place():
    """A redeploy that changes USER_PROFILE_MAX_TOKENS/PROJECT_PROFILE_MAX_
    TOKENS must reach a model that already exists, same as `brief._reconcile`
    learned to do for TRIGGER after fixing this exact class of bug once
    (`brief.py`'s own docstring records it) -- and this budget is explicitly
    the least settled of the constants provisioned here."""
    client = FakeClient(models=[_model(max_tokens=999)])

    assert profiles.provision_profile(client, "user_1", "user") == "reconciled"

    (_, _, kwargs) = client.updated[0]
    assert kwargs["max_tokens"] == profiles.USER_PROFILE_MAX_TOKENS
    # `test_a_model_already_in_line_is_not_patched` above already covers the
    # already-correct case (its `_model()` default now includes a matching
    # `max_tokens`), so no separate no-op test is needed here.


# ---------------------------------------------------------------------------
# Reconcile: wrong-scope schema correction
# ---------------------------------------------------------------------------


def test_a_model_holding_the_wrong_scope_schema_is_corrected():
    """Adversarial case called out by the plan: a user-bank model somehow
    ended up with the project schema in its trigger. Reconcile must catch
    this even though `mode`/`refresh_after_consolidation`/`keep_trace`
    already match."""
    wrong_trigger = {
        "mode": "full",
        "refresh_after_consolidation": False,
        "response_schema": profiles.project_response_schema(),
        "keep_trace": True,
    }
    client = FakeClient(models=[_model(trigger=wrong_trigger)])

    assert profiles.provision_profile(client, "user_1", "user") == "reconciled"

    (_, _, kwargs) = client.updated[0]
    assert kwargs["trigger"]["response_schema"] == profiles.user_response_schema()


def test_a_schema_already_matching_the_scope_is_not_patched():
    matching_trigger = {
        "mode": "full",
        "refresh_after_consolidation": False,
        "response_schema": profiles.project_response_schema(),
        "keep_trace": True,
    }
    client = FakeClient(
        models=[
            _model(
                source_query=profiles.PROFILE_PROJECT_QUERY,
                max_tokens=profiles.PROJECT_PROFILE_MAX_TOKENS,
                trigger=matching_trigger,
            )
        ]
    )

    assert profiles.provision_profile(client, "project_1", "project") == "reconciled"

    assert client.updated == []


# ---------------------------------------------------------------------------
# Reconcile: trigger drift and field preservation
# ---------------------------------------------------------------------------


def test_a_changed_trigger_field_reaches_a_model_that_already_exists():
    stale_trigger = {
        "mode": "delta",
        "refresh_after_consolidation": False,
        "response_schema": profiles.user_response_schema(),
        "keep_trace": True,
    }
    client = FakeClient(models=[_model(trigger=stale_trigger)])

    profiles.provision_profile(client, "user_1", "user")

    (_, _, kwargs) = client.updated[0]
    assert kwargs["trigger"]["mode"] == "full"
    assert "source_query" not in kwargs
    assert "max_tokens" not in kwargs


def test_reconciling_a_trigger_keeps_fields_this_module_does_not_set():
    """Hindsight puts its own keys in the trigger. Replacing the object
    wholesale would drop them silently -- same reasoning as
    `brief._reconcile`'s own docstring."""
    trigger = {
        "mode": "delta",
        "refresh_after_consolidation": False,
        "response_schema": profiles.user_response_schema(),
        "keep_trace": True,
        "upstream_knob": 7,
    }
    client = FakeClient(models=[_model(trigger=trigger)])

    profiles.provision_profile(client, "user_1", "user")

    (_, _, kwargs) = client.updated[0]
    assert kwargs["trigger"]["upstream_knob"] == 7
    assert kwargs["trigger"]["mode"] == "full"


# ---------------------------------------------------------------------------
# No refresh
# ---------------------------------------------------------------------------


def test_provisioning_never_triggers_a_refresh():
    """Both the create and reconcile paths must avoid `refresh_mental_model`
    and `dry_run_refresh_mental_model` entirely -- `FakeClient` raises if
    either is called, so this test fails loudly rather than passing on an
    unexercised path."""
    created_client = FakeClient(models=[])
    profiles.provision_profile(created_client, "user_1", "user")

    reconciled_client = FakeClient(models=[_model(source_query="stale")])
    profiles.provision_profile(reconciled_client, "user_1", "user")


# ---------------------------------------------------------------------------
# Phase 0 target: pure, unapplied
# ---------------------------------------------------------------------------


def test_desired_phase0_trigger_user_scope():
    assert profiles.desired_phase0_trigger("user") == {
        "mode": "delta",
        "fact_types": ["observation"],
        "tags": ["profile_eligible"],
        "tags_match": "exact",
        "refresh_after_consolidation": False,
        "response_schema": profiles.user_response_schema(),
        "keep_trace": True,
    }


def test_desired_phase0_trigger_project_scope():
    assert profiles.desired_phase0_trigger("project") == {
        "mode": "delta",
        "fact_types": ["observation"],
        "tags": ["profile_eligible"],
        "tags_match": "exact",
        "refresh_after_consolidation": False,
        "response_schema": profiles.project_response_schema(),
        "keep_trace": True,
    }


def test_desired_phase0_trigger_is_never_called_outside_its_own_tests():
    """Pure and unwired: nothing in application code may call this during
    Phase 4 (plan "Hindsight target and safe rollout" / "Rollout order")."""
    for path in REPO_SRC.rglob("*.py"):
        if path.name == "profiles.py":
            continue
        assert "desired_phase0_trigger(" not in path.read_text()


# ---------------------------------------------------------------------------
# provision_profile is wired only from the new admin route (no GET creates)
# ---------------------------------------------------------------------------


def test_provision_profile_is_wired_only_from_the_admin_route():
    """Structural proof that nothing on a read path can create or reconcile
    a profile model: the only call site anywhere in `src/memory` outside
    `profiles.py` itself is the new `POST /v1/admin/profile/{scope}/
    provision` route in `api/admin.py`."""
    admin_source = (REPO_SRC / "api" / "admin.py").read_text()
    call_sites = [
        line
        for line in admin_source.splitlines()
        if "profiles.provision_profile(" in line
    ]
    assert len(call_sites) == 1

    for path in REPO_SRC.rglob("*.py"):
        if path.name in {"admin.py", "profiles.py"}:
            continue
        assert "provision_profile(" not in path.read_text()


# ---------------------------------------------------------------------------
# POST /v1/admin/profile/{scope}/provision
# ---------------------------------------------------------------------------


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


def test_provision_profile_refuses_a_user_key_even_the_banks_own_owner(
    client, juan, tenant
):
    response = client.post(
        "/v1/admin/profile/user/provision",
        params={"user_id": juan["user_id"]},
        headers=juan["headers"],
    )

    assert response.status_code == 403


@respx.mock
def test_provision_profile_creates_the_model_and_reports_scope(
    client, juan, master_headers, tenant
):
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(200, json={"mental_models": []})
    )
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models$").mock(
        return_value=httpx.Response(201, json={"id": "mm-new"})
    )

    response = client.post(
        "/v1/admin/profile/user/provision",
        params={"user_id": juan["user_id"]},
        headers=master_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"outcome": "created", "scope": "user"}
    assert route.called

    sent = json.loads(route.calls.last.request.content)
    assert sent["name"] == profiles.PROFILE_MODEL_NAME
    assert sent["source_query"] == profiles.PROFILE_USER_QUERY
    assert sent["max_tokens"] == profiles.USER_PROFILE_MAX_TOKENS
    assert sent["tags"] == []
    assert sent["trigger"]["mode"] == "full"
    assert sent["trigger"]["response_schema"] == profiles.user_response_schema()


@respx.mock
def test_provision_profile_reconciles_an_existing_model_for_a_project(
    client, juan, master_headers, tenant
):
    client.post("/v1/projects", json={"project_slug": "alpha"}, headers=juan["headers"])

    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models").mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {
                        "id": "mm-1",
                        "name": profiles.PROFILE_MODEL_NAME,
                        "source_query": "stale query",
                        "trigger": {
                            "mode": "delta",
                            "refresh_after_consolidation": False,
                            "response_schema": profiles.project_response_schema(),
                            "keep_trace": True,
                        },
                    }
                ]
            },
        )
    )
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/mental-models/mm-1$"
    ).mock(return_value=httpx.Response(200, json={"id": "mm-1"}))

    response = client.post(
        "/v1/admin/profile/project/provision",
        params={"project_slug": "alpha"},
        headers=master_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"outcome": "reconciled", "scope": "project"}
    assert response.json()["project_slug"] == "alpha"

    sent = json.loads(route.calls.last.request.content)
    assert sent["source_query"] == profiles.PROFILE_PROJECT_QUERY
    assert sent["max_tokens"] == profiles.PROJECT_PROFILE_MAX_TOKENS
    assert sent["trigger"]["mode"] == "full"


def test_provision_on_an_unknown_project_slug_404s_without_creating_it(
    client, master_headers, tenant
):
    """`create=False` bank resolution: an admin naming a project that was
    never allocated must not conjure a bank -- and therefore never reach
    Hindsight at all."""
    response = client.post(
        "/v1/admin/profile/project/provision",
        params={"project_slug": "never-created"},
        headers=master_headers,
    )

    assert response.status_code == 404
    listed = client.get("/v1/projects", headers=master_headers).json()
    assert listed == []
