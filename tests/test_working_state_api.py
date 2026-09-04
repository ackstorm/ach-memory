import pytest

from memory import ids
from memory.auth import keys

WS = "ws_" + "a" * 32


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


def _project(client, headers, slug: str = "acme-api") -> None:
    client.post("/v1/projects", json={"project_slug": slug}, headers=headers)


def _start(client, headers, session_id: str, *, project_slug: str = "acme-api", workspace_id: str = WS):
    return client.post(
        "/v1/working-state/sessions",
        json={"project_slug": project_slug, "workspace_id": workspace_id, "session_id": session_id},
        headers=headers,
    )


def _write(**overrides) -> dict:
    body = {
        "project_slug": "acme-api",
        "workspace_id": WS,
        "session_id": "sess-1",
        "session_epoch": 1,
        "checkpoint_seq": 1,
        "objective": "ship the feature",
    }
    body.update(overrides)
    return body


def test_a_user_starts_a_session_and_receives_a_positive_server_epoch(client, juan, tenant):
    _project(client, juan["headers"])

    response = _start(client, juan["headers"], "sess-1")

    assert response.status_code == 200
    body = response.json()
    assert body["session_epoch"] > 0
    assert body["session_id"] == "sess-1"
    assert body["workspace_id"] == WS
    assert body["project_slug"] == "acme-api"


def test_retrying_the_start_request_returns_the_same_epoch(client, juan, tenant):
    _project(client, juan["headers"])

    first = _start(client, juan["headers"], "sess-1").json()
    second = _start(client, juan["headers"], "sess-1").json()

    assert first["session_epoch"] == second["session_epoch"]


def test_an_explicit_write_returns_its_stored_ordering_and_source_fields(client, juan, tenant):
    _project(client, juan["headers"])
    epoch = _start(client, juan["headers"], "sess-1").json()["session_epoch"]

    response = client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch, checkpoint_seq=1, objective="ship the feature"),
        headers=juan["headers"],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "sess-1"
    assert body["session_epoch"] == epoch
    assert body["checkpoint_seq"] == 1
    assert body["objective"] == "ship the feature"
    assert body["changed"] is True
    assert "updated_at" in body


def test_a_lower_pair_is_a_409_with_a_stable_domain_code(client, juan, tenant):
    _project(client, juan["headers"])
    epoch = _start(client, juan["headers"], "sess-1").json()["session_epoch"]
    client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch, checkpoint_seq=2),
        headers=juan["headers"],
    )

    response = client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch, checkpoint_seq=1),
        headers=juan["headers"],
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORKING_STATE_STALE"


def test_a_conflicting_pair_is_a_409_with_a_stable_domain_code(client, juan, tenant):
    _project(client, juan["headers"])
    epoch = _start(client, juan["headers"], "sess-1").json()["session_epoch"]
    client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch, checkpoint_seq=1, objective="first version"),
        headers=juan["headers"],
    )

    response = client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch, checkpoint_seq=1, objective="a different version"),
        headers=juan["headers"],
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORKING_STATE_CONFLICT"


def test_a_malformed_workspace_id_is_a_422(client, juan, tenant):
    _project(client, juan["headers"])

    response = _start(client, juan["headers"], "sess-1", workspace_id="not-a-workspace-id")

    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"objective": "   "},
        {"objective": "\x00\x01"},
        {"recent_decisions": ["ok", "\x07"]},
        {"recent_decisions": [f"item {i}" for i in range(11)]},
        {"objective": "x" * 513},
        {"checkpoint_seq": -1},
    ],
)
def test_invalid_write_fields_are_a_422(client, juan, tenant, overrides):
    _project(client, juan["headers"])
    epoch = _start(client, juan["headers"], "sess-1").json()["session_epoch"]

    response = client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch, **overrides),
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_a_master_key_cannot_start_a_session(client, master_headers, tenant):
    response = client.post(
        "/v1/working-state/sessions",
        json={"project_slug": "acme-api", "workspace_id": WS, "session_id": "sess-1"},
        headers=master_headers,
    )

    assert response.status_code == 403


def test_a_master_key_cannot_write_state(client, master_headers, tenant):
    response = client.put("/v1/working-state", json=_write(), headers=master_headers)

    assert response.status_code == 403


def test_another_user_cannot_write_to_this_project(client, juan, master_headers, tenant):
    _project(client, juan["headers"])
    epoch = _start(client, juan["headers"], "sess-1").json()["session_epoch"]
    other_user = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    other_key = client.post(
        f"/v1/users/{other_user}/keys", json={}, headers=master_headers
    ).json()["key"]
    other_headers = {"Authorization": f"Bearer {other_key}"}

    response = client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch),
        headers=other_headers,
    )

    assert response.status_code == 404


