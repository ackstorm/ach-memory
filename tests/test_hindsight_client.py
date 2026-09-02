import httpx
import pytest
import respx

from memory.errors import (
    DocumentNotFound,
    DomainError,
    HindsightError,
    MemoryNotFound,
    MentalModelNotFound,
    OperationNotFound,
    UpstreamRejected,
)
from memory.hindsight.client import HindsightClient

BASE = "http://hindsight.test"
BANK = "user_11111111-1111-1111-1111-111111111111"
MEM_ID = "22222222-2222-2222-2222-222222222222"
OP_ID = "33333333-3333-3333-3333-333333333333"
ABSENT_ID = "00000000-0000-0000-0000-000000000000"
# Hindsight-minted shape (measured live, hindsight-api 0.9.1, 2026-08-22):
# "mm-" + 32 hex chars -- NOT a UUID. See paths.reject_mental_model_id_traversal.
MM_ID = "mm-" + "1" * 32


def test_httpx_logger_is_muted_by_the_app(caplog, configured_env):
    import logging

    from memory.api.app import create_app

    create_app()

    assert logging.getLogger("httpx").level >= logging.WARNING


@pytest.fixture
def client() -> HindsightClient:
    return HindsightClient(base_url=BASE, api_key="secret", tenant_id="default")


@respx.mock
def test_retain_posts_the_item_envelope(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"success": True, "operation_id": "op-1"})
    )

    result = client.retain(BANK, "we use uv", metadata={"agent": "codex"})

    assert result["operation_id"] == "op-1"
    body = route.calls.last.request.read()
    import json

    payload = json.loads(body)
    assert payload["items"][0]["content"] == "we use uv"
    assert payload["items"][0]["metadata"] == {"agent": "codex"}
    assert payload["async"] is True


@respx.mock
def test_retain_sends_the_api_key(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.retain(BANK, "x")

    assert route.calls.last.request.headers["authorization"] == "Bearer secret"


@respx.mock
def test_sync_retain_sets_async_false(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    client.retain(BANK, "x", is_async=False)

    import json

    assert json.loads(route.calls.last.request.read())["async"] is False


@respx.mock
def test_recall_posts_the_query(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"memories": []})
    )

    client.recall(BANK, "how do we do migrations")

    import json

    assert json.loads(route.calls.last.request.read())["query"] == (
        "how do we do migrations"
    )


@respx.mock
def test_recall_with_no_new_filter_sends_the_exact_pre_phase_5_body(client):
    """An existing caller that never learns about the new keyword-only
    filters must see its request unchanged: this pins the body to exactly
    `{"query": ...}`, no key added by default for any of them."""
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    client.recall(BANK, "how do we do migrations")

    import json

    assert json.loads(route.calls.last.request.read()) == {
        "query": "how do we do migrations"
    }


@respx.mock
def test_recall_forwards_types_and_prefer_observations(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    client.recall(
        BANK,
        "q",
        types=["observation", "world", "experience"],
        prefer_observations=True,
    )

    import json

    body = json.loads(route.calls.last.request.read())
    assert body["types"] == ["observation", "world", "experience"]
    assert body["prefer_observations"] is True


@respx.mock
def test_recall_omits_prefer_observations_when_left_at_its_upstream_default(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    client.recall(BANK, "q", types=["world"])

    import json

    assert "prefer_observations" not in json.loads(route.calls.last.request.read())


@respx.mock
def test_recall_forwards_tags_and_tags_match(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    client.recall(BANK, "q", tags=["evidence_only", "kind:gotcha"], tags_match="all_strict")

    import json

    body = json.loads(route.calls.last.request.read())
    assert body["tags"] == ["evidence_only", "kind:gotcha"]
    assert body["tags_match"] == "all_strict"


@respx.mock
def test_recall_forwards_tag_groups_verbatim(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    groups = [{"or": [{"tags": ["a"], "match": "any"}, {"tags": ["b"], "match": "any"}]}]

    client.recall(BANK, "q", tag_groups=groups)

    import json

    assert json.loads(route.calls.last.request.read())["tag_groups"] == groups


@respx.mock
def test_recall_forwards_max_tokens(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    client.recall(BANK, "q", max_tokens=1200)

    import json

    assert json.loads(route.calls.last.request.read())["max_tokens"] == 1200


@respx.mock
def test_recall_still_disables_entities_alongside_the_new_filters(client):
    """`with_entities` and the new keyword filters are independent knobs on
    the same request body; setting one must not disturb the other."""
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    client.recall(BANK, "q", with_entities=False, types=["world"])

    import json

    body = json.loads(route.calls.last.request.read())
    assert body["include"] == {"entities": None}
    assert body["types"] == ["world"]


@respx.mock
def test_get_memory_history_hits_the_history_subpath(client):
    route = respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{MEM_ID}/history").mock(
        return_value=httpx.Response(200, json=[])
    )

    client.get_memory_history(BANK, MEM_ID)

    assert route.calls.last.request.url.path == (
        f"/v1/default/banks/{BANK}/memories/{MEM_ID}/history"
    )


@respx.mock
def test_get_memory_history_returns_whatever_shape_upstream_sends_unreshaped(client):
    """The response is untyped in hindsight-api 0.9.2's own openapi.json, so
    this method reshapes nothing -- a bare array survives as a list, an
    object survives as a dict. Defensive normalization against either shape
    is `read_service.py`'s job, not this client's."""
    route = respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{MEM_ID}/history").mock(
        return_value=httpx.Response(
            200,
            json=[{"text": "old value", "changed_at": "2026-08-01T00:00:00Z"}],
        )
    )

    result = client.get_memory_history(BANK, MEM_ID)

    assert route.called
    assert result == [{"text": "old value", "changed_at": "2026-08-01T00:00:00Z"}]


@respx.mock
def test_a_non_uuid_memory_id_is_rejected_locally_for_history_with_no_http_call(client):
    # No route mocked: an unmocked request means the local guard did not run.
    with pytest.raises(MemoryNotFound):
        client.get_memory_history(BANK, "ghost")


@respx.mock
def test_get_memory_history_404_is_memory_not_found(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{ABSENT_ID}/history").mock(
        return_value=httpx.Response(404, json={"detail": "no such memory"})
    )

    with pytest.raises(MemoryNotFound):
        client.get_memory_history(BANK, ABSENT_ID)


@respx.mock
def test_get_memory_history_not_found_never_carries_the_bank_id(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{ABSENT_ID}/history").mock(
        return_value=httpx.Response(404, json={"detail": f"bank {BANK} lacks it"})
    )

    with pytest.raises(MemoryNotFound) as caught:
        client.get_memory_history(BANK, ABSENT_ID)

    rendered = f"{caught.value!r} {caught.value.details} {caught.value.__context__!r}"
    assert BANK not in rendered
    assert caught.value.__cause__ is None


# -- Hostile upstream payloads: this client layer only parses JSON and maps
# -- status codes, never field-level shape. Interpreting a recall/history
# -- payload into typed hits -- and failing closed or dropping one malformed
# -- entry when a field is missing, oversized, wrongly typed or a nested bank
# -- ID leaked into a fact's own metadata -- is read_service.py's job (Task
# -- 4 of this plan), which owns the only layer that actually reads these
# -- fields. What this client must prove is narrower and structural: it never
# -- inspects, mutates, validates or crashes on the response body, for any of
# -- these, so nothing here silently reshapes hostile data into something
# -- that merely looks safe.


@respx.mock
def test_recall_passes_through_a_nested_bank_id_untouched(client):
    hostile = {
        "results": [
            {
                "id": MEM_ID,
                "text": "t",
                "type": "world",
                "metadata": {"bank_id": "project_should_not_leak_here"},
            }
        ]
    }
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json=hostile)
    )

    result = client.recall(BANK, "q")

    assert result == hostile


@respx.mock
def test_recall_passes_through_an_oversized_result_array_untouched(client):
    hostile = {"results": [{"id": f"m{n}", "text": "x" * 10_000} for n in range(500)]}
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json=hostile)
    )

    result = client.recall(BANK, "q")

    assert len(result["results"]) == 500


