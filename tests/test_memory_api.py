import json

import httpx
import pytest
import respx

BASE = "http://hindsight.test"


from tests.conftest import _create_user


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, object]:
    return _create_user(client, master_headers)


@pytest.fixture
def alice(client, master_headers, tenant) -> dict[str, object]:
    return _create_user(client, master_headers)


@pytest.fixture
def user_key(client, master_headers, tenant) -> tuple[str, str]:
    user = _create_user(client, master_headers)
    return user["user_id"], user["key"]


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _mock_hindsight() -> None:
    respx.put(url__regex=rf"{BASE}/v1/default/banks/[^/]+$").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/config$").mock(
        return_value=httpx.Response(
            200,
            json={
                "config": {
                    "retain_strategies": {
                        "ach-exact-v1": {
                            "retain_extraction_mode": "chunks",
                            "retain_chunk_size": 4096,
                            "retain_structured_chunk_size": 4096,
                        }
                    }
                },
                "overrides": {},
            },
        )
    )


def _retain_body(**overrides) -> dict:
    """The minimum typed-retain shape every REST retain call needs."""
    import uuid

    body = {
        "scope": "user",
        "content": "we use uv",
        "memory_type": "convention",
        "basis": "human_explicit",
        "trigger": "user_requested",
        "evidence": [{"kind": "user_quote", "raw": "We standardized on uv."}],
        "operation_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _create_project(client, key: str, slug: str) -> None:
    response = client.post(
        "/v1/projects", json={"project_slug": slug}, headers=_headers(key)
    )
    assert response.status_code == 201, response.text


@respx.mock
def test_retain_reaches_the_callers_own_bank(client, user_key, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    _, key = user_key

    response = client.post(
        "/v1/memory/retain", json=_retain_body(), headers=_headers(key)
    )

    assert response.status_code == 202, response.text
    assert route.called


@pytest.mark.parametrize("path", ["/v1/memory/retain", "/v1/memory/sync_retain"])
@respx.mock
def test_retain_always_uses_the_fixed_ach_exact_v1_shape(client, user_key, tenant, path):
    """v0.4.0: exact typed retain always selects the frozen `ach-exact-v1`
    strategy and server-derived tags, never overridable by caller input, and
    evidence never reaches the wire payload."""
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    respx.get(url__regex=rf"{BASE}/v1/default/banks/[^/]+/operations/.+").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(memory_type="decision", basis="agent_verified"),
        headers=_headers(key),
    )

    assert response.status_code == 202, response.text
    item = json.loads(route.calls.last.request.read())["items"][0]
    assert item["tags"] == [
        "type:decision", "basis:agent_verified", "schema:ach-retain-v1", "validity:indefinite",
    ]
    assert item["strategy"] == "ach-exact-v1"
    assert "evidence" not in item
    assert "observation_scopes" not in item


def test_rest_retain_requires_typed_fields_and_existing_project(client, user_key, tenant):
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(
            scope="project", project_slug="missing", content="Use PostgreSQL.",
            memory_type="decision", trigger="agent_proactive",
            evidence=[{"kind": "user_quote", "raw": "We will use PostgreSQL."}],
        ),
        headers=_headers(key),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


@respx.mock
def test_retain_response_never_contains_the_bank_id(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/retain", json=_retain_body(), headers=_headers(key)
    ).json()

    assert "bank_id" not in str(body)


@respx.mock
def test_two_users_reach_two_different_banks(client, master_headers, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    banks = []
    for _ in range(2):
        user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
            "user_id"
        ]
        key = client.post(
            f"/v1/users/{user_id}/keys", json={}, headers=master_headers
        ).json()["key"]
        client.post(
            "/v1/memory/retain", json=_retain_body(), headers=_headers(key)
        )
        banks.append(str(route.calls.last.request.url))

    assert banks[0] != banks[1]


@respx.mock
def test_user_key_cannot_target_another_user(client, master_headers, user_key, tenant):
    _mock_hindsight()
    other = client.post("/v1/users", json={}, headers=master_headers).json()["user_id"]
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(user_id=other),
        headers=_headers(key),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@respx.mock
def test_master_key_must_name_the_target_user(client, master_headers, tenant):
    _mock_hindsight()

    response = client.post(
        "/v1/memory/retain", json=_retain_body(), headers=master_headers
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SCOPE"


@respx.mock
def test_master_key_reaches_a_named_user_bank(client, master_headers, user_key, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    user_id, _ = user_key

    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(user_id=user_id),
        headers=master_headers,
    )

    assert response.status_code == 202, response.text
    assert route.called


@respx.mock
def test_project_scope_reaches_a_project_bank(client, two_users, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    juan = two_users[0]
    _create_project(client, juan["key"], "payments-api")

    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(scope="project", project_slug="payments-api"),
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 202, response.text
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_user_and_project_scope_use_different_banks(client, two_users, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    juan = two_users[0]
    _create_project(client, juan["key"], "payments-api")

    client.post(
        "/v1/memory/retain", json=_retain_body(), headers=_headers(juan["key"])
    )
    user_url = str(route.calls.last.request.url)
    client.post(
        "/v1/memory/retain",
        json=_retain_body(scope="project", project_slug="payments-api"),
        headers=_headers(juan["key"]),
    )

    assert user_url != str(route.calls.last.request.url)


@respx.mock
def test_a_stranger_cannot_reach_someone_elses_project(client, two_users, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    juan, alice = two_users
    _create_project(client, juan["key"], "payments-api")
    client.post(
        "/v1/memory/retain",
        json=_retain_body(scope="project", project_slug="payments-api"),
        headers=_headers(juan["key"]),
    )

    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "project_slug": "payments-api", "query": "x"},
        headers=_headers(alice["key"]),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_project_scope_without_a_slug_is_unavailable(client, two_users, tenant):
    response = client.post(
        "/v1/memory/recall",
        json={"scope": "project", "query": "x"},
        headers=_headers(two_users[0]["key"]),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "PROJECT_CONTEXT_UNAVAILABLE"
    # The caller that hits this is usually an LLM holding only the tool
    # schema: "MEMORY_PROJECT or a Git repository" names things it cannot
    # touch, so the message must name the parameter it can actually pass.
    message = response.json()["error"]["message"]
    assert "project_slug" in message
    # And ONLY that one. git_locator was offered here and it is a dead end:
    # no slug means this error whatever locator the call carries, because a
    # locator never resolves identity (inv. 11) and is not unique (§17).
    # Measured against production 2026-08-27, a model that took the offer got
    # the identical error back.
    assert "git_locator" not in message


@respx.mock
def test_retain_against_a_retired_slug_still_reaches_the_project_bank(client, two_users, tenant):
    """After a rename, a retain against the OLD slug must still forward to
    the same project bank. The typed response (v0.4.0) carries no
    resolved_from/notice fields -- it is built entirely from ACH's own DB
    row -- so only the forwarding itself is asserted here."""
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    juan = two_users[0]
    _create_project(client, juan["key"], "payments-api")

    client.post(
        "/v1/memory/retain",
        json=_retain_body(scope="project", project_slug="payments-api"),
        headers=_headers(juan["key"]),
    )
    original_bank_url = str(route.calls.last.request.url)

    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-service"},
        headers=_headers(juan["key"]),
    )

    client.post(
        "/v1/memory/retain",
        json=_retain_body(scope="project", project_slug="payments-api", content="y"),
        headers=_headers(juan["key"]),
    )

    # call_count, not just the last URL: if the second retain never reached
    # Hindsight at all (e.g. it 404'd before forwarding), calls.last still
    # points at the FIRST call and the URL assertion above would pass on that
    # failure path too.
    assert route.call_count == 2
    assert str(route.calls.last.request.url) == original_bank_url


def test_oversize_content_is_rejected(client, user_key, tenant):
    """v0.4.0's canonical-claim ceiling (normalize_claim, 4096 bytes) is
    fixed by spec, not MEMORY_MAX_CONTENT_BYTES-configurable."""
    _, key = user_key

    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(content="x" * 5000),
        headers=_headers(key),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"


@respx.mock
def test_recall_returns_the_upstream_payload(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "mem-1", "text": "we use uv", "type": "world"}]},
        )
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "deps"},
        headers=_headers(key),
    ).json()

    assert body["result"]["hits"][0]["text"] == "we use uv"


