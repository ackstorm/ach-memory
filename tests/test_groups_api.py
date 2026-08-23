import pytest


@pytest.fixture
def user_id(client, master_headers, tenant) -> str:
    return client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]


def test_master_creates_a_group_with_a_generated_id(client, master_headers, tenant):
    response = client.post("/v1/groups", json={}, headers=master_headers)

    assert response.status_code == 201
    assert response.json()["group_id"].startswith("grp_")


def test_master_creates_a_group_with_an_explicit_id(client, master_headers, tenant):
    response = client.post(
        "/v1/groups", json={"id": "grp_payments", "name": "Payments"},
        headers=master_headers,
    )

    assert response.status_code == 201
    assert response.json()["group_id"] == "grp_payments"


def test_membership_is_added_and_removed(client, master_headers, tenant, user_id):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]

    added = client.put(
        f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers
    )
    listed = client.get(f"/v1/groups/{group_id}", headers=master_headers).json()

    assert added.status_code == 204
    assert listed["members"] == [user_id]

    removed = client.delete(
        f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers
    )
    after = client.get(f"/v1/groups/{group_id}", headers=master_headers).json()

    assert removed.status_code == 204
    assert after["members"] == []


def test_adding_the_same_member_twice_is_idempotent(
    client, master_headers, tenant, user_id
):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]

    first = client.put(f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers)
    second = client.put(f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers)
    listed = client.get(f"/v1/groups/{group_id}", headers=master_headers).json()

    assert (first.status_code, second.status_code) == (204, 204)
    assert listed["members"] == [user_id]


def test_group_operations_require_the_master_key(
    client, master_headers, tenant, user_id
):
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.post(
        "/v1/groups", json={}, headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 403


def test_listing_groups_requires_the_master_key(client, master_headers, tenant, user_id):
    """Group membership IS project authorization: GET /v1/groups dumps every
    group's full membership, so a user key seeing this list is a data leak,
    not a robustness gap."""
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.get("/v1/groups", headers={"Authorization": f"Bearer {key}"})

    assert response.status_code == 403


def test_getting_a_group_requires_the_master_key(client, master_headers, tenant, user_id):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.get(
        f"/v1/groups/{group_id}", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 403


def test_adding_a_member_requires_the_master_key(client, master_headers, tenant, user_id):
    """If this gate ever moved to current_principal, any user key could add
    itself to a group and inherit that group's project memory."""
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.put(
        f"/v1/groups/{group_id}/members/{user_id}",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 403


def test_removing_a_member_requires_the_master_key(
    client, master_headers, tenant, user_id
):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]
    client.put(f"/v1/groups/{group_id}/members/{user_id}", headers=master_headers)
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]

    response = client.delete(
        f"/v1/groups/{group_id}/members/{user_id}",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 403


def test_unknown_group_is_404(client, master_headers, tenant):
    response = client.get("/v1/groups/grp_nope", headers=master_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GROUP_NOT_FOUND"


def test_adding_an_unknown_user_is_404(client, master_headers, tenant):
    group_id = client.post("/v1/groups", json={}, headers=master_headers).json()[
        "group_id"
    ]

    response = client.put(
        f"/v1/groups/{group_id}/members/usr_nope", headers=master_headers
    )

    assert response.status_code == 404
    # The group exists; only the user is missing. The code must say so.
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_an_oversize_explicit_group_id_is_a_422_not_a_500(client, master_headers, tenant):
    """Group.id is String(128); an overflow is a DataError, not an
    IntegrityError, so create_group's db.begin_nested()/except IntegrityError
    guard never catches it -- it must be rejected at the boundary instead."""
    response = client.post(
        "/v1/groups", json={"id": "g" * 200}, headers=master_headers
    )

    assert response.status_code == 422


def test_an_oversize_group_name_is_a_422_not_a_500(client, master_headers, tenant):
    """Group.name is String(256); same DataError-not-IntegrityError gap."""
    response = client.post(
        "/v1/groups", json={"name": "n" * 300}, headers=master_headers
    )

    assert response.status_code == 422


def test_reusing_an_explicit_group_id_conflicts(client, master_headers, tenant):
    client.post("/v1/groups", json={"id": "grp_dup"}, headers=master_headers)

    response = client.post("/v1/groups", json={"id": "grp_dup"}, headers=master_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GROUP_ALREADY_EXISTS"