@respx.mock
def test_recall_passes_through_a_result_missing_its_id_untouched(client):
    hostile = {"results": [{"text": "no id field at all"}]}
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json=hostile)
    )

    result = client.recall(BANK, "q")

    assert "id" not in result["results"][0]


@respx.mock
def test_recall_passes_through_an_undocumented_fact_type_untouched(client):
    hostile = {"results": [{"id": MEM_ID, "text": "t", "type": "tool_trace_dump"}]}
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/recall").mock(
        return_value=httpx.Response(200, json=hostile)
    )

    result = client.recall(BANK, "q")

    assert result["results"][0]["type"] == "tool_trace_dump"


@respx.mock
def test_get_memory_history_passes_through_a_self_referential_source_fact(client):
    """JSON itself cannot encode a true reference cycle -- `json.loads`
    always produces a tree -- but a change can still list its OWN memory_id
    as one of its source facts, a logical cycle a consumer that walks
    `source_facts` must guard against. This client neither notices nor
    cares: it returns the structure exactly as sent."""
    hostile = [
        {
            "text": "t",
            "changed_at": "2026-08-01T00:00:00Z",
            "source_facts": [{"id": MEM_ID, "text": "cites itself"}],
        }
    ]
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{MEM_ID}/history").mock(
        return_value=httpx.Response(200, json=hostile)
    )

    result = client.get_memory_history(BANK, MEM_ID)

    assert result == hostile


def test_ensure_bank_is_a_bare_upsert_with_no_config_patch(client, respx_mock):
    """The bank upsert survives; the config PATCH does not.

    Both fields v1 used to set are gone: `memory_defense` was accepted and
    ignored by hindsight-api 0.9.1 (measured, SPEC §20.2) and screening moved
    to the LiteLLM pre_mcp_call guardrail; `store_document_text` now stays at
    Hindsight's default of True so `update_mode="append"` works (SPEC §11.4).
    A PATCH here would be a no-op round trip on every cold bank.
    """
    put = respx_mock.put("/v1/default/banks/user_abc").respond(200, json={})
    patch = respx_mock.patch("/v1/default/banks/user_abc/config")

    client.ensure_bank("user_abc")

    assert put.called
    assert not patch.called


def test_ensure_bank_does_not_cache_across_calls(client, respx_mock):
    """No TTL cache: every call issues the PUT.

    The cache existed only to skip the config PATCH. It was per process, so
    with replicaCount>1 a delete_bank served by one pod left another pod's
    entry live and that pod then skipped re-materialization (review finding
    I3). With nothing left to skip, the cache is pure liability.
    """
    put = respx_mock.put("/v1/default/banks/user_abc").respond(200, json={})

    client.ensure_bank("user_abc")
    client.ensure_bank("user_abc")

    assert put.call_count == 2


def test_delete_bank_needs_no_cache_eviction(client, respx_mock):
    """delete_bank is a plain DELETE now.

    Its eviction existed to stop a stale cache entry from skipping the config
    PATCH after the bank was torn down. There is no cache and no PATCH.
    """
    route = respx_mock.delete("/v1/default/banks/user_abc").respond(
        200, json={"message": "Bank 'user_abc' and all associated data deleted successfully"}
    )

    client.delete_bank("user_abc")

    assert route.called