@respx.mock
def test_nested_bank_id_is_stripped_from_recall(client, user_key, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "mem-1",
                        "text": "we use uv",
                        "type": "world",
                        "metadata": {"bank_id": "user_leaked_nested"},
                    }
                ],
                "meta": {"inner": {"bank_id": "user_leaked_deep"}},
            },
        )
    )
    _, key = user_key

    body = client.post(
        "/v1/memory/recall",
        json={"scope": "user", "query": "deps"},
        headers=_headers(key),
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked_nested" not in str(body)
    assert "user_leaked_deep" not in str(body)
    assert body["result"]["hits"][0]["text"] == "we use uv"


@respx.mock
def test_retained_record_survives_a_failed_hindsight_call(client, two_users, tenant, session):
    """Pins the commit-before-upstream-call ordering in submit_retain: a
    transport/backend failure after the provenance row is durably committed
    must leave that row (pending, retry-safe) rather than losing it. Typed
    retain is existing-only, so unlike its predecessor this needs a
    pre-existing project rather than relying on first-touch creation."""
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    juan = two_users[0]
    _create_project(client, juan["key"], "first-touch-retain")
    body = _retain_body(scope="project", project_slug="first-touch-retain")

    response = client.post(
        "/v1/memory/retain", json=body, headers=_headers(juan["key"]),
    )

    assert response.status_code == 502

    from memory.models import RetainedRecord

    record = (
        session.query(RetainedRecord)
        .filter_by(tenant_id=tenant, operation_id=body["operation_id"])
        .one()
    )
    assert record.upstream_state == "pending"
    assert record.lifecycle == "active"


@respx.mock
def test_recall_does_not_create_a_project_on_a_missing_slug(
    client, two_users, tenant, session
):
    """Legacy recall is now an existing-only read."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    juan = two_users[0]

    response = client.post(
        "/v1/memory/recall",
        json={
            "scope": "project",
            "project_slug": "first-touch-recall",
            "query": "x",
        },
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 404

    from memory.models import Project, ProjectSlug

    assert session.get(ProjectSlug, (tenant, "first-touch-recall")) is None
    assert session.query(Project).count() == 0


@respx.mock
def test_user_id_is_ignored_under_project_scope(client, two_users, tenant):
    """scope=project always resolves to the named project's bank; a user_id
    riding alongside it must never redirect to that user's own bank."""
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    juan, alice = two_users
    _create_project(client, juan["key"], "payments-api")

    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(
            scope="project", project_slug="payments-api", user_id=alice["user_id"]
        ),
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 202, response.text
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_a_custom_operation_id_is_passed_through(client, juan, tenant):
    _mock_hindsight()
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories$").mock(
        return_value=httpx.Response(200, json={"status": "pending"})
    )
    op_id = "11111111-1111-1111-1111-111111111111"

    client.post(
        "/v1/memory/retain",
        json=_retain_body(operation_id=op_id),
        headers=juan["headers"],
    )

    assert json.loads(route.calls.last.request.read())["operation_id"] == op_id


