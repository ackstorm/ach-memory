"""HindsightBackend adapter tests (SPEC §4-§5), `httpx.MockTransport` against its own routes.

Tests the adapter's translation logic in isolation; the shared `Backend` contract
(`tests/backend/test_contract.py`) is what proves behavioural parity with the fake, and runs
this adapter only under `-m integration` against a live Hindsight.
"""
from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from memory.backend.base import Hit, MemoryView, WriteAck
from memory.backend.hindsight import HindsightBackend, _bank
from memory.builtin_models import BuiltinModel
from memory.errors import InvalidRequest, MemoryNotFound, UpstreamError

BASE = "http://hindsight.test"
BANK = "user_11111111-1111-1111-1111-111111111111"
UNIT_1 = "22222222-2222-2222-2222-222222222222"
UNIT_2 = "33333333-3333-3333-3333-333333333333"
OP_ID = "44444444-4444-4444-4444-444444444444"

Route = httpx.Response | Callable[[httpx.Request], httpx.Response]


class _Recorder:
    """Captures the last request to each routed path, so a test can inspect the body/params
    it sent -- `httpx.MockTransport` gives no route object of its own to assert against."""

    def __init__(self) -> None:
        self.calls: dict[tuple[str, str], httpx.Request] = {}

    def route(self, routes: dict[tuple[str, str], Route]) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            key = (request.method, request.url.path)
            self.calls[key] = request
            route = routes.get(key)
            if route is None:
                return httpx.Response(404)
            return route(request) if callable(route) else route
        return httpx.MockTransport(handler)


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def make_backend(monkeypatch, recorder):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", BASE)
    get_settings.cache_clear()

    def _make(routes: dict[tuple[str, str], Route]) -> HindsightBackend:
        return HindsightBackend(transport=recorder.route(routes))

    yield _make
    get_settings.cache_clear()


def _units_responder(valid: list[dict] | None = None, invalidated: list[dict] | None = None):
    """Answers `.../memories/list` by the `state` query param, the way `_units` distinguishes
    a valid-units lookup from an invalidated-units one."""
    valid = valid or []
    invalidated = invalidated or []

    def _respond(request: httpx.Request) -> httpx.Response:
        state = request.url.params.get("state")
        items = invalidated if state == "invalidated" else valid
        return httpx.Response(200, json={"items": items, "total": len(items)})

    return _respond


# ---------------------------------------------------------------------------
# provision
# ---------------------------------------------------------------------------


def test_provision_skips_the_patch_when_config_already_matches(make_backend, recorder):
    backend = make_backend({
        ("PUT", f"{_bank(BANK)}"): httpx.Response(200, json={}),
        ("GET", f"{_bank(BANK)}/config"): httpx.Response(
            200, json={"config": {"retain_extraction_mode": "verbatim"}}
        ),
        ("PATCH", f"{_bank(BANK)}/config"): httpx.Response(200, json={}),
    })

    backend.provision(BANK)

    assert ("PATCH", f"{_bank(BANK)}/config") not in recorder.calls


def test_provision_patches_and_reverifies_when_config_differs(make_backend, recorder):
    responses = iter([
        httpx.Response(200, json={"config": {"retain_extraction_mode": "concise"}}),
        httpx.Response(200, json={"config": {"retain_extraction_mode": "verbatim"}}),
    ])
    backend = make_backend({
        ("PUT", f"{_bank(BANK)}"): httpx.Response(200, json={}),
        ("GET", f"{_bank(BANK)}/config"): lambda request: next(responses),
        ("PATCH", f"{_bank(BANK)}/config"): httpx.Response(200, json={}),
    })

    backend.provision(BANK)

    patch_request = recorder.calls[("PATCH", f"{_bank(BANK)}/config")]
    assert json.loads(patch_request.read()) == {"updates": {"retain_extraction_mode": "verbatim"}}


def test_provision_raises_upstream_error_when_verification_still_fails(make_backend):
    backend = make_backend({
        ("PUT", f"{_bank(BANK)}"): httpx.Response(200, json={}),
        ("GET", f"{_bank(BANK)}/config"): httpx.Response(
            200, json={"config": {"retain_extraction_mode": "concise"}}
        ),
        ("PATCH", f"{_bank(BANK)}/config"): httpx.Response(200, json={}),
    })

    with pytest.raises(UpstreamError):
        backend.provision(BANK)


# ---------------------------------------------------------------------------
# transport / status mapping
# ---------------------------------------------------------------------------