@respx.mock
def test_upstream_failure_becomes_a_hindsight_error(client):
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with pytest.raises(HindsightError):
        client.retain(BANK, "x")


@respx.mock
def test_hindsight_error_does_not_carry_the_bank_id(client):
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(500, text=f"bank {BANK} exploded")
    )

    with pytest.raises(HindsightError) as caught:
        client.retain(BANK, "x")

    assert BANK not in str(caught.value)
    assert BANK not in str(caught.value.details)


@respx.mock
def test_transport_failure_does_not_chain_the_bank_id(client):
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(HindsightError) as caught:
        client.retain(BANK, "x")

    error = caught.value
    assert error.__cause__ is None
    # Not "no bank id in the repr" — the exception chain must be empty, so
    # there is nothing for a traceback renderer or error reporter to walk.
    assert error.__context__ is None
    assert BANK not in str(error)
    assert BANK not in str(error.details)


@respx.mock
def test_failed_materialization_is_retried(client):
    upsert = respx.put(f"{BASE}/v1/default/banks/{BANK}").mock(
        return_value=httpx.Response(500, text="boom")
    )

    for _ in range(2):
        with pytest.raises(HindsightError):
            client.ensure_bank(BANK)

    assert upsert.call_count == 2


@respx.mock
def test_reflect_posts_the_query(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/reflect").mock(
        return_value=httpx.Response(200, json={"answer": "use uv"})
    )

    result = client.reflect(BANK, "how do we manage dependencies")

    assert result == {"answer": "use uv"}
    assert route.calls.last.request.read() == b'{"query":"how do we manage dependencies"}'


@respx.mock
def test_list_memories_uses_the_list_subpath_and_drops_empty_filters(client):
    route = respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/list").mock(
        return_value=httpx.Response(200, json={"memories": []})
    )

    client.list_memories(BANK, q="alembic", state=None, limit=10)

    url = route.calls.last.request.url
    assert url.path == f"/v1/default/banks/{BANK}/memories/list"
    assert dict(url.params) == {"q": "alembic", "limit": "10"}


@respx.mock
def test_forget_and_restore_are_one_patch_with_different_states(client):
    route = respx.patch(f"{BASE}/v1/default/banks/{BANK}/memories/{MEM_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEM_ID})
    )

    client.curate(BANK, MEM_ID, state="invalidated", reason="wrong")
    forget_body = route.calls.last.request.read()
    client.curate(BANK, MEM_ID, state="valid")
    restore_body = route.calls.last.request.read()
    client.curate(BANK, MEM_ID, text="uv, not pip")
    correct_body = route.calls.last.request.read()

    assert forget_body == b'{"state":"invalidated","reason":"wrong"}'
    assert restore_body == b'{"state":"valid"}'
    assert correct_body == b'{"text":"uv, not pip"}'


@respx.mock
def test_cancel_operation_does_not_use_the_delete_suffix(client):
    """DELETE .../operations/{id} cancels; .../{id}/delete is a different
    endpoint that removes a terminal operation, and v1 does not expose it."""
    route = respx.delete(f"{BASE}/v1/default/banks/{BANK}/operations/{OP_ID}").mock(
        return_value=httpx.Response(200, json={"status": "cancelled"})
    )

    client.cancel_operation(BANK, OP_ID)

    assert route.calls.last.request.url.path.endswith(f"/operations/{OP_ID}")


@respx.mock
def test_cancel_operation_409_is_operation_not_cancellable(client):
    """A terminal operation is a caller-visible conflict, not a backend fault."""
    respx.delete(f"{BASE}/v1/default/banks/{BANK}/operations/{OP_ID}").mock(
        return_value=httpx.Response(
            409, json={"detail": f"operation {OP_ID} in bank {BANK} is complete"}
        )
    )

    with pytest.raises(DomainError) as caught:
        client.cancel_operation(BANK, OP_ID)

    assert caught.value.code == "OPERATION_NOT_CANCELLABLE"
    rendered = f"{caught.value!r} {caught.value.details} {caught.value.__context__!r}"
    assert BANK not in rendered
    assert OP_ID not in rendered
    assert "complete" not in rendered


@respx.mock
def test_an_upstream_404_becomes_the_supplied_not_found_error(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{ABSENT_ID}").mock(
        return_value=httpx.Response(404, json={"detail": "no such memory"})
    )

    with pytest.raises(MemoryNotFound):
        client.get_memory(BANK, ABSENT_ID)