def test_a_non_uuid_operation_id_on_retain_is_rejected_not_blamed_on_hindsight(
    client, juan, tenant
):
    """Hindsight 422s a non-UUID operation id, which surfaced as a 502 --
    blaming the backend for the caller's typo. Rejected at the boundary
    instead (operation_id is a required UUID field on TypedRetainRequest)."""
    response = client.post(
        "/v1/memory/retain",
        json=_retain_body(operation_id="retry-1"),
        headers=juan["headers"],
    )

    assert response.status_code == 422


@respx.mock
def test_reflect_reaches_the_reflect_endpoint_of_the_right_bank(client, juan, tenant):
    _mock_hindsight()
    setup = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )
    assert setup.status_code == 201
    route = respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"text": "use uv", "usage": {}})
    )

    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=juan["headers"],
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"text": "use uv", "usage": {}}
    assert "banks/project_" in str(route.calls.last.request.url)


@respx.mock
def test_reflect_is_denied_on_someone_elses_project(client, juan, alice, tenant):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"text": "leaked", "usage": {}})
    )
    _create_project(client, juan["key"], "payments-api")

    response = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=alice["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


@respx.mock
def test_bank_id_is_stripped_from_reflect(client, juan, tenant):
    _mock_hindsight()
    setup = client.post(
        "/v1/projects", json={"project_slug": "payments-api"}, headers=juan["headers"]
    )
    assert setup.status_code == 201
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(
            200,
            json={"text": "use uv", "usage": {}, "bank_id": "user_leaked_reflect"},
        )
    )

    body = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=juan["headers"],
    ).json()

    assert "bank_id" not in str(body)
    assert "user_leaked_reflect" not in str(body)
    assert body["result"]["text"] == "use uv"