def test_connection_failure_raises_upstream_error(monkeypatch):
    from memory.config import get_settings

    monkeypatch.setenv("MEMORY_HINDSIGHT_URL", BASE)
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend = HindsightBackend(transport=httpx.MockTransport(handler))

    with pytest.raises(UpstreamError):
        backend.get(BANK, "mem_1")


def test_a_4xx_raises_invalid_request(make_backend):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): httpx.Response(400, json={}),
    })

    with pytest.raises(InvalidRequest):
        backend.get(BANK, "mem_1")


# ---------------------------------------------------------------------------
# retain
# ---------------------------------------------------------------------------


def test_retain_sync_sends_replace_and_returns_completed(make_backend, recorder):
    backend = make_backend({
        ("POST", f"{_bank(BANK)}/memories"): httpx.Response(200, json={}),
    })

    ack = backend.retain(BANK, "mem_1", "text", tags=("a", "b"), metadata={"k": "v"}, wait=True)

    body = json.loads(recorder.calls[("POST", f"{_bank(BANK)}/memories")].read())
    assert body["async"] is False
    item = body["items"][0]
    assert item["document_id"] == "mem_1"
    assert item["metadata"] == {"k": "v", "memory_id": "mem_1"}
    assert item["tags"] == ["a", "b"]
    assert item["update_mode"] == "replace"
    assert ack == WriteAck(memory_id="mem_1", status="completed", operation_ref=None)


def test_retain_async_sends_replace_and_returns_pending_with_operation_ref(make_backend, recorder):
    backend = make_backend({
        ("POST", f"{_bank(BANK)}/memories"): httpx.Response(200, json={"operation_id": OP_ID}),
    })

    ack = backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=False)

    body = json.loads(recorder.calls[("POST", f"{_bank(BANK)}/memories")].read())
    assert body["async"] is True
    assert ack == WriteAck(memory_id="mem_1", status="pending", operation_ref=OP_ID)


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------


def test_recall_maps_hits_and_dedupes_by_memory_id(make_backend):
    backend = make_backend({
        ("POST", f"{_bank(BANK)}/memories/recall"): httpx.Response(200, json={
            "results": [
                {
                    "id": UNIT_1, "text": "first", "tags": ["type:fact"],
                    "metadata": {"memory_id": "mem_1"}, "scores": {"semantic": 0.9},
                    "mentioned_at": "2026-01-01",
                },
                # Duplicate memory_id: same logical memory, second raw hit -- dropped.
                {
                    "id": UNIT_2, "text": "first again", "tags": [],
                    "metadata": {"memory_id": "mem_1"}, "scores": {"semantic": 0.5},
                },
            ]
        }),
    })

    hits = backend.recall(BANK, "q", tag_groups=(), limit=10)

    assert hits == [
        Hit(memory_id="mem_1", text="first", score=0.9, tags=("type:fact",),
            metadata={}, mentioned_at="2026-01-01")
    ]


def test_recall_skips_a_hit_with_no_memory_id(make_backend):
    backend = make_backend({
        ("POST", f"{_bank(BANK)}/memories/recall"): httpx.Response(
            200, json={"results": [{"id": UNIT_1, "text": "gone", "metadata": {}}]}
        ),
    })

    hits = backend.recall(BANK, "q", tag_groups=(), limit=10)

    assert hits == []


def test_recall_limit_zero_does_not_call_the_engine(make_backend, recorder):
    backend = make_backend({
        ("POST", f"{_bank(BANK)}/memories/recall"): httpx.Response(200, json={"results": []}),
    })

    hits = backend.recall(BANK, "q", tag_groups=(), limit=0)

    assert hits == []
    assert ("POST", f"{_bank(BANK)}/memories/recall") not in recorder.calls


# ---------------------------------------------------------------------------
# invalidate / revalidate
# ---------------------------------------------------------------------------


def test_invalidate_curates_every_valid_unit(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(
            valid=[{"id": UNIT_1}, {"id": UNIT_2}]
        ),
        ("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}"): httpx.Response(200, json={}),
        ("PATCH", f"{_bank(BANK)}/memories/{UNIT_2}"): httpx.Response(200, json={}),
    })

    backend.invalidate(BANK, "mem_1", reason="stale")

    body = json.loads(recorder.calls[("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}")].read())
    assert body == {"state": "invalidated", "reason": "stale"}
    assert ("PATCH", f"{_bank(BANK)}/memories/{UNIT_2}") in recorder.calls