@respx.mock
def test_other_upstream_failures_stay_hindsight_errors(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{MEM_ID}").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    with pytest.raises(HindsightError):
        client.get_memory(BANK, MEM_ID)


@respx.mock
def test_a_not_found_error_never_carries_the_bank_id(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{ABSENT_ID}").mock(
        return_value=httpx.Response(404, json={"detail": f"bank {BANK} lacks it"})
    )

    with pytest.raises(MemoryNotFound) as caught:
        client.get_memory(BANK, ABSENT_ID)

    rendered = f"{caught.value!r} {caught.value.details} {caught.value.__context__!r}"
    assert BANK not in rendered
    # Not "no bank id in the repr" — the exception chain must be empty on
    # BOTH links, so there is nothing for a traceback renderer or error
    # reporter to walk. See test_transport_failure_does_not_chain_the_bank_id
    # above for why __context__ alone is not enough.
    assert caught.value.__cause__ is None


@respx.mock
def test_a_404_mapping_is_logged_naming_only_the_error_class(client, caplog):
    """A wrong tenant_id or a path renamed on a Hindsight upgrade produces the
    same 404 as a genuine miss (measured against a live server). Without a
    signal here that misconfiguration is silent forever. The log line must
    still never carry the path, the URL, the response body or the bank id."""
    respx.get(f"{BASE}/v1/default/banks/{BANK}/memories/{ABSENT_ID}").mock(
        return_value=httpx.Response(404, json={"detail": f"bank {BANK} lacks it"})
    )

    with caplog.at_level("WARNING", logger="memory.hindsight"), pytest.raises(
        MemoryNotFound
    ):
        client.get_memory(BANK, ABSENT_ID)

    messages = [r.getMessage() for r in caplog.records]
    assert any("MemoryNotFound" in m for m in messages)
    assert not any(BANK in m for m in messages)
    assert not any("ghost" in m or "/memories/" in m for m in messages)


@respx.mock
def test_list_documents_uses_the_documents_path(client):
    route = respx.get(f"{BASE}/v1/default/banks/{BANK}/documents").mock(
        return_value=httpx.Response(200, json={"documents": []})
    )

    client.list_documents(BANK, q="onboarding", limit=5)

    url = route.calls.last.request.url
    assert url.path == f"/v1/default/banks/{BANK}/documents"
    assert dict(url.params) == {"q": "onboarding", "limit": "5"}


@respx.mock
def test_get_document_returns_the_document(client):
    # document_id is caller-managed and arbitrary (SPEC), unlike memory_id and
    # operation_id — colons and slashes are a legitimate id.
    doc_id = "github:acme/api:pr:382"
    respx.get(f"{BASE}/v1/default/banks/{BANK}/documents/{doc_id}").mock(
        return_value=httpx.Response(200, json={"id": doc_id})
    )

    result = client.get_document(BANK, doc_id)

    assert result["id"] == doc_id


@respx.mock
def test_get_document_404_is_document_not_found(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/documents/missing-doc").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    with pytest.raises(DocumentNotFound):
        client.get_document(BANK, "missing-doc")


@respx.mock
def test_delete_document_hits_the_document_path(client):
    route = respx.delete(f"{BASE}/v1/default/banks/{BANK}/documents/doc_1").mock(
        return_value=httpx.Response(200, json={"deleted": True})
    )

    client.delete_document(BANK, "doc_1")

    assert route.called


@respx.mock
def test_delete_document_404_is_document_not_found(client):
    respx.delete(f"{BASE}/v1/default/banks/{BANK}/documents/missing-doc").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    with pytest.raises(DocumentNotFound):
        client.delete_document(BANK, "missing-doc")


@respx.mock
def test_get_operation_returns_the_operation(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/operations/{OP_ID}").mock(
        return_value=httpx.Response(200, json={"id": OP_ID, "status": "completed"})
    )

    result = client.get_operation(BANK, OP_ID)

    assert result["status"] == "completed"


@respx.mock
def test_get_operation_404_is_operation_not_found(client):
    """Defensive: the `not_found=` mapping stays in place in case a future
    Hindsight version (or a different failure mode) ever does send a genuine
    404 here. Measured live, it currently never does -- see
    test_get_operation_status_not_found_is_operation_not_found below for
    what the real server actually sends."""
    respx.get(f"{BASE}/v1/default/banks/{BANK}/operations/{ABSENT_ID}").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    with pytest.raises(OperationNotFound):
        client.get_operation(BANK, ABSENT_ID)


@respx.mock
def test_get_operation_status_not_found_is_operation_not_found(client):
    """Measured against a live server (hindsight-api, 2026-08-22): GET on an
    absent operation is a 200 with {"operation_id": ..., "status":
    "not_found"}, never a 404 -- the not_found= mapping above never actually
    fires for this route. Without this check get_operation returned 200 with
    a body that looked like a real, found operation."""
    respx.get(f"{BASE}/v1/default/banks/{BANK}/operations/{ABSENT_ID}").mock(
        return_value=httpx.Response(
            200, json={"operation_id": ABSENT_ID, "status": "not_found"}
        )
    )

    with pytest.raises(OperationNotFound):
        client.get_operation(BANK, ABSENT_ID)


@respx.mock
def test_list_operations_uses_the_operations_path(client):
    route = respx.get(f"{BASE}/v1/default/banks/{BANK}/operations").mock(
        return_value=httpx.Response(200, json={"operations": []})
    )

    client.list_operations(BANK, status="pending", limit=5)

    url = route.calls.last.request.url
    assert url.path == f"/v1/default/banks/{BANK}/operations"
    assert dict(url.params) == {"status": "pending", "limit": "5"}


@respx.mock
def test_a_non_uuid_memory_id_is_rejected_locally_with_no_http_call(client):
    # No route is mocked: if either method fails to reject "ghost" before the
    # round trip, respx raises on the unmocked request instead of the
    # expected error, and the test fails.
    with pytest.raises(MemoryNotFound):
        client.get_memory(BANK, "ghost")

    with pytest.raises(MemoryNotFound):
        client.curate(BANK, "ghost", text="x")


@respx.mock
def test_a_non_uuid_operation_id_is_rejected_locally_with_no_http_call(client):
    with pytest.raises(OperationNotFound):
        client.get_operation(BANK, "op_1")

    with pytest.raises(OperationNotFound):
        client.cancel_operation(BANK, "op_1")


def test_get_operation_reports_failed_when_every_child_errored(client, respx_mock):
    """Upstream leaves the parent `pending` forever when the work failed.

    Measured live 2026-08-22 against hindsight-api 0.9.1: an async retain
    whose child failed sat at `pending` for 30s of polling, with the reason
    only in child_operations[0].error_message. A caller polling for a terminal
    status never gets one, so we derive it.
    """
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={
            "operation_id": "op-1",
            "status": "pending",
            "child_operations": [
                {"operation_id": "c-1", "status": "pending", "error_message": "ValueError: nope"}
            ],
        },
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "failed"


def test_get_operation_leaves_a_partially_failed_parent_alone(client, respx_mock):
    """One failed child among several is not a failed operation.

    Work may still be in flight; reporting `failed` would stop a caller
    polling for the rest.
    """
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={
            "operation_id": "op-1",
            "status": "pending",
            "child_operations": [
                {"operation_id": "c-1", "status": "pending", "error_message": "ValueError: nope"},
                {"operation_id": "c-2", "status": "pending"},
            ],
        },
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "pending"


def test_get_operation_does_not_touch_a_terminal_status(client, respx_mock):
    """`completed` stays `completed`, children or not."""
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={
            "operation_id": "op-1",
            "status": "completed",
            "child_operations": [
                {"operation_id": "c-1", "status": "completed", "error_message": "warn"}
            ],
        },
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "completed"


def test_get_operation_leaves_alone_when_no_child_operations_key_at_all(client, respx_mock):
    """A pending operation with no `child_operations` key must not crash and
    must not be reported failed.

    The three brief tests only exercise a populated list; none pins the
    absent-key case, and `_derive_failed` reads it via
    `record.get("child_operations") or []`. A future rewrite to
    `record["child_operations"]` would KeyError on exactly this shape --
    plausible, since an operation that hasn't spawned any children yet may
    omit the key rather than send `[]` or `null`.
    """
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={"operation_id": "op-1", "status": "pending"},
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "pending"


@pytest.mark.parametrize("children", [[], None], ids=["empty-list", "null"])
def test_get_operation_leaves_alone_when_there_are_no_children(
    client, respx_mock, children
):
    """The other two childless shapes: `[]` and an explicit `null`.

    Both ride the same `or []` guard as the absent key, and neither was
    pinned. `null` is the one that bites: drop the guard and
    `all(... for child in None)` raises TypeError inside the client, which
    reaches a caller as a 500 on an ordinary poll. `[]` matters for a
    different reason -- `all()` over an empty sequence is True, so only the
    separate `not children` check stops a brand-new operation with no children
    yet from being reported failed.
    """
    respx_mock.get(url__regex=r".*/operations/.*").respond(
        200,
        json={
            "operation_id": "op-1",
            "status": "pending",
            "child_operations": children,
        },
    )

    result = client.get_operation("user_abc", "11111111-1111-1111-1111-111111111111")

    assert result["status"] == "pending"


# ---------------------------------------------------------------------------
# Mental models: mental_model_id is Hindsight-minted as `mm-<32 hex>`, NOT a
# UUID (measured live). Before the fix, `_require_uuid` rejected every real
# id locally, so get/update/delete/refresh/clear 404'd forever -- only
# create/list ever reached Hindsight. These pin the replacement
# traversal/charset guard: a real id round-trips, a dot-segment payload is
# still rejected with no HTTP call.
# ---------------------------------------------------------------------------


@respx.mock
def test_get_mental_model_returns_a_real_mm_id(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}").mock(
        return_value=httpx.Response(200, json={"id": MM_ID})
    )

    result = client.get_mental_model(BANK, MM_ID)

    assert result["id"] == MM_ID


@respx.mock
def test_get_mental_model_404_is_mental_model_not_found(client):
    respx.get(f"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}").mock(
        return_value=httpx.Response(404, json={"detail": "nope"})
    )

    with pytest.raises(MentalModelNotFound):
        client.get_mental_model(BANK, MM_ID)


@respx.mock
def test_a_dot_segment_mental_model_id_is_rejected_locally_with_no_http_call(client):
    # No route is mocked for any of these: if the guard fails to reject ".."
    # before the round trip, respx raises on the unmocked request instead of
    # the expected error, and the test fails. A real UUID ("not-a-uuid"-style
    # rejection is gone on purpose -- that was the bug) is deliberately NOT
    # tested here; see test_mental_models_api.py for that corrected case.
    with pytest.raises(MentalModelNotFound):
        client.get_mental_model(BANK, "..")
    with pytest.raises(MentalModelNotFound):
        client.update_mental_model(BANK, "..", name="x")
    with pytest.raises(MentalModelNotFound):
        client.delete_mental_model(BANK, "..")
    with pytest.raises(MentalModelNotFound):
        client.refresh_mental_model(BANK, "..")
    with pytest.raises(MentalModelNotFound):
        client.clear_mental_model(BANK, "..")
    with pytest.raises(MentalModelNotFound):
        client.dry_run_refresh_mental_model(BANK, "..")


# ---------------------------------------------------------------------------
# Trusted tags and dry-run-refresh (Phase 4 Task 2): explicit server-owned
# `tags` on create/update, and a wholly separate internal-only dry-run-refresh
# client method. Neither is reachable from CreateMentalModelRequest/
# UpdateMentalModelRequest or any route -- see test_mental_models_api.py for
# the public-boundary side of this.
# ---------------------------------------------------------------------------


@respx.mock
def test_create_mental_model_sends_explicit_tags_when_supplied(client):
    import json

    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/mental-models").mock(
        return_value=httpx.Response(201, json={"id": MM_ID})
    )

    client.create_mental_model(BANK, name="n", source_query="q", tags=["a", "b"])

    sent = json.loads(route.calls.last.request.content)
    assert sent["tags"] == ["a", "b"]


