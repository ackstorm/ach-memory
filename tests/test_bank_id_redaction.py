"""The substring half of _strip_bank_id, on every response-bearing route.

SPEC inv. 29 is absolute: bank_id never crosses the API boundary. A live
`memories/list` (hindsight-api 0.9.1, 2026-08-22) embedded it inside
`chunk_id` as f"{bank_id}_{document_id}_{n}" -- invisible to a key-only
filter, which is why `_strip_bank_id` also redacts it as a SUBSTRING and why
every call site must pass its own bank_id.

Before this file existed exactly ONE test constructed a substring-shaped
response (test_curation_api.py's test_bank_id_embedded_in_chunk_id_is_
redacted, covering list_memories only), so the rest of the 28 call sites
across memory/curation/documents/operations/admin/directives/mental_models
were unpinned: mutating `_strip_bank_id(result, bank_id)` to
`_strip_bank_id(result)` across all seven routers left the whole suite green
(2026-08-23 review, R4-C1). Every other existing bank_id test (in
test_admin_api.py, test_directives_api.py, test_mental_models_api.py) only
checks the literal "bank_id" KEY with a placeholder value -- that survives
this mutation unchanged, since key-stripping never depended on the second
argument. Add a row here for every new route that returns an upstream body.
"""

import httpx
import pytest
import respx

BASE = "http://hindsight.test"

# UUID-shaped ids for routes that validate id shape locally
# (HindsightClient._require_uuid: memory_id, operation_id, directive_id).
MEMORY_ID = "11111111-1111-1111-1111-111111111111"
OPERATION_ID = "22222222-2222-2222-2222-222222222222"
DIRECTIVE_ID = "33333333-3333-3333-3333-333333333333"
# mental_model_id is Hindsight-minted as "mm-<32 hex>", NOT a UUID -- only
# traversal/charset-checked locally (paths.reject_mental_model_id_traversal).
MENTAL_MODEL_ID = "mm-1"


@pytest.fixture
def juan(client, master_headers, tenant) -> dict:
    user_id = client.post("/v1/users", json={}, headers=master_headers).json()[
        "user_id"
    ]
    key = client.post(
        f"/v1/users/{user_id}/keys", json={}, headers=master_headers
    ).json()["key"]
    return {"user_id": user_id, "headers": {"Authorization": f"Bearer {key}"}}


def _bank_id(session, user_id: str) -> str:
    from memory.models import User

    return session.get(User, user_id).bank_id