def test_invalidate_already_invalidated_is_a_noop(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(invalidated=[{"id": UNIT_1}]),
        ("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}"): httpx.Response(200, json={}),
    })

    backend.invalidate(BANK, "mem_1", reason=None)  # must not raise

    assert ("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}") not in recorder.calls


def test_invalidate_unknown_id_raises_not_found(make_backend):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(),
    })

    with pytest.raises(MemoryNotFound):
        backend.invalidate(BANK, "mem_missing", reason=None)


def test_revalidate_curates_every_invalidated_unit(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(invalidated=[{"id": UNIT_1}]),
        ("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}"): httpx.Response(200, json={}),
    })

    backend.revalidate(BANK, "mem_1")

    body = json.loads(recorder.calls[("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}")].read())
    assert body == {"state": "valid"}


def test_revalidate_already_valid_is_a_noop(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(valid=[{"id": UNIT_1}]),
        ("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}"): httpx.Response(200, json={}),
    })

    backend.revalidate(BANK, "mem_1")  # must not raise

    assert ("PATCH", f"{_bank(BANK)}/memories/{UNIT_1}") not in recorder.calls


def test_revalidate_unknown_id_raises_not_found(make_backend):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(),
    })

    with pytest.raises(MemoryNotFound):
        backend.revalidate(BANK, "mem_missing")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_is_idempotent_on_a_404(make_backend):
    backend = make_backend({
        ("DELETE", f"{_bank(BANK)}/documents/mem_1"): httpx.Response(404),
    })

    backend.delete(BANK, "mem_1")  # must not raise


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_returns_the_valid_unit(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(valid=[
            {"id": UNIT_1, "text": "t", "tags": ["a"], "metadata": {"k": "v"},
             "created_at": "2026-01-01"}
        ]),
    })

    view = backend.get(BANK, "mem_1")

    assert view == MemoryView(memory_id="mem_1", text="t", state="valid", tags=("a",),
                               metadata={"k": "v"}, created_at="2026-01-01")
    # _units() must exclude a document's derived observation twin, or get()/invalidate()/
    # revalidate() could surface or curate synthesized text instead of the retained fact.
    request = recorder.calls[("GET", f"{_bank(BANK)}/memories/list")]
    assert request.url.params["type"] == "world"


def test_get_falls_back_to_the_invalidated_unit(make_backend):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(
            invalidated=[{"id": UNIT_1, "text": "t"}]
        ),
    })

    view = backend.get(BANK, "mem_1")

    assert view.state == "invalidated"


def test_get_returns_none_when_unknown(make_backend):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): _units_responder(),
    })

    assert backend.get(BANK, "mem_missing") is None


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_applies_the_first_group_server_side_and_further_groups_client_side(
    make_backend, recorder
):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): httpx.Response(200, json={
            "items": [
                {"document_id": "mem_1", "text": "t1", "tags": ["a", "b"], "state": "valid"},
                {"document_id": "mem_2", "text": "t2", "tags": ["a"], "state": "valid"},
            ],
            "total": 2,
        }),
    })

    page = backend.list(
        BANK,
        tag_groups=({"tags": ["a"], "match": "any"}, {"tags": ["b"], "match": "all"}),
        state=None, limit=10, offset=0,
    )

    params = recorder.calls[("GET", f"{_bank(BANK)}/memories/list")].url.params
    assert params["tags"] == "a"
    assert params["tags_match"] == "any"
    assert [item.memory_id for item in page.items] == ["mem_1"]
    # total is the engine's own count for the first group -- not adjusted for the client-side
    # filter, so it can overcount when more than one group is given.
    assert page.total == 2


def test_list_rejects_an_invalid_further_group_match(make_backend):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/memories/list"): httpx.Response(
            200, json={"items": [{"document_id": "mem_1", "tags": ["a"]}], "total": 1}
        ),
    })

    with pytest.raises(ValueError, match="invalid match"):
        backend.list(
            BANK,
            tag_groups=({"tags": ["a"], "match": "any"}, {"tags": ["b"], "match": "bogus"}),
            state=None, limit=10, offset=0,
        )


# ---------------------------------------------------------------------------
# reflect
# ---------------------------------------------------------------------------


