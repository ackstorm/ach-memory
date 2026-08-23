import httpx
import pytest
import respx

from memory.models import Project, User

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


def _mock_bank() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_list_memories_reaches_the_list_subpath(client, juan, tenant):
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list").mock(
        # "items", not "memories": that is what hindsight-api 0.9.1 actually
        # sends (PROJECT-STATE.md:262). A mock whose shape has drifted from
        # the real upstream is how the chunk_id bank-id leak went unseen.
        return_value=httpx.Response(200, json={"items": [{"id": "mem_1"}]})
    )

    response = client.post(
        "/v1/memory/list",
        json={"scope": "user", "q": "alembic", "limit": 5},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"]["items"] == [{"id": "mem_1"}]
    assert dict(route.calls.last.request.url.params) == {"q": "alembic", "limit": "5"}


@respx.mock
def test_forget_invalidates_rather_than_deleting(client, juan, tenant):
    # memory_id must be a syntactically valid UUID: the client now rejects a
    # non-UUID memory_id locally (a malformed id is a 400 upstream, not a
    # 404 — see "Pinned Hindsight facts"), so the mock would never be reached.
    mem_id = "22222222-2222-2222-2222-222222222222"
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    response = client.post(
        "/v1/memory/forget",
        json={"scope": "user", "memory_id": mem_id, "reason": "wrong"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.calls.last.request.method == "PATCH"
    assert b'"state":"invalidated"' in route.calls.last.request.read()


@respx.mock
def test_restore_reverts_an_invalidated_memory(client, juan, tenant):
    mem_id = "22222222-2222-2222-2222-222222222222"
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    client.post(
        "/v1/memory/restore",
        json={"scope": "user", "memory_id": mem_id},
        headers=juan["headers"],
    )

    assert b'"state":"valid"' in route.calls.last.request.read()


@respx.mock
def test_correct_edits_the_text(client, juan, tenant):
    mem_id = "22222222-2222-2222-2222-222222222222"
    route = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    client.post(
        "/v1/memory/correct",
        json={"scope": "user", "memory_id": mem_id, "content": "uv, not pip"},
        headers=juan["headers"],
    )

    assert b'"text":"uv, not pip"' in route.calls.last.request.read()


@respx.mock
def test_a_missing_memory_is_a_404_not_a_backend_error(client, juan, tenant):
    # A syntactically valid but absent UUID: "ghost" would now be rejected by
    # the client's local UUID guard before the request is ever sent, so the
    # mocked route would never be hit.
    absent_id = "00000000-0000-0000-0000-000000000000"
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{absent_id}").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    response = client.post(
        "/v1/memory/get",
        json={"scope": "user", "memory_id": absent_id},
        headers=juan["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MEMORY_NOT_FOUND"


@respx.mock
def test_idor_a_memory_id_cannot_be_used_to_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    """SPEC §20.1: authorization is by bank, never by object id. Alice naming a
    memory_id from juan's project must be refused BEFORE the id is used, so the
    request never reaches Hindsight at all.

    memory_id must be a syntactically valid UUID: `curate()`'s local
    `_require_uuid` guard runs before any upstream call and would itself zero
    out call_count for a malformed id, making the assertion pass whether or
    not the bank check ran at all.
    """
    mem_id = "22222222-2222-2222-2222-222222222222"
    _mock_bank()
    setup_retain = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    setup = client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    # The project row this test relies on survives even if this call fails
    # (retain commits before the upstream call) -- so a silently-broken mock
    # here would otherwise go unnoticed. Assert it actually succeeded.
    assert setup.status_code == 200
    assert setup_retain.called
    curate = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    response = client.post(
        "/v1/memory/forget",
        json={"scope": "project", "project_slug": "payments-api", "memory_id": mem_id},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
    assert curate.call_count == 0


@respx.mock
def test_idor_a_user_key_cannot_curate_another_users_memory(
    client, juan, alice, tenant
):
    mem_id = "22222222-2222-2222-2222-222222222222"
    curate = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    response = client.post(
        "/v1/memory/forget",
        json={"scope": "user", "user_id": juan["user_id"], "memory_id": mem_id},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert curate.call_count == 0


@respx.mock
def test_idor_list_memories_cannot_reach_an_unauthorized_bank(
    client, juan, alice, tenant
):
    """SPEC §20.1, mirrored from the documents/operations routers: `list`,
    `get`, `restore` and `correct` each resolve the bank on their own line,
    same as `forget` -- a future edit to one is invisible to the others, so
    each needs its own explicit IDOR test. Verified by mutation: stubbing
    `list_memories`'s `_bank()` call away (skipping authorization) leaves
    this failing on the assertion below, not passing."""
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    listed = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list"
    ).mock(
        # "items", not "memories": that is what hindsight-api 0.9.1 actually
        # sends (PROJECT-STATE.md:262). A mock whose shape has drifted from
        # the real upstream is how the chunk_id bank-id leak went unseen.
        return_value=httpx.Response(200, json={"items": []})
    )

    response = client.post(
        "/v1/memory/list",
        json={"scope": "project", "project_slug": "payments-api"},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
    assert listed.call_count == 0


@respx.mock
def test_idor_get_memory_cannot_reach_an_unauthorized_bank(client, juan, alice, tenant):
    """Same invariant as the other curation routes, for `get_memory`. Verified
    by mutation: stubbing `get_memory`'s `_bank()` call away leaves this
    failing on the assertion below, not passing."""
    mem_id = "22222222-2222-2222-2222-222222222222"
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    get = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    response = client.post(
        "/v1/memory/get",
        json={"scope": "project", "project_slug": "payments-api", "memory_id": mem_id},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
    assert get.call_count == 0


@respx.mock
def test_idor_restore_cannot_reach_an_unauthorized_bank(client, juan, alice, tenant):
    """Same invariant as the other curation routes, for `restore`. Verified by
    mutation: stubbing `restore`'s `_bank()` call away leaves this failing on
    the assertion below, not passing."""
    mem_id = "22222222-2222-2222-2222-222222222222"
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    restore = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    response = client.post(
        "/v1/memory/restore",
        json={"scope": "project", "project_slug": "payments-api", "memory_id": mem_id},
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
    assert restore.call_count == 0


@respx.mock
def test_idor_correct_cannot_reach_an_unauthorized_bank(client, juan, alice, tenant):
    """Same invariant as the other curation routes, for `correct` -- the one
    write among the four this closes. Verified by mutation: stubbing
    `correct`'s `_bank()` call away leaves this failing on the assertion
    below, not passing."""
    mem_id = "22222222-2222-2222-2222-222222222222"
    _mock_bank()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    client.post(
        "/v1/memory/retain",
        json={"scope": "project", "project_slug": "payments-api", "content": "x"},
        headers=juan["headers"],
    )
    correct = respx.patch(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id}))

    response = client.post(
        "/v1/memory/correct",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "memory_id": mem_id,
            "content": "uv, not pip",
        },
        headers=alice["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"
    assert correct.call_count == 0


@respx.mock
def test_idor_scenario_z_a_known_secondary_id_from_an_unreachable_bank_is_just_not_found(
    client, juan, alice, tenant, session
):
    """SPEC §24 scenario Z: knowing a memory_id from a project you cannot
    access does not grant access when you supply it under a scope you CAN
    access -- it is simply absent there, an ordinary 404, and the victim's
    bank is never touched. Distinct from the PROJECT_ACCESS_DENIED tests
    above, which name a scope Alice cannot reach at all; here she names her
    own scope=user bank, which she is fully authorized for."""
    from memory.models import User

    mem_id = "22222222-2222-2222-2222-222222222222"
    juan_bank_id = session.get(User, juan["user_id"]).bank_id
    alice_bank_id = session.get(User, alice["user_id"]).bank_id

    juan_route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/{juan_bank_id}/memories/{mem_id}$"
    ).mock(return_value=httpx.Response(200, json={"id": mem_id, "secret": "juan's"}))
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/{alice_bank_id}/memories/{mem_id}$"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    response = client.post(
        "/v1/memory/get",
        json={"scope": "user", "memory_id": mem_id},
        headers=alice["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MEMORY_NOT_FOUND"
    assert juan_route.call_count == 0


def test_curation_route_on_an_unknown_slug_creates_no_project(
    client, juan, tenant, session
):
    """A curation call is maintenance on something that already exists (SPEC
    §11.3), never first-touch creation (SPEC §16.2 -- retain/recall/reflect
    only). An unknown slug must 404 and leave nothing behind, not squat the
    slug for whoever asked first."""
    response = client.post(
        "/v1/memory/list",
        json={"scope": "project", "project_slug": "typo-slug"},
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


def test_list_memories_rejects_a_negative_limit(client, juan, tenant):
    response = client.post(
        "/v1/memory/list",
        json={"scope": "user", "limit": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_list_memories_rejects_a_negative_offset(client, juan, tenant):
    response = client.post(
        "/v1/memory/list",
        json={"scope": "user", "offset": -1},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_list_memories_rejects_a_bogus_type_not_blamed_on_hindsight(
    client, juan, tenant
):
    """2026-08-23 review, finding 4: REST's `type` was bare `str | None`
    while its MCP twin (`list_memories`) already typed it as
    Literal["world", "experience", "observation"] -- and `state`, right
    below, got the same Literal treatment in this very branch. A typo here
    forwarded upstream instead of a boundary 422."""
    response = client.post(
        "/v1/memory/list",
        json={"scope": "user", "type": "nonsence"},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_list_memories_rejects_a_bogus_state_not_blamed_on_hindsight(
    client, juan, tenant
):
    """Measured against a live server: state outside {"valid", "invalidated"}
    400s with "Invalid state '...': expected 'valid' or 'invalidated'.",
    which surfaced as a 502 blaming the backend for the caller's typo."""
    response = client.post(
        "/v1/memory/list",
        json={"scope": "user", "state": "anything-else"},
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_correct_rejects_blank_content_at_the_boundary(client, juan, tenant):
    """Upstream checks blank text before the row lookup, so without this the
    reply is 409 MEMORY_NOT_CURATABLE -- a lie about the memory (finding I5).
    Deleting `CorrectRequest`'s `min_length=1`/`_not_blank` validator turns
    this red: pydantic accepts the whitespace-only body and the 422 this test
    asserts never happens."""
    response = client.post(
        "/v1/memory/correct",
        json={
            "scope": "user",
            "memory_id": "11111111-1111-1111-1111-111111111111",
            "content": "   ",
        },
        headers=juan["headers"],
    )

    assert response.status_code == 422


def test_correct_rejects_oversize_content(client, juan, tenant):
    """`correct` writes caller text into a memory exactly as `retain` does, so
    it takes the same MEMORY_MAX_CONTENT_BYTES ceiling (SPEC §20).

    Deleting `_check_content_size(body.content)` from the `correct` handler
    turns this red: the oversize body sails past the boundary to Hindsight.
    The MCP twin got this in Plan 6's F1 fix and the REST route was the half
    left open -- the same one-surface-validated drift F1 existed to close.
    """
    from memory.config import get_settings

    oversize = "A" * (get_settings().max_content_bytes + 1)

    response = client.post(
        "/v1/memory/correct",
        json={
            "scope": "user",
            "memory_id": "11111111-1111-1111-1111-111111111111",
            "content": oversize,
        },
        headers=juan["headers"],
    )

    assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


@respx.mock
def test_curation_route_still_enriches_git_locator_on_an_existing_project(
    client, juan, tenant, session
):
    """The db.commit() in _bank has nothing left to persist for project
    CREATION (create=False, finding 1) -- but resolve() can still enrich an
    existing project's git_locator, and that mutation still needs a commit."""
    client.post(
        "/v1/projects",
        json={"project_slug": "payments-api"},
        headers=juan["headers"],
    )
    route = respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list").mock(
        # "items", not "memories": that is what hindsight-api 0.9.1 actually
        # sends (PROJECT-STATE.md:262). A mock whose shape has drifted from
        # the real upstream is how the chunk_id bank-id leak went unseen.
        return_value=httpx.Response(200, json={"items": []})
    )

    response = client.post(
        "/v1/memory/list",
        json={
            "scope": "project",
            "project_slug": "payments-api",
            "git_locator": "git@github.com:acme/payments-api.git",
        },
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert route.called
    project = (
        session.query(Project)
        .filter_by(tenant_id=tenant, project_slug="payments-api")
        .one()
    )
    assert project.git_locator == "github.com/acme/payments-api"


@respx.mock
def test_bank_id_is_stripped_from_a_curation_response(client, juan, tenant):
    mem_id = "22222222-2222-2222-2222-222222222222"
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{mem_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": mem_id,
                "bank_id": "user_leaked",
                "meta": {"bank_id": "user_leaked_nested"},
            },
        )
    )

    body = client.post(
        "/v1/memory/get",
        json={"scope": "user", "memory_id": mem_id},
        headers=juan["headers"],
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked" not in str(body)
    assert "user_leaked_nested" not in str(body)


@respx.mock
def test_bank_id_embedded_in_chunk_id_is_redacted(client, juan, tenant, session):
    """Measured against a live server: memories/list's chunk_id is literally
    f"{bank_id}_{document_id}_{n}" -- a key-only filter (the shape every mock
    up to this test used) lets the bank_id straight through under a field
    name that is not called "bank_id". No respx test caught this because none
    of them ever included a chunk_id field.
    """
    bank_id = session.get(User, juan["user_id"]).bank_id
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "chunk_id": f"{bank_id}_11111111-1111-1111-1111-111111111111_0",
                    }
                ]
            },
        )
    )

    body = client.post(
        "/v1/memory/list",
        json={"scope": "user"},
        headers=juan["headers"],
    ).json()

    assert bank_id not in str(body)