# name -> (method, path, json body, query params, "juan" | "master")
#
# "master" routes (admin.py) are authorized by `require_master`, not a user
# key -- juan's own key would 403 before ever reaching `_strip_bank_id` -- so
# they run under `master_headers` with `user_id` added to `params` at call
# time (see the test body below).
ROUTES = {
    # memory.py
    "retain": ("POST", "/v1/memory/retain", {"scope": "user", "content": "x"}, None, "juan"),
    "recall": ("POST", "/v1/memory/recall", {"scope": "user", "query": "x"}, None, "juan"),
    "reflect": ("POST", "/v1/memory/reflect", {"scope": "user", "query": "x"}, None, "juan"),
    # curation.py
    "list_memories": ("POST", "/v1/memory/list", {"scope": "user"}, None, "juan"),
    "get_memory": (
        "POST", "/v1/memory/get", {"scope": "user", "memory_id": MEMORY_ID}, None, "juan",
    ),
    "forget": (
        "POST", "/v1/memory/forget", {"scope": "user", "memory_id": MEMORY_ID}, None, "juan",
    ),
    "restore": (
        "POST", "/v1/memory/restore", {"scope": "user", "memory_id": MEMORY_ID}, None, "juan",
    ),
    "correct": (
        "POST", "/v1/memory/correct",
        {"scope": "user", "memory_id": MEMORY_ID, "content": "fixed"}, None, "juan",
    ),
    # documents.py
    "list_documents": ("POST", "/v1/memory/documents/list", {"scope": "user"}, None, "juan"),
    "get_document": (
        "POST", "/v1/memory/documents/get", {"scope": "user", "document_id": "d1"}, None, "juan",
    ),
    "delete_document": (
        "POST", "/v1/memory/documents/delete",
        {"scope": "user", "document_id": "d1"}, None, "juan",
    ),
    # operations.py
    "list_operations": ("POST", "/v1/memory/operations/list", {"scope": "user"}, None, "juan"),
    "get_operation": (
        "POST", "/v1/memory/operations/get",
        {"scope": "user", "operation_id": OPERATION_ID}, None, "juan",
    ),
    "cancel_operation": (
        "POST", "/v1/memory/operations/cancel",
        {"scope": "user", "operation_id": OPERATION_ID}, None, "juan",
    ),
    # directives.py -- REST-only, GET/DELETE carry scope in query params.
    "create_directive": (
        "POST", "/v1/directives",
        {"scope": "user", "name": "n", "content": "c"}, None, "juan",
    ),
    "list_directives": ("GET", "/v1/directives", None, {"scope": "user"}, "juan"),
    "get_directive": (
        "GET", f"/v1/directives/{DIRECTIVE_ID}", None, {"scope": "user"}, "juan",
    ),
    "update_directive": (
        "PATCH", f"/v1/directives/{DIRECTIVE_ID}", {"scope": "user", "name": "n2"}, None, "juan",
    ),
    "delete_directive": (
        "DELETE", f"/v1/directives/{DIRECTIVE_ID}", None, {"scope": "user"}, "juan",
    ),
    # mental_models.py -- REST-only, same query-param shape as directives.
    "create_mental_model": (
        "POST", "/v1/mental-models",
        {"scope": "user", "name": "n", "source_query": "q"}, None, "juan",
    ),
    "list_mental_models": ("GET", "/v1/mental-models", None, {"scope": "user"}, "juan"),
    "get_mental_model": (
        "GET", f"/v1/mental-models/{MENTAL_MODEL_ID}", None, {"scope": "user"}, "juan",
    ),
    "update_mental_model": (
        "PATCH", f"/v1/mental-models/{MENTAL_MODEL_ID}",
        {"scope": "user", "name": "n2"}, None, "juan",
    ),
    "delete_mental_model": (
        "DELETE", f"/v1/mental-models/{MENTAL_MODEL_ID}", None, {"scope": "user"}, "juan",
    ),
    "refresh_mental_model": (
        "POST", f"/v1/mental-models/{MENTAL_MODEL_ID}/refresh", None, {"scope": "user"}, "juan",
    ),
    "clear_mental_model": (
        "POST", f"/v1/mental-models/{MENTAL_MODEL_ID}/clear", None, {"scope": "user"}, "juan",
    ),
    # admin.py -- require_master, scope/user_id arrive as query params, never
    # a JSON body. `user_id` is filled in from `juan` at call time below.
    "admin_clear_memories": ("POST", "/v1/admin/memory/user/clear", None, {}, "master"),
    "admin_delete_bank": ("DELETE", "/v1/admin/memory/user", None, {}, "master"),
}


@respx.mock
@pytest.mark.parametrize("name", sorted(ROUTES))
def test_a_bank_id_embedded_in_an_upstream_string_is_redacted(
    client, juan, session, tenant, master_headers, name
):
    method, path, body, params, who = ROUTES[name]
    bank_id = _bank_id(session, juan["user_id"])

    # Every upstream response carries the bank id INSIDE another field's
    # value, under a key no filter looks at -- the shape the live leak had.
    respx.route(url__regex=rf"^{BASE}/.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "m1",
                        "chunk_id": f"{bank_id}_doc7_3",
                        "nested": {"trace": f"resolved via {bank_id} ok"},
                    }
                ]
            },
        )
    )

    params = dict(params or {})
    if who == "master":
        headers = master_headers
        params["user_id"] = juan["user_id"]
    else:
        headers = juan["headers"]

    response = client.request(
        method, path, json=body, params=params, headers=headers
    )
    assert response.status_code < 400, (name, response.text)
    assert bank_id not in response.text, (
        f"{name}: bank_id survived in the response body"
    )
    assert "REDACTED" in response.text, (
        f"{name}: nothing was redacted -- the call site is not passing bank_id"
    )