def test_reflect_returns_the_answer_text(make_backend, recorder):
    backend = make_backend({
        ("POST", f"{_bank(BANK)}/reflect"): httpx.Response(200, json={"text": "the answer"}),
    })

    answer = backend.reflect(BANK, "q", tag_groups=({"tags": ["a"], "match": "any"},))

    body = json.loads(recorder.calls[("POST", f"{_bank(BANK)}/reflect")].read())
    assert body["tag_groups"] == [{"tags": ["a"], "match": "any_strict"}]
    assert answer == "the answer"


def test_reflect_returns_empty_string_when_no_text(make_backend):
    backend = make_backend({
        ("POST", f"{_bank(BANK)}/reflect"): httpx.Response(200, json={}),
    })

    assert backend.reflect(BANK, "q", tag_groups=()) == ""


def test_reflect_rejects_an_invalid_tag_group_match(make_backend):
    backend = make_backend({})

    with pytest.raises(ValueError, match="invalid match"):
        backend.reflect(BANK, "q", tag_groups=({"tags": ["a"], "match": "bogus"},))


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def test_capabilities_declare_synthesis_operations_and_mental_models(make_backend):
    backend = make_backend({})

    assert backend.capabilities() == frozenset({"synthesis", "operations", "mental_models"})


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


def test_get_operation_strips_the_bank_id(make_backend):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/operations/{OP_ID}"): httpx.Response(
            200, json={"status": "completed", "bank_id": BANK, "note": f"in {BANK}"}
        ),
    })

    result = backend.get_operation(BANK, OP_ID)

    assert "bank_id" not in result
    assert BANK not in result["note"]


def test_list_operations_passes_through_with_filters_and_strips_bank_id(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/operations"): httpx.Response(
            200, json={"items": [{"operation_id": OP_ID, "bank_id": BANK}], "total": 1}
        ),
    })

    result = backend.list_operations(BANK, status="pending", limit=5, offset=0)

    params = recorder.calls[("GET", f"{_bank(BANK)}/operations")].url.params
    assert params["status"] == "pending"
    assert "bank_id" not in result["items"][0]


def test_cancel_operation_passes_through_and_strips_bank_id(make_backend):
    backend = make_backend({
        ("DELETE", f"{_bank(BANK)}/operations/{OP_ID}"): httpx.Response(
            200, json={"status": "cancelled", "bank_id": BANK}
        ),
    })

    result = backend.cancel_operation(BANK, OP_ID)

    assert "bank_id" not in result


# ---------------------------------------------------------------------------
# mental models
# ---------------------------------------------------------------------------


def test_provision_mental_models_creates_a_missing_model(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/mental-models"): httpx.Response(200, json={"items": []}),
        ("POST", f"{_bank(BANK)}/mental-models"): httpx.Response(200, json={"id": "mm-1"}),
    })
    builtin = BuiltinModel(key="user-context", name="User Context", prompt="summarize",
                            version=1, source_tags=("schema:ach-retain-v1",), tags_match="all")

    backend.provision_mental_models(BANK, (builtin,))

    body = json.loads(recorder.calls[("POST", f"{_bank(BANK)}/mental-models")].read())
    assert body == {
        "name": "User Context", "source_query": "summarize",
        "tags": ["schema:ach-retain-v1"], "tags_match": "all",
    }


def test_provision_mental_models_updates_a_model_with_a_changed_prompt(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/mental-models"): httpx.Response(200, json={
            "items": [{"id": "mm-1", "name": "User Context", "source_query": "old prompt"}]
        }),
        ("PATCH", f"{_bank(BANK)}/mental-models/mm-1"): httpx.Response(200, json={}),
    })
    builtin = BuiltinModel(key="user-context", name="User Context", prompt="new prompt",
                            version=2)

    backend.provision_mental_models(BANK, (builtin,))

    assert ("PATCH", f"{_bank(BANK)}/mental-models/mm-1") in recorder.calls


def test_provision_mental_models_is_a_noop_when_the_prompt_is_unchanged(make_backend, recorder):
    backend = make_backend({
        ("GET", f"{_bank(BANK)}/mental-models"): httpx.Response(200, json={
            "items": [{"id": "mm-1", "name": "User Context", "source_query": "same"}]
        }),
    })
    builtin = BuiltinModel(key="user-context", name="User Context", prompt="same", version=1)

    backend.provision_mental_models(BANK, (builtin,))

    assert ("PATCH", f"{_bank(BANK)}/mental-models/mm-1") not in recorder.calls
    assert ("POST", f"{_bank(BANK)}/mental-models") not in recorder.calls