@respx.mock
def test_create_mental_model_omits_tags_when_not_supplied(client):
    """Same discipline as `trigger`: an omitted `tags` sends no "tags" key at
    all, never `[]` or a default of our own."""
    import json

    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/mental-models").mock(
        return_value=httpx.Response(201, json={"id": MM_ID})
    )

    client.create_mental_model(BANK, name="n", source_query="q")

    sent = json.loads(route.calls.last.request.content)
    assert "tags" not in sent


@respx.mock
def test_update_mental_model_sends_explicit_tags_when_supplied(client):
    import json

    route = respx.patch(f"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}").mock(
        return_value=httpx.Response(200, json={"id": MM_ID})
    )

    client.update_mental_model(BANK, MM_ID, tags=[])

    sent = json.loads(route.calls.last.request.content)
    assert sent["tags"] == []


@respx.mock
def test_update_mental_model_omits_tags_when_not_supplied(client):
    import json

    route = respx.patch(f"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}").mock(
        return_value=httpx.Response(200, json={"id": MM_ID})
    )

    client.update_mental_model(BANK, MM_ID, name="renamed")

    sent = json.loads(route.calls.last.request.content)
    assert "tags" not in sent


@respx.mock
def test_create_mental_model_trigger_carries_response_schema_and_keep_trace_verbatim(
    client,
):
    """Pins the existing verbatim trigger pass-through (no new handling
    added for this): a structured-profile trigger's `mode`/`response_schema`/
    `keep_trace` keys reach the wire exactly as given."""
    import json

    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/mental-models").mock(
        return_value=httpx.Response(201, json={"id": MM_ID})
    )
    trigger = {
        "mode": "full",
        "response_schema": {"type": "object", "properties": {"user_profile": {}}},
        "keep_trace": True,
    }

    client.create_mental_model(BANK, name="n", source_query="q", trigger=trigger)

    sent = json.loads(route.calls.last.request.content)
    assert sent["trigger"] == trigger


