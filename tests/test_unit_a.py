"""Four small conformance fixes grouped as one unit.

17a  SPEC §8.6: a request that reached a project through a rename tombstone is
     annotated with resolved_from/notice. get_project did; the two PATCH routes
     dropped it, so a client keying off `notice` to update a stale
     MEMORY_PROJECT never learned it had followed one.
17b  release_slug was the only slug lookup in the service that did not
     normalize its path parameter -- on the one route whose whole purpose is an
     operator typing a name by hand.
24a  A master key naming an unknown user got 403, not USER_NOT_FOUND. §20.3
     gives it tenant-wide bypass and §18 names USER_NOT_FOUND for this case.
24b  RFC 7235 makes the auth scheme case-insensitive.
"""


def _key(client, master_headers) -> tuple[str, dict]:
    uid = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    secret = client.post(
        f"/v1/users/{uid}/keys", json={}, headers=master_headers
    ).json()["key"]
    return uid, {"Authorization": f"Bearer {secret}"}


def test_both_patch_routes_annotate_a_request_that_followed_a_tombstone(
    client, master_headers, tenant
):
    uid, headers = _key(client, master_headers)
    client.post("/v1/projects", json={"project_slug": "old-name"}, headers=headers)
    client.patch(
        "/v1/projects/old-name", json={"project_slug": "new-name"}, headers=headers
    )

    patched = client.patch(
        "/v1/projects/old-name",
        json={"git_locator": "github.com/acme/repo"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["resolved_from"] == "old-name", patched.text
    assert patched.json()["notice"] == "PROJECT_RENAMED", patched.text

    transferred = client.patch(
        "/v1/projects/old-name/owner",
        json={"type": "user", "id": uid},
        headers=headers,
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["resolved_from"] == "old-name", transferred.text
    assert transferred.json()["notice"] == "PROJECT_RENAMED", transferred.text


def test_release_slug_normalizes_the_operator_typed_name(
    client, master_headers, tenant
):
    _uid, headers = _key(client, master_headers)
    client.post("/v1/projects", json={"project_slug": "payments-api"}, headers=headers)
    client.patch(
        "/v1/projects/payments-api", json={"project_slug": "payments"}, headers=headers
    )
    # The tombstone is stored normalized as "payments-api"; the operator types
    # it with capitals.
    released = client.post(
        "/v1/admin/slugs/Payments-API/release", headers=master_headers
    )
    assert released.status_code == 204, released.text


def test_a_master_key_naming_an_unknown_user_gets_user_not_found(
    client, master_headers, tenant
):
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": "usr_definitely_not_here", "query": "x"},
        headers=master_headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "USER_NOT_FOUND", response.text


def test_a_user_key_addressing_someone_else_still_gets_forbidden(
    client, master_headers, tenant
):
    """The 403 shape must survive for a USER key -- it withholds existence."""
    _uid, headers = _key(client, master_headers)
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": "usr_someone_else", "query": "x"},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN", response.text


def test_the_bearer_scheme_is_case_insensitive(client, master_headers, tenant):
    uid, _ = _key(client, master_headers)
    secret = client.post(
        f"/v1/users/{uid}/keys", json={}, headers=master_headers
    ).json()["key"]
    for scheme in ("Bearer", "bearer", "BEARER", "BeArEr"):
        r = client.get("/v1/projects", headers={"Authorization": f"{scheme} {secret}"})
        assert r.status_code == 200, f"{scheme}: {r.text}"
