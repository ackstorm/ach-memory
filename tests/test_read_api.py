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
                        "text": "Capture is disabled.",
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
        json={"scope": "user", "query": "Why is capture disabled?"},
        headers=_headers(key),
    )

    assert response.status_code == 200
    assert response.json() == {
        "project_slug": None,
        "resolved_from": None,
        "hits": [
            {
                "memory_id": "memory-1",
                "text": "Capture is disabled.",
                "fact_type": "observation",
                "state": "valid",
                "kind": None,
                "origin": None,
                "eligibility": None,
                "occurred_at": None,
                "document_id": None,
            }
        ],
        "truncated": False,
    }
    assert route.called
    assert "must-not-leak" not in response.text


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