@respx.mock
def test_list_mental_models_full_detail_forwards_structured_output_unmangled(client):
    """Pins the existing generic dict pass-through (no new handling added for
    this, same as the trigger pin above): Task 5's typed section loader will
    call `list_mental_models(detail="full")` and read
    `reflect_response.structured_output` off each item, so that nested field
    must survive this client's list call byte-for-byte, with no reshaping or
    validation added at this layer."""
    structured_output = {
        "user_profile": {
            "interaction": [],
            "engineering": [{"claim": "Use uv, not pip.", "kind": "convention"}],
        }
    }
    based_on = {"memories": ["11111111-1111-1111-1111-111111111111"]}
    # `(\?|$)` -- same overlap trap as test_mental_models_api.py's
    # test_list_mental_models_forwards_query_params: an unanchored regex here
    # also matches `.../mental-models/{id}`.
    route = respx.get(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models(\?|$)"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "mental_models": [
                    {
                        "id": MM_ID,
                        "reflect_response": {
                            "structured_output": structured_output,
                            "based_on": based_on,
                        },
                    }
                ]
            },
        )
    )

    result = client.list_mental_models(BANK, detail="full")

    assert dict(route.calls.last.request.url.params) == {"detail": "full"}
    model = result["mental_models"][0]
    assert model["reflect_response"]["structured_output"] == structured_output
    assert model["reflect_response"]["based_on"] == based_on


@respx.mock
def test_dry_run_refresh_mental_model_sends_no_request_body(client):
    route = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}/dry-run-refresh$"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "usage": {"input_tokens": 1000, "output_tokens": 200},
                "duration_ms": 4321,
                "diff": {"added": 2, "removed": 1},
                "preview_content": '{"user_profile": {}}',
            },
        )
    )

    result = client.dry_run_refresh_mental_model(BANK, MM_ID)

    assert route.calls.last.request.content == b""
    # A dict pass-through, same as every other method in this file -- no
    # response parsing/validation added at this layer.
    assert result["usage"] == {"input_tokens": 1000, "output_tokens": 200}
    assert result["duration_ms"] == 4321
    assert result["diff"] == {"added": 2, "removed": 1}
    assert result["preview_content"] == '{"user_profile": {}}'


@respx.mock
def test_dry_run_refresh_mental_model_404_is_mental_model_not_found(client):
    respx.post(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}/dry-run-refresh$"
    ).mock(return_value=httpx.Response(404, json={"detail": "nope"}))

    with pytest.raises(MentalModelNotFound):
        client.dry_run_refresh_mental_model(BANK, MM_ID)


@respx.mock
def test_dry_run_refresh_does_not_hit_refresh_or_clear_and_vice_versa(client):
    """Same overlap trap as test_mental_models_api.py's
    test_clear_uses_the_clear_suffix_not_the_refresh_suffix: `.../refresh`,
    `.../clear`, `.../history` and `.../dry-run-refresh` all overlap under an
    unanchored regex on this shared `{id}` prefix. Every mock here is
    `$`-anchored, and each call must hit exactly one of the three routes."""
    refresh = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}/refresh$"
    ).mock(return_value=httpx.Response(200, json={"status": "refreshing"}))
    clear = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}/clear$"
    ).mock(return_value=httpx.Response(200, json={"status": "cleared"}))
    dry_run = respx.post(
        url__regex=rf"{BASE}/v1/default/banks/{BANK}/mental-models/{MM_ID}/dry-run-refresh$"
    ).mock(return_value=httpx.Response(200, json={"usage": {}, "duration_ms": 1}))

    result = client.dry_run_refresh_mental_model(BANK, MM_ID)

    assert result == {"usage": {}, "duration_ms": 1}
    assert dry_run.call_count == 1
    assert refresh.call_count == 0
    assert clear.call_count == 0


