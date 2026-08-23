"""A control character in a caller-supplied identifier must be a typed 4xx.

psycopg raises DataError ("PostgreSQL text fields cannot contain NUL (0x00)
bytes") at parameter adaptation. SQLAlchemy wraps it as sqlalchemy.exc.
DataError, which is NOT an IntegrityError -- so users.py's and groups.py's
`except IntegrityError` never see it and it reaches api/app.py's catch-all as
a 500. Eight routes were verified live in the 2026-08-23 review; this table
pins all of them plus the two admin audit filters.
"""

import pytest

NUL = "a\x00b"

# For a path *segment* the raw NUL has to be percent-encoded: httpx's own URL
# parser rejects a literal non-printable ASCII character in a URL string
# before the request is ever sent (httpx2._urlparse.urlparse), which would
# make the test fail the same way whether our guard exists or not. Starlette
# decodes the percent-encoding while routing, so the handler still receives
# the real "a\x00b" in the path parameter -- `params=`/`json=` (below) go
# through httpx's own encoders, which already percent-encode a raw NUL, so
# only path segments need this done by hand.
NUL_PATH = "a%00b"


@pytest.mark.parametrize(
    "method,path,params,body,expected_code",
    [
        ("GET", f"/v1/users/{NUL_PATH}", None, None, "USER_NOT_FOUND"),
        ("GET", f"/v1/users/{NUL_PATH}/keys", None, None, "USER_NOT_FOUND"),
        ("GET", f"/v1/groups/{NUL_PATH}", None, None, "GROUP_NOT_FOUND"),
        (
            "POST",
            f"/v1/admin/slugs/{NUL_PATH}/release",
            None,
            None,
            "RETIRED_SLUG_NOT_FOUND",
        ),
        # The two audit rows are FILTERS: they must answer 200 with an empty
        # list, not an error. Asserted separately below.
        ("GET", "/v1/admin/audit", {"actor_key_id": NUL}, None, None),
        ("GET", "/v1/admin/audit", {"on_behalf_of": NUL}, None, None),
    ],
)
def test_a_control_character_is_never_a_500(
    client, master_headers, tenant, method, path, params, body, expected_code
):
    response = client.request(
        method, path, params=params, json=body, headers=master_headers
    )
    assert response.status_code != 500, response.text
    if expected_code is not None:
        assert response.json()["error"]["code"] == expected_code, response.text


@pytest.mark.parametrize("param", ["actor_key_id", "on_behalf_of", "action"])
def test_an_unstorable_audit_filter_matches_nothing(
    client, master_headers, tenant, param
):
    """A filter is not a lookup. A value Postgres cannot store matches nothing,
    so 200 with an empty list is the correct answer -- raising some other
    route's not-found error here would be borrowing a contract that does not
    apply."""
    response = client.get(
        "/v1/admin/audit", params={param: NUL}, headers=master_headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_a_control_character_in_a_scoped_user_id_is_not_a_500(
    client, master_headers, tenant
):
    """The data plane's own identifier, reached through ScopedRequest."""
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "user_id": NUL, "query": "hi"},
        headers=master_headers,
    )
    assert response.status_code != 500, response.text


# --- Review findings C1, C2, I1, I2 (2026-08-23) -----------------------


@pytest.mark.parametrize(
    "owner_type,expected_code",
    [("user", "USER_NOT_FOUND"), ("group", "GROUP_NOT_FOUND")],
)
def test_c1_a_control_character_owner_id_is_not_a_500(
    client, master_headers, tenant, owner_type, expected_code
):
    """_validate_owner is the choke point for both create() and transfer();
    this pins create() (POST /v1/projects). An unstorable owner id names no
    object, so it is a LOOKUP -- the route's own not-found error, not a 500."""
    response = client.post(
        "/v1/projects",
        json={"project_slug": "p", "owner": {"type": owner_type, "id": NUL}},
        headers=master_headers,
    )
    assert response.status_code != 500, response.text
    assert response.json()["error"]["code"] == expected_code, response.text


def test_c1_a_control_character_owner_id_on_transfer_is_not_a_500(
    client, master_headers, tenant
):
    """The other caller of _validate_owner (PATCH .../owner)."""
    owner = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    client.post(
        "/v1/projects",
        json={"project_slug": "p", "owner": {"type": "user", "id": owner}},
        headers=master_headers,
    )

    response = client.patch(
        "/v1/projects/p/owner",
        json={"type": "user", "id": NUL},
        headers=master_headers,
    )
    assert response.status_code != 500, response.text
    assert response.json()["error"]["code"] == "USER_NOT_FOUND", response.text


