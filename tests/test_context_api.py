from tests.conftest import create_user


def test_context_load_is_an_authorized_non_creating_read(client, session, tenant):
    user = create_user(client, session)

    response = client.post(
        "/v1/context/load", json={}, headers=user["headers"]
    )

    assert response.status_code == 200
    assert response.json()["text"] == ""
    assert response.json()["total_tokens"] == 0


def test_context_load_rejects_an_unauthenticated_caller(client, tenant):
    response = client.post("/v1/context/load", json={})

    assert response.status_code == 401