@respx.mock
def test_a_curate_refused_upstream_is_not_a_backend_error(client):
    """Hindsight 400s a curate on a derived `observation` -- "only
    world/experience facts can be curated". That is a property of the memory
    the caller named, so a 502 would tell an agent to retry something that can
    never succeed."""
    from memory.errors import MemoryNotCuratable

    respx.patch(f"{BASE}/v1/default/banks/{BANK}/memories/{MEM_ID}").mock(
        return_value=httpx.Response(
            400, json={"detail": f"Memory is a observation; bank {BANK}"}
        )
    )

    with pytest.raises(MemoryNotCuratable) as caught:
        client.curate(BANK, MEM_ID, state="invalidated")

    rendered = f"{caught.value!r} {caught.value.details} {caught.value.__context__!r}"
    assert BANK not in rendered


@respx.mock
def test_other_upstream_400s_stay_backend_errors(client):
    """Only the calls that pass `bad_request` get the typed mapping; a 400
    from anywhere else is still an unexpected backend response."""
    respx.get(f"{BASE}/v1/default/banks/{BANK}/documents").mock(
        return_value=httpx.Response(400, json={"detail": "nope"})
    )

    with pytest.raises(HindsightError):
        client.list_documents(BANK)


@respx.mock
def test_an_upstream_422_is_not_reported_as_a_backend_fault(client):
    """FastAPI answers a schema violation with 422. Folding it into
    HINDSIGHT_ERROR tells an agent to retry what can never succeed. Deleting
    the `response.status_code == 422` branch in `_request` turns this red:
    the request falls through to the generic `>= 400` branch and raises
    HindsightError (status 502) instead of UpstreamRejected (status 400)."""
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(422, json={"detail": "nope"})
    )

    with pytest.raises(UpstreamRejected) as excinfo:
        client.retain(BANK, "content")

    assert excinfo.value.status == 400


def test_the_llm_bound_calls_get_a_longer_read_timeout(configured_env):
    """`sync_retain` blocks until Hindsight has run extraction through an LLM
    and `reflect` is a full synthesis, but both shared the 30s timeout a cheap
    GET uses. A ReadTimeout surfaces as HINDSIGHT_ERROR 502 -- "retry" -- while
    the upstream worker completes the original write anyway, so the retry
    duplicates it."""
    import respx

    from memory.hindsight.client import get_client

    get_client.cache_clear()
    client = get_client()

    with respx.mock:
        sync = respx.post(url__regex=r".*/memories$").respond(200, json={})
        client.retain("user_x", "content", is_async=False)
        assert sync.calls.last.request.extensions["timeout"]["read"] >= 180

    with respx.mock:
        asy = respx.post(url__regex=r".*/memories$").respond(200, json={})
        client.retain("user_x", "content", is_async=True)
        assert asy.calls.last.request.extensions["timeout"]["read"] <= 30

    with respx.mock:
        refl = respx.post(url__regex=r".*/reflect$").respond(200, json={})
        client.reflect("user_x", "q")
        assert refl.calls.last.request.extensions["timeout"]["read"] >= 180

    with respx.mock:
        cheap = respx.get(url__regex=r".*/memories/list$").respond(200, json={})
        client.list_memories("user_x")
        assert cheap.calls.last.request.extensions["timeout"]["read"] <= 30

    with respx.mock:
        # Same reasoning as refresh_mental_model's own comment: dry-run-refresh
        # costs exactly the same LLM call as a real refresh.
        dry = respx.post(url__regex=r".*/dry-run-refresh$").respond(200, json={})
        client.dry_run_refresh_mental_model("user_x", MM_ID)
        assert dry.calls.last.request.extensions["timeout"]["read"] >= 180


def test_a_bodiless_or_non_json_success_is_not_an_internal_error(configured_env):
    """`.json()` was called unconditionally on anything below 400. A
    JSONDecodeError is not an httpx.HTTPError, so it walked past _request's
    handler and became INTERNAL_ERROR -- a code outside every branch a caller
    could reasonably handle."""
    import pytest
    import respx

    from memory.errors import HindsightError
    from memory.hindsight.client import get_client

    get_client.cache_clear()
    client = get_client()

    # 204 No Content: an empty body is an empty result, not a failure.
    with respx.mock:
        respx.delete(url__regex=r".*/documents/.*").respond(204)
        assert client.delete_document("user_x", "d1") == {}

    # A non-JSON 2xx (an intermediary's HTML error page) must be a typed
    # backend failure, never INTERNAL_ERROR.
    with respx.mock:
        respx.delete(url__regex=r".*/documents/.*").respond(
            200, content=b"<html>gateway</html>", headers={"content-type": "text/html"}
        )
        with pytest.raises(HindsightError):
            client.delete_document("user_x", "d2")


def test_an_upstream_auth_failure_does_not_report_its_status(configured_env):
    """An upstream 401 means OUR MEMORY_HINDSIGHT_API_KEY is misconfigured.
    Reporting {'upstream_status': 401} told an untrusted MCP client about the
    backend's auth state, which is not the caller's business."""
    import pytest
    import respx

    from memory.errors import HindsightError
    from memory.hindsight.client import get_client

    get_client.cache_clear()
    with respx.mock:
        respx.post(url__regex=r".*/memories$").respond(401, json={"detail": "nope"})
        with pytest.raises(HindsightError) as excinfo:
            get_client().retain("user_x", "c")

    assert "401" not in str(excinfo.value.details), excinfo.value.details