def test_c2_a_control_character_in_on_behalf_of_is_a_422(
    client, master_headers, tenant
):
    """The header is provenance, neither a lookup nor a filter -- bounded at
    the boundary as a 422, like the header's own max_length already is.

    A raw NUL in a header value IS accepted by httpx and reaches the server
    (unlike a URL path, where the same byte makes httpx itself raise
    InvalidURL before the request is sent), so no percent-encoding is needed
    here.
    """
    headers = {**master_headers, "On-Behalf-Of": NUL}
    response = client.post("/v1/users", json={}, headers=headers)
    assert response.status_code == 422, response.text


def test_i1_a_control_character_user_id_is_a_422_not_a_409(
    client, master_headers, tenant
):
    """Same field already answers 422 for an oversize id (Field max_length);
    a control-character id is the other kind of unstorable value and must
    get the same treatment, not USER_ALREADY_EXISTS -- which would tell a
    client that retrying with a different id fixes the problem, when it
    doesn't."""
    response = client.post("/v1/users", json={"id": NUL}, headers=master_headers)
    assert response.status_code == 422, response.text
    # FastAPI's own validation-error shape ({"detail": [...]}), never the
    # domain-error envelope with a code that would contradict this status.
    assert "error" not in response.json(), response.text


def test_i2_a_control_character_group_id_is_a_422_not_a_409(
    client, master_headers, tenant
):
    response = client.post("/v1/groups", json={"id": NUL}, headers=master_headers)
    assert response.status_code == 422, response.text
    assert "error" not in response.json(), response.text


# --- 2026-08-23 whole-branch review, finding 1: control-character screening
# closed on 8 sites, left open on 4 (git_locator on ScopedRequest/
# CreateProjectRequest/UpdateProjectRequest, CreateGroupRequest.name, and
# every scoped_query_params route). All six reproduced live as 500
# INTERNAL_ERROR before the fix. ------------------------------------------


def _create_user_key(client, master_headers) -> str:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    return client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]


def test_r1_a_control_character_git_locator_on_retain_is_a_422(
    client, master_headers, tenant
):
    """ScopedRequest.git_locator reaches the projects INSERT via
    _resolve_bank -- same DataError -> 500 as ScopedRequest.user_id."""
    key = _create_user_key(client, master_headers)
    response = client.post(
        "/v1/memory/retain",
        json={
            "scope": "project",
            "project_slug": "p",
            "content": "x",
            "git_locator": f"github.com/a/b{NUL}c",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_r1_a_control_character_git_locator_on_create_project_is_a_422(
    client, master_headers, tenant
):
    key = _create_user_key(client, master_headers)
    response = client.post(
        "/v1/projects",
        json={"project_slug": "p", "git_locator": f"github.com/a/b{NUL}c"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_r1_a_control_character_git_locator_on_patch_project_is_a_422(
    client, master_headers, tenant
):
    key = _create_user_key(client, master_headers)
    headers = {"Authorization": f"Bearer {key}"}
    client.post("/v1/projects", json={"project_slug": "pp"}, headers=headers)

    response = client.patch(
        "/v1/projects/pp",
        json={"git_locator": f"github.com/a/b{NUL}c"},
        headers=headers,
    )
    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_r1_a_control_character_group_name_is_a_422_not_a_500(
    client, master_headers, tenant
):
    response = client.post(
        "/v1/groups", json={"name": f"team{NUL}x"}, headers=master_headers
    )
    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_r1_a_control_character_query_param_user_id_is_a_422_not_a_500(
    client, master_headers, tenant
):
    """scoped_query_params builds a ScopedRequest inside its own function
    body, not through FastAPI's request-model validation -- so a raised
    pydantic ValidationError previously escaped as a 500 from api/app.py's
    catch-all instead of a 422. Affects all eight scoped_query_params
    routes; pinned here on GET /v1/directives."""
    response = client.get(
        "/v1/directives",
        params={"scope": "user", "user_id": f"usr_{NUL}bad"},
        headers=master_headers,
    )
    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_r1_a_control_character_admin_clear_user_id_is_a_422_not_a_500(
    client, master_headers, tenant
):
    """_admin_scope has the identical bug: admin.py's clear/delete routes
    take user_id as a bare query param, so the same ScopedRequest
    ValidationError escapes as a 500."""
    response = client.post(
        "/v1/admin/memory/user/clear",
        params={"user_id": f"usr_{NUL}bad"},
        headers=master_headers,
    )
    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text
