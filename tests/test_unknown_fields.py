"""A typoed field must be a 422, never a silent no-op.

This is Plan 6's finding I1 -- "a caller following SPEC §8.4 PATCHed
git_locator, got 200 OK, and nothing changed" -- on every model that did not
get the fix. Verified before this test was written: UpdateDirectiveRequest(
scope="user", priorty=9) validated cleanly with every real field None, so the
PATCH sent an empty {} upstream and answered 200 (2026-08-23 review, R2-I4).
"""

import pytest

DIR_ID = "11111111-1111-1111-1111-111111111111"
MM_ID = "mm-1234567890abcdef1234567890abcdef"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/v1/memory/retain", {"scope": "user", "content": "x", "documnt_id": "d"}),
        ("POST", "/v1/memory/recall", {"scope": "user", "query": "x", "quer": "y"}),
        ("POST", "/v1/memory/list", {"scope": "user", "stat": "valid"}),
        ("POST", "/v1/directives", {"scope": "user", "name": "n", "content": "c", "priorty": 9}),
        ("PATCH", f"/v1/directives/{DIR_ID}", {"scope": "user", "priorty": 9}),
        ("POST", "/v1/mental-models", {"scope": "user", "name": "n", "source_query": "q", "max_token": 999}),
        ("PATCH", f"/v1/mental-models/{MM_ID}", {"scope": "user", "max_token": 999}),
    ],
)
def test_an_unknown_field_is_refused(client, master_headers, tenant, method, path, body):
    response = client.request(method, path, json=body, headers=master_headers)
    assert response.status_code == 422, response.text


def test_a_typoed_user_id_does_not_silently_provision_a_random_user(
    client, master_headers, tenant
):
    """SPEC §16.3: ACH supplies its own user ids. `{"user_id": ...}` instead of
    `{"id": ...}` used to 201 with a service-generated usr_<random>, and ACH's
    own id was never stored -- a provisioning failure that looks like success."""
    response = client.post(
        "/v1/users", json={"user_id": "ach-user-82f"}, headers=master_headers
    )
    assert response.status_code == 422, response.text


def test_a_typoed_group_name_is_refused(client, master_headers, tenant):
    response = client.post(
        "/v1/groups", json={"id": "grp_x", "nmae": "X"}, headers=master_headers
    )
    assert response.status_code == 422, response.text


def test_mental_model_trigger_still_passes_unknown_keys_through(client):
    """§14.5 makes MentalModelTrigger deliberate pass-through. Inheriting
    forbid on the OUTER model must not close it."""
    from memory.api.mental_models import CreateMentalModelRequest

    body = CreateMentalModelRequest(
        scope="user",
        name="n",
        source_query="q",
        trigger={"mode": "full", "refresh_cron": "0 3 * * *"},
    )
    assert body.trigger.model_dump()["refresh_cron"] == "0 3 * * *"