@respx.mock
def test_upstream_calls_are_timed(client):
    """`method` and `status` only. The path carries the bank id and must
    never become a label."""
    from prometheus_client import REGISTRY

    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"success": True, "operation_id": "op-1"})
    )
    before = REGISTRY.get_sample_value(
        "memory_hindsight_request_seconds_count", {"method": "POST", "status": "200"}
    ) or 0.0

    client.retain(BANK, "hello")

    after = REGISTRY.get_sample_value(
        "memory_hindsight_request_seconds_count", {"method": "POST", "status": "200"}
    )
    assert after == before + 1


@respx.mock
def test_transport_failure_is_timed_as_error(client):
    """A transport failure never yields a status code -- it is recorded as
    status="error", and the original DomainError still propagates unchanged."""
    from prometheus_client import REGISTRY

    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        side_effect=httpx.ConnectError("boom")
    )
    before = REGISTRY.get_sample_value(
        "memory_hindsight_request_seconds_count", {"method": "POST", "status": "error"}
    ) or 0.0

    with pytest.raises(HindsightError):
        client.retain(BANK, "hello")

    after = REGISTRY.get_sample_value(
        "memory_hindsight_request_seconds_count", {"method": "POST", "status": "error"}
    )
    assert after == before + 1


# ---------------------------------------------------------------------------
# Task 4: dry_run_extract, get_bank_config, retain_items
# ---------------------------------------------------------------------------


@respx.mock
def test_dry_run_extract_posts_content_and_overrides(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(200, json={"facts": [{"text": "we use uv"}]})
    )

    result = client.dry_run_extract(
        BANK, "we use uv", retain_extraction_mode="verbatim", retain_mission="keep as-is"
    )

    assert result["facts"][0]["text"] == "we use uv"
    import json

    payload = json.loads(route.calls.last.request.read())
    assert payload == {
        "content": "we use uv",
        "retain_extraction_mode": "verbatim",
        "retain_mission": "keep as-is",
    }


@respx.mock
def test_dry_run_extract_omits_absent_overrides(client):
    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(200, json={"facts": []})
    )

    client.dry_run_extract(BANK, "we use uv")

    import json

    payload = json.loads(route.calls.last.request.read())
    assert payload == {"content": "we use uv"}


@respx.mock
def test_dry_run_extract_never_retains(client):
    """No route is mocked for POST .../memories (only .../dry-run-extract):
    if dry_run_extract ever fell through to the real retain path, respx
    would raise on the unmocked request and this test would fail."""
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(200, json={"facts": []})
    )

    client.dry_run_extract(BANK, "we use uv")


@respx.mock
def test_dry_run_extract_error_does_not_carry_the_bank_id(client):
    respx.post(f"{BASE}/v1/default/banks/{BANK}/memories/dry-run-extract").mock(
        return_value=httpx.Response(500, text=f"bank {BANK} exploded")
    )

    with pytest.raises(HindsightError) as caught:
        client.dry_run_extract(BANK, "x")

    assert BANK not in str(caught.value)
    assert BANK not in str(caught.value.details)


@respx.mock
def test_get_bank_config_is_a_plain_get(client):
    route = respx.get(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json={"retain_default_strategy": "concise"})
    )

    result = client.get_bank_config(BANK)

    assert result == {"retain_default_strategy": "concise"}
    assert route.calls.last.request.method == "GET"


@respx.mock
def test_no_client_method_ever_patches_the_config_path(client):
    """The route exists in the client's vocabulary (get_bank_config reads
    it) but nothing here ever writes it -- this phase's config PATCH stays
    undeployed until a later, separately-approved rollout."""
    patch_route = respx.patch(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{BASE}/v1/default/banks/{BANK}/config").mock(
        return_value=httpx.Response(200, json={})
    )

    client.get_bank_config(BANK)

    assert not patch_route.called


@respx.mock
def test_retain_items_sends_one_operation_id_and_item_level_fields(client):
    from memory.hindsight.client import RetainItem

    route = respx.post(f"{BASE}/v1/default/banks/{BANK}/memories").mock(
        return_value=httpx.Response(200, json={"operation_id": OP_ID})
    )
    items = [
        RetainItem(
            content="we use uv",
            document_id="doc-1",
            metadata={"host": "claude-code"},
            tags=["kind:convention", "profile_eligible"],
            observation_scopes=[["profile_eligible"]],
            strategy="candidate_verbatim",
        ),
        RetainItem(
            content="ci runs on every push",
            document_id="doc-1",
            metadata={"host": "claude-code"},
            tags=["kind:convention", "evidence_only"],
            observation_scopes=[["evidence_only"]],
            strategy="candidate_verbatim",
        ),
    ]

    client.retain_items(BANK, items, operation_id=OP_ID)

    import json

    payload = json.loads(route.calls.last.request.read())
    assert payload["operation_id"] == OP_ID
    assert payload["async"] is True
    assert len(payload["items"]) == 2
    assert payload["items"][0]["tags"] == ["kind:convention", "profile_eligible"]
    assert payload["items"][0]["observation_scopes"] == [["profile_eligible"]]
    assert payload["items"][0]["strategy"] == "candidate_verbatim"
    assert payload["items"][0]["document_id"] == "doc-1"


def test_retain_items_rejects_a_non_uuid_operation_id_with_no_http_call(client):
    from memory.hindsight.client import RetainItem

    item = RetainItem(
        content="x", document_id="doc-1", metadata={}, tags=[], observation_scopes=[],
        strategy="candidate_verbatim",
    )

    with pytest.raises(ValueError, match="operation_id"):
        client.retain_items(BANK, [item], operation_id="not-a-uuid")
