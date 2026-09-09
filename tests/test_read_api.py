import json

import httpx
import respx

BASE = "http://hindsight.test"


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@respx.mock
def test_read_recall_returns_a_closed_bounded_hit(client, two_users):
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "memory-1",
                        "text": "Production deployment is disabled.",
                        "type": "observation",
                        "bank_id": "must-not-leak",
                        "embedding": [1, 2, 3],
                    }
                ]
            },
        )
    )

    key = two_users[0]["key"]
    response = client.post(
        "/v1/read/recall",
        json={"scope": "user", "query": "Why is deployment disabled?"},
        headers=_headers(key),
    )

    assert response.status_code == 200
    assert response.json() == {
        "project_slug": None,
        "resolved_from": None,
        "hits": [
            {
                "memory_id": "memory-1",
                "text": "Production deployment is disabled.",
                "fact_type": "observation",
                "state": "valid",
                "kind": None,
                "origin": None,
                "occurred_at": None,
                "document_id": None,
            }
        ],
        "truncated": False,
    }
    assert route.called
    assert "must-not-leak" not in response.text


@respx.mock
def test_read_recall_sends_v040_schema_and_type_tags(client, two_users):
    """Every recall carries the fixed schema tag, optionally narrowed by
    the caller's closed memory types."""
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall"
    ).mock(return_value=httpx.Response(200, json={"results": []}))

    key = two_users[0]["key"]
    response = client.post(
        "/v1/read/recall",
        json={"scope": "user", "query": "database", "kinds": ["decision"]},
        headers=_headers(key),
    )

    assert response.status_code == 200
    sent = json.loads(route.calls.last.request.content)
    assert sent["tags"] == ["schema:ach-retain-v1", "type:decision"]
    assert sent["tags_match"] == "all_strict"
    assert set(sent["types"]) == {"world", "observation"}


@respx.mock
def test_read_recall_ands_caller_tags_into_the_upstream_filter(client, two_users):
    """The caller's own tag narrows the fixed filter without replacing it,
    and is normalised the same way retain writes it."""
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall"
    ).mock(return_value=httpx.Response(200, json={"results": []}))

    key = two_users[0]["key"]
    response = client.post(
        "/v1/read/recall",
        json={"scope": "user", "query": "database", "tags": ["Repo:Group/App"]},
        headers=_headers(key),
    )

    assert response.status_code == 200
    sent = json.loads(route.calls.last.request.content)
    assert sent["tags"] == ["schema:ach-retain-v1", "repo:group/app"]
    assert sent["tags_match"] == "all_strict"


@respx.mock
def test_read_recall_refuses_a_reserved_tag_namespace(client, two_users):
    """The derived `schema:` tag is server-owned; a caller must not be able
    to forge one into the read filter either."""
    key = two_users[0]["key"]
    response = client.post(
        "/v1/read/recall",
        json={"scope": "user", "query": "database", "tags": ["schema:ach-retain-v1"]},
        headers=_headers(key),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_TAG"


@respx.mock
def test_read_recall_withheld_bank_is_currentness_unavailable(client, two_users, session, tenant):
    """An ACH-mediated safety mutation with an unproven upstream outcome
    withholds ordinary current reads for that bank (SPEC §5.8) -- proven here
    without ever reaching Hindsight."""
    from memory.currentness import withhold_bank
    from memory.models import User
    from memory.retained_records import LogicalBankRef

    user_id = two_users[0]["user_id"]
    key = two_users[0]["key"]
    user = session.get(User, user_id)
    bank = LogicalBankRef(tenant, "user", user_id, None, user.bank_id)
    withhold_bank(session, bank, "op-unknown")
    session.commit()

    response = client.post(
        "/v1/read/recall",
        json={"scope": "user", "query": "database"},
        headers=_headers(key),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BANK_CURRENTNESS_UNAVAILABLE"


@respx.mock
def test_read_recall_missing_project_makes_no_upstream_call(client, two_users):
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/recall"
    ).mock(return_value=httpx.Response(200, json={"results": []}))

    key = two_users[0]["key"]
    response = client.post(
        "/v1/read/recall",
        json={"scope": "project", "project_slug": "missing", "query": "q"},
        headers=_headers(key),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert not route.called


@respx.mock
def test_read_history_is_scoped_to_the_resolved_bank(client, two_users):
    memory_id = "22222222-2222-2222-2222-222222222222"
    respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{memory_id}$"
    ).mock(return_value=httpx.Response(200, json={"text": "current", "state": "valid"}))
    history_route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/[^/]+/memories/{memory_id}/history$"
    ).mock(return_value=httpx.Response(200, json=[]))

    key = two_users[0]["key"]
    response = client.post(
        "/v1/read/history",
        json={"scope": "user", "memory_id": memory_id},
        headers=_headers(key),
    )

    assert response.status_code == 200
    assert response.json()["memory_id"] == memory_id
    assert response.json()["changes"] == []
    assert history_route.called
