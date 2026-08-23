"""REST equivalent of tests/test_mcp_tools.py's
`test_mcp_is_write_flags_match_the_security_table`.

Plan 4 built a table-driven test proving every `is_write` flag on the fifteen
MCP tools is load-bearing (`MCP_IS_WRITE_TABLE`); the directive and
mental-model REST routes added later never got the same coverage. Verified by
mutation: `sed -i 's/is_write=True/is_write=False/g'` across
`src/memory/api/directives.py` and `src/memory/api/mental_models.py` left
`uv run pytest -m "not integration"` at 437 passed -- not one of the eleven
`is_write=True` call sites in those two files was pinned by anything.

Same technique as the MCP table: set the write limit to 1, consume it with an
ordinary write, then every `is_write=True` governance route must refuse with
RATE_LIMITED and every `is_write=False` one must not.
"""

import httpx
import pytest
import respx

BASE = "http://hindsight.test"


@pytest.fixture
def juan(client, master_headers, tenant) -> dict[str, str]:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


DIR_ID = "11111111-1111-1111-1111-111111111111"
MM_ID = "mm-1234567890abcdef1234567890abcdef"

# name -> (method, path, json_body, params, expected is_write)
GOVERNANCE_ROUTES: dict[str, tuple[str, str, dict | None, dict | None, bool]] = {
    "directives.create": (
        "POST", "/v1/directives",
        {"scope": "user", "name": "n", "content": "c"}, None, True,
    ),
    "directives.list": ("GET", "/v1/directives", None, {"scope": "user"}, False),
    "directives.get": (
        "GET", f"/v1/directives/{DIR_ID}", None, {"scope": "user"}, False,
    ),
    "directives.update": (
        "PATCH", f"/v1/directives/{DIR_ID}",
        {"scope": "user", "content": "new"}, None, True,
    ),
    "directives.delete": (
        "DELETE", f"/v1/directives/{DIR_ID}", None, {"scope": "user"}, True,
    ),
    "mental_models.create": (
        "POST", "/v1/mental-models",
        {"scope": "user", "name": "n", "source_query": "q"}, None, True,
    ),
    "mental_models.list": ("GET", "/v1/mental-models", None, {"scope": "user"}, False),
    "mental_models.get": (
        "GET", f"/v1/mental-models/{MM_ID}", None, {"scope": "user"}, False,
    ),
    "mental_models.update": (
        "PATCH", f"/v1/mental-models/{MM_ID}",
        {"scope": "user", "source_query": "q2"}, None, True,
    ),
    "mental_models.delete": (
        "DELETE", f"/v1/mental-models/{MM_ID}", None, {"scope": "user"}, True,
    ),
    "mental_models.refresh": (
        "POST", f"/v1/mental-models/{MM_ID}/refresh", None, {"scope": "user"}, True,
    ),
    "mental_models.clear": (
        "POST", f"/v1/mental-models/{MM_ID}/clear", None, {"scope": "user"}, True,
    ),
}


def test_the_governance_table_covers_every_route_in_both_files(client):
    """A twelfth/thirteenth route landing in either file without an entry
    here must fail loudly, not be silently unverified by the test below."""
    schema = client.get("/openapi.json").json()
    routes = {
        f"{method.upper()} {path}"
        for path, ops in schema["paths"].items()
        if path.startswith(("/v1/directives", "/v1/mental-models"))
        for method in ops
    }
    expected = {
        "POST /v1/directives", "GET /v1/directives",
        "GET /v1/directives/{directive_id}", "PATCH /v1/directives/{directive_id}",
        "DELETE /v1/directives/{directive_id}",
        "POST /v1/mental-models", "GET /v1/mental-models",
        "GET /v1/mental-models/{mental_model_id}",
        "PATCH /v1/mental-models/{mental_model_id}",
        "DELETE /v1/mental-models/{mental_model_id}",
        "POST /v1/mental-models/{mental_model_id}/refresh",
        "POST /v1/mental-models/{mental_model_id}/clear",
    }
    assert routes == expected
    assert len(GOVERNANCE_ROUTES) == len(expected)


@respx.mock
def test_rest_is_write_flags_match_the_governance_table(client, juan, tenant, monkeypatch):
    from memory import ratelimit
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_WRITE_LIMIT", "1")
    monkeypatch.setenv("MEMORY_WRITE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ratelimit.get_limiter.cache_clear()
    respx.route(url__regex=r"^http://hindsight\.test/.*").mock(
        return_value=httpx.Response(200, json={})
    )

    # Consume the whole limit with an ordinary write.
    warmup = client.post(
        "/v1/memory/retain",
        json={"scope": "user", "content": "warmup"},
        headers=juan["headers"],
    )
    assert warmup.status_code == 200, warmup.text

    for name, (method, path, body, params, expect_write) in GOVERNANCE_ROUTES.items():
        response = client.request(
            method, path, json=body, params=params, headers=juan["headers"]
        )
        if expect_write:
            assert response.status_code == 429, (name, response.text)
            assert response.json()["error"]["code"] == "RATE_LIMITED", name
        else:
            assert response.status_code != 429, (name, response.text)