@respx.mock
def test_reflect_project_row_survives_a_failed_hindsight_call(
    client, two_users, tenant, session
):
    """Reflect is now existing-only and cannot mint a project on failure."""
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    juan = two_users[0]

    response = client.post(
        "/v1/memory/reflect",
        json={
            "scope": "project",
            "project_slug": "first-touch-reflect",
            "query": "x",
        },
        headers=_headers(juan["key"]),
    )

    assert response.status_code == 404

    from memory.models import Project, ProjectSlug

    assert session.get(ProjectSlug, (tenant, "first-touch-reflect")) is None
    assert session.query(Project).count() == 0


@respx.mock
def test_reflect_against_a_retired_slug_forwards_and_pins_resolved_from(
    client, juan, tenant
):
    _mock_hindsight()
    respx.post(url__regex=rf"{BASE}/v1/default/banks/[^/]+/reflect").mock(
        return_value=httpx.Response(200, json={"text": "use uv", "usage": {}})
    )

    _create_project(client, juan["key"], "payments-api")
    client.patch(
        "/v1/projects/payments-api",
        json={"project_slug": "payments-service"},
        headers=juan["headers"],
    )

    body = client.post(
        "/v1/memory/reflect",
        json={"scope": "project", "project_slug": "payments-api", "query": "deps?"},
        headers=juan["headers"],
    ).json()

    assert body["resolved_from"] == "payments-api"
    assert body["project_slug"] == "payments-service"
    assert body["notice"] == "PROJECT_RENAMED"


def test_a_master_key_cannot_reach_another_tenants_user_bank(
    client, master_headers, tenant, session
):
    """Mutating banks.py's `user.tenant_id != principal.tenant_id` away
    survived the whole suite: a master key in tenant A could then address a
    user in tenant B's private bank via scope=user&user_id=... . The control
    -plane equivalent of this test exists (tests/test_users_api.py:499-536);
    the data-plane one never did (2026-08-23 review, R4-I4).

    `tenant` is required (the brief's draft omitted it): resolve_user_bank's
    master-key branch audit-logs cross-tenant bank access with
    tenant_id=principal.tenant_id ("default") before this test's own
    assertions run, so without a "default" Tenant row the mutated code path
    fails on a FOREIGN KEY violation instead of the 200 this test means to
    catch."""
    from memory.models import Tenant, User

    session.add(Tenant(id="other"))
    session.add(
        User(id="usr_elsewhere", tenant_id="other", bank_id="user_other-bank-id")
    )
    session.flush()

    with respx.mock:
        route = respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
            return_value=httpx.Response(200, json={})
        )
        response = client.post(
            "/v1/memory/recall",
            json={"scope": "user", "user_id": "usr_elsewhere", "query": "x"},
            headers=master_headers,
        )

    # 404, not 403 (resolved 2026-08-23, after Task 24). From tenant A's
    # point of view a user that lives only in tenant B does not exist, and
    # saying USER_NOT_FOUND discloses nothing -- whereas 403 would imply "it
    # exists but you may not have it", which is a cross-tenant existence
    # signal. Task 24's branch already answers not-found for both `user is
    # None` and a tenant mismatch, so this asserts exactly what it does.
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "USER_NOT_FOUND", response.text
    assert route.call_count == 0, "the request reached Hindsight before being refused"
