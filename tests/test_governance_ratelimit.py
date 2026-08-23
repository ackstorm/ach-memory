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
MEM_ID = "33333333-3333-3333-3333-333333333333"
OP_ID = "44444444-4444-4444-4444-444444444444"

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
    # curation, documents, operations -- the three routers the original table
    # never reached. `correct` writes caller text into the bank and
    # `delete_document` is SPEC §12.2's only hard-delete lever; both were
    # unmetered.
    "memory.list": ("POST", "/v1/memory/list", {"scope": "user"}, None, False),
    "memory.get": (
        "POST", "/v1/memory/get",
        {"scope": "user", "memory_id": MEM_ID}, None, False,
    ),
    "memory.forget": (
        "POST", "/v1/memory/forget",
        {"scope": "user", "memory_id": MEM_ID}, None, True,
    ),
    "memory.restore": (
        "POST", "/v1/memory/restore",
        {"scope": "user", "memory_id": MEM_ID}, None, True,
    ),
    "memory.correct": (
        "POST", "/v1/memory/correct",
        {"scope": "user", "memory_id": MEM_ID, "content": "fixed"}, None, True,
    ),
    "documents.list": (
        "POST", "/v1/memory/documents/list", {"scope": "user"}, None, False,
    ),
    "documents.get": (
        "POST", "/v1/memory/documents/get",
        {"scope": "user", "document_id": "d1"}, None, False,
    ),
    "documents.delete": (
        "POST", "/v1/memory/documents/delete",
        {"scope": "user", "document_id": "d1"}, None, True,
    ),
    "operations.list": (
        "POST", "/v1/memory/operations/list", {"scope": "user"}, None, False,
    ),
    "operations.get": (
        "POST", "/v1/memory/operations/get",
        {"scope": "user", "operation_id": OP_ID}, None, False,
    ),
    "operations.cancel": (
        "POST", "/v1/memory/operations/cancel",
        {"scope": "user", "operation_id": OP_ID}, None, True,
    ),
}


def test_the_governance_table_covers_every_route_in_all_five_files(client):
    """A new route landing in any of these files without an entry here must
    fail loudly, not be silently unverified by the test below. Originally
    scoped to directives + mental-models only, which is why curation,
    documents and operations went uncovered -- five is_write=True flags were
    individually deletable with the suite green (2026-08-23 review, R4-I2)."""
    schema = client.get("/openapi.json").json()
    prefixes = (
        "/v1/directives",
        "/v1/mental-models",
        "/v1/memory/list",
        "/v1/memory/get",
        "/v1/memory/forget",
        "/v1/memory/restore",
        "/v1/memory/correct",
        "/v1/memory/documents",
        "/v1/memory/operations",
    )
    routes = {
        f"{method.upper()} {path}"
        for path, ops in schema["paths"].items()
        if path.startswith(prefixes)
        for method in ops
    }
    # GOVERNANCE_ROUTES paths carry concrete ids for the request to hit
    # (DIR_ID, MM_ID); the OpenAPI schema reports the route template instead
    # ("{directive_id}"). Normalize back to the template form to compare.
    covered = {
        f"{method} {path}".replace(DIR_ID, "{directive_id}").replace(
            MM_ID, "{mental_model_id}"
        )
        for method, path, _, _, _ in GOVERNANCE_ROUTES.values()
    }
    assert routes == covered, (
        f"uncovered: {sorted(routes - covered)}; stale: {sorted(covered - routes)}"
    )


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
            # != 429 alone let a route that regressed to 500 pass as "not
            # rate limited". A read route must actually answer.
            assert response.status_code < 500, (name, response.text)