def test_a_user_without_group_membership_cannot_write_a_group_project(
    client, juan, master_headers, tenant
):
    client.post("/v1/groups", json={"id": "grp_payments"}, headers=master_headers)
    client.post(
        "/v1/projects",
        json={
            "project_slug": "payments-api",
            "owner": {"type": "group", "id": "grp_payments"},
        },
        headers=master_headers,
    )

    response = client.post(
        "/v1/working-state/sessions",
        json={"project_slug": "payments-api", "workspace_id": WS, "session_id": "sess-1"},
        headers=juan["headers"],
    )

    assert response.status_code == 404


def test_another_tenant_cannot_write(client, juan, session, tenant):
    """Same project_slug, a genuinely different tenant's own key."""
    from memory.models import ApiKey, Tenant, User

    _project(client, juan["headers"])
    epoch = _start(client, juan["headers"], "sess-1").json()["session_epoch"]

    other_tenant = "other-tenant"
    session.add(Tenant(id=other_tenant))
    other_user = User(
        id=ids.new_user_id(), tenant_id=other_tenant, bank_id=ids.new_user_bank_id()
    )
    session.add(other_user)
    session.flush()
    plaintext = keys.generate_key()
    session.add(
        ApiKey(
            id=ids.new_key_id(),
            tenant_id=other_tenant,
            user_id=other_user.id,
            secret_hash=keys.hash_key(plaintext),
        )
    )
    session.commit()
    other_headers = {"Authorization": f"Bearer {plaintext}"}

    response = client.put(
        "/v1/working-state",
        json=_write(session_epoch=epoch),
        headers=other_headers,
    )

    assert response.status_code in (403, 404)


def test_an_unknown_project_is_not_found_and_creates_no_project(client, juan, session, tenant):
    from memory.models import Project

    before = session.query(Project).count()

    response = _start(client, juan["headers"], "sess-1", project_slug="no-such-project")

    assert response.status_code == 404
    assert session.query(Project).count() == before


def test_extra_forbid_rejects_a_misspelled_field(client, juan, tenant):
    _project(client, juan["headers"])

    response = client.post(
        "/v1/working-state/sessions",
        json={
            "project_slug": "acme-api",
            "workspace_id": WS,
            "seession_id": "sess-1",
        },
        headers=juan["headers"],
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Phase 2 review finding #3: workspace_id used `.match()` against a
# `$`-anchored pattern, which also matches just before a trailing newline;
# session_id had no bound at all. Both must fail as a 422 before reaching
# PostgreSQL, on the session-allocation route and the checkpoint route.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["session", "checkpoint"])
def test_a_workspace_id_with_a_trailing_newline_is_a_422(client, juan, tenant, route):
    _project(client, juan["headers"])
    smuggled = WS + "\n"

    if route == "session":
        response = _start(client, juan["headers"], "sess-1", workspace_id=smuggled)
    else:
        response = client.put(
            "/v1/working-state", json=_write(workspace_id=smuggled), headers=juan["headers"]
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad_session_id", ["", "   ", "sess\x00-1", "sess\n1", "s" * 129], ids=repr
)
@pytest.mark.parametrize("route", ["session", "checkpoint"])
def test_a_blank_control_or_over_length_session_id_is_a_422(
    client, juan, tenant, route, bad_session_id
):
    _project(client, juan["headers"])

    if route == "session":
        response = _start(client, juan["headers"], bad_session_id)
    else:
        response = client.put(
            "/v1/working-state", json=_write(session_id=bad_session_id), headers=juan["headers"]
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Phase 2 review finding #6: both routes echoed the request's `project_slug`
# verbatim instead of the resolved project's current slug, so a caller
# following a rename tombstone got its own retired slug reflected back with
# no rename signal at all.
# ---------------------------------------------------------------------------


def test_a_retired_slug_returns_the_current_slug_and_rename_metadata(client, juan, tenant):
    _project(client, juan["headers"], slug="acme-api")
    rename = client.patch(
        "/v1/projects/acme-api", json={"project_slug": "acme-api-v2"}, headers=juan["headers"]
    )
    assert rename.status_code == 200, rename.text

    session_response = _start(client, juan["headers"], "sess-1", project_slug="acme-api")
    assert session_response.status_code == 200, session_response.text
    session_body = session_response.json()
    assert session_body["project_slug"] == "acme-api-v2"
    assert session_body["resolved_from"] == "acme-api"
    assert session_body["notice"] == "PROJECT_RENAMED"

    write_response = client.put(
        "/v1/working-state",
        json=_write(
            project_slug="acme-api",
            session_epoch=session_body["session_epoch"],
        ),
        headers=juan["headers"],
    )
    assert write_response.status_code == 200, write_response.text
    write_body = write_response.json()
    assert write_body["project_slug"] == "acme-api-v2"
    assert write_body["resolved_from"] == "acme-api"
    assert write_body["notice"] == "PROJECT_RENAMED"
