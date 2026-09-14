"""Backend adapter contract (SPEC §4): one module, every adapter runs it.

Parametrized over every `Backend` implementation: the fake, always, and Hindsight, only when
`MEMORY_HINDSIGHT_URL` is set (the `integration` marker skips it from the default run).
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

from memory.backend.base import Backend
from memory.backend.fake import FakeBackend
from memory.builtin_models import BuiltinModel
from memory.errors import MemoryNotFound, UnsupportedCapability
from tests.conftest import PLACEHOLDER_HINDSIGHT_URL

BANK = "bank-a"
OTHER_BANK = "bank-b"


@pytest.fixture(params=[pytest.param("fake"), pytest.param("hindsight", marks=pytest.mark.integration)])
def backend(request, monkeypatch):
    if request.param == "fake":
        adapter = FakeBackend()
        adapter.provision(BANK)
        return adapter
    if request.param == "hindsight":  # pragma: no cover - exercised only with the marker enabled
        base_url = os.environ.get("MEMORY_HINDSIGHT_URL")
        if not base_url or base_url == PLACEHOLDER_HINDSIGHT_URL:
            pytest.skip("MEMORY_HINDSIGHT_URL not set")
        from memory.backend.hindsight import HindsightBackend
        from memory.config import get_settings

        get_settings.cache_clear()
        adapter = HindsightBackend()
        # Every test below addresses its bank through the module-level BANK / OTHER_BANK
        # constants, not a fixture return value -- rebind them to fresh, per-test banks so the
        # live suite does not share state across test runs.
        this_module = sys.modules[__name__]
        monkeypatch.setattr(this_module, "BANK", f"contract-{uuid.uuid4().hex[:8]}")
        monkeypatch.setattr(this_module, "OTHER_BANK", f"contract-{uuid.uuid4().hex[:8]}")
        adapter.provision(BANK)
        return adapter
    raise AssertionError(f"unknown backend param {request.param!r}")  # pragma: no cover


def test_retain_with_existing_memory_id_replaces_the_memory(backend):
    ack = backend.retain(BANK, "mem_1", "original text", tags=("a",),
                          metadata={"k": "v1"}, wait=True)
    assert ack.memory_id == "mem_1"

    backend.retain(BANK, "mem_1", "replaced text", tags=("b",),
                    metadata={"k": "v2"}, wait=True)

    view = backend.get(BANK, "mem_1")
    assert view.memory_id == "mem_1"
    assert view.text == "replaced text"
    assert view.tags == ("b",)
    assert view.metadata == {"k": "v2"}


def test_invalidate_unknown_id_raises_not_found(backend):
    with pytest.raises(MemoryNotFound):
        backend.invalidate(BANK, "mem_missing", reason=None)


def test_invalidate_already_invalidated_is_a_noop(backend):
    backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=True)
    backend.invalidate(BANK, "mem_1", reason="first")

    backend.invalidate(BANK, "mem_1", reason="second")  # must not raise

    assert backend.get(BANK, "mem_1").state == "invalidated"


def test_revalidate_unknown_id_raises_not_found(backend):
    with pytest.raises(MemoryNotFound):
        backend.revalidate(BANK, "mem_missing")


def test_revalidate_valid_id_is_a_noop(backend):
    backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=True)

    backend.revalidate(BANK, "mem_1")  # must not raise

    assert backend.get(BANK, "mem_1").state == "valid"


def test_delete_is_idempotent_and_hides_the_memory(backend):
    backend.retain(BANK, "mem_1", "delete me", tags=(), metadata={}, wait=True)

    backend.delete(BANK, "mem_1")
    backend.delete(BANK, "mem_1")  # second delete, unknown id, must not raise

    assert backend.get(BANK, "mem_1") is None
    assert backend.recall(BANK, "delete", tag_groups=(), limit=10) == []


def test_recall_never_returns_invalidated_memories(backend):
    backend.retain(BANK, "mem_1", "apple banana", tags=(), metadata={}, wait=True)
    backend.invalidate(BANK, "mem_1", reason=None)

    hits = backend.recall(BANK, "apple banana", tag_groups=(), limit=10)

    assert hits == []


def test_recall_respects_tag_groups(backend):
    backend.retain(BANK, "mem_1", "apple", tags=("fruit", "red"), metadata={}, wait=True)
    backend.retain(BANK, "mem_2", "apple", tags=("fruit",), metadata={}, wait=True)
    backend.retain(BANK, "mem_3", "apple", tags=("vegetable",), metadata={}, wait=True)

    any_hits = backend.recall(
        BANK, "apple",
        tag_groups=({"tags": ["fruit", "vegetable"], "match": "any"},), limit=10,
    )
    assert {h.memory_id for h in any_hits} == {"mem_1", "mem_2", "mem_3"}

    all_hits = backend.recall(
        BANK, "apple",
        tag_groups=({"tags": ["fruit", "red"], "match": "all"},), limit=10,
    )
    assert {h.memory_id for h in all_hits} == {"mem_1"}


def test_recall_ands_multiple_tag_groups(backend):
    backend.retain(BANK, "mem_1", "apple", tags=("type:constraint", "proj:a"),
                    metadata={}, wait=True)
    backend.retain(BANK, "mem_2", "apple", tags=("type:constraint", "proj:b"),
                    metadata={}, wait=True)
    backend.retain(BANK, "mem_3", "apple", tags=("type:preference", "proj:a"),
                    metadata={}, wait=True)

    hits = backend.recall(
        BANK, "apple",
        tag_groups=(
            {"tags": ["type:constraint"], "match": "any"},
            {"tags": ["proj:a"], "match": "all"},
        ),
        limit=10,
    )

    assert {h.memory_id for h in hits} == {"mem_1"}


def test_recall_returns_at_most_limit(backend):
    for i in range(5):
        backend.retain(BANK, f"mem_{i}", "apple", tags=(), metadata={}, wait=True)

    hits = backend.recall(BANK, "apple", tag_groups=(), limit=2)

    assert len(hits) == 2


def test_list_filters_by_state_counts_total_and_pages(backend):
    for i in range(3):
        backend.retain(BANK, f"mem_valid_{i}", "text", tags=(), metadata={}, wait=True)
    backend.retain(BANK, "mem_gone", "text", tags=(), metadata={}, wait=True)
    backend.invalidate(BANK, "mem_gone", reason=None)

    valid_page = backend.list(BANK, tag_groups=(), state=None, limit=2, offset=0)
    assert valid_page.total == 3
    assert [item.memory_id for item in valid_page.items] == ["mem_valid_2", "mem_valid_1"]

    invalidated_page = backend.list(BANK, tag_groups=(), state="invalidated",
                                     limit=10, offset=0)
    assert invalidated_page.total == 1
    assert [item.memory_id for item in invalidated_page.items] == ["mem_gone"]

    second_page = backend.list(BANK, tag_groups=(), state=None, limit=2, offset=2)
    assert [item.memory_id for item in second_page.items] == ["mem_valid_0"]


def test_list_state_none_and_invalidated_partition_the_bank(backend):
    backend.retain(BANK, "mem_valid", "text", tags=(), metadata={}, wait=True)
    backend.retain(BANK, "mem_gone", "text", tags=(), metadata={}, wait=True)
    backend.invalidate(BANK, "mem_gone", reason=None)

    valid_ids = {
        item.memory_id
        for item in backend.list(BANK, tag_groups=(), state=None, limit=10, offset=0).items
    }
    invalidated_ids = {
        item.memory_id
        for item in backend.list(BANK, tag_groups=(), state="invalidated", limit=10, offset=0).items
    }

    assert valid_ids == {"mem_valid"}
    assert invalidated_ids == {"mem_gone"}
    assert valid_ids.isdisjoint(invalidated_ids)


def test_list_limit_zero_returns_nothing(backend):
    backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=True)

    page = backend.list(BANK, tag_groups=(), state=None, limit=0, offset=0)

    assert page.items == ()
    assert page.total == 1


def test_list_offset_beyond_total_is_an_empty_page_with_correct_total(backend):
    backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=True)

    page = backend.list(BANK, tag_groups=(), state=None, limit=10, offset=100)

    assert page.items == ()
    assert page.total == 1


def test_recall_limit_zero_returns_nothing(backend):
    backend.retain(BANK, "mem_1", "apple", tags=(), metadata={}, wait=True)

    hits = backend.recall(BANK, "apple", tag_groups=(), limit=0)

    assert hits == []


def test_retain_on_invalidated_id_revalidates_it(backend):
    backend.retain(BANK, "mem_1", "original", tags=(), metadata={}, wait=True)
    backend.invalidate(BANK, "mem_1", reason=None)

    backend.retain(BANK, "mem_1", "replaced", tags=(), metadata={}, wait=True)

    assert backend.get(BANK, "mem_1").state == "valid"
    hits = backend.recall(BANK, "replaced", tag_groups=(), limit=10)
    assert [h.memory_id for h in hits] == ["mem_1"]


def test_backend_never_aliases_caller_or_returned_data(backend):
    metadata = {"k": "v"}
    backend.retain(BANK, "mem_1", "text", tags=(), metadata=metadata, wait=True)
    metadata["k"] = "mutated-after-retain-call"
    assert backend.get(BANK, "mem_1").metadata == {"k": "v"}

    view = backend.get(BANK, "mem_1")
    view.metadata["k"] = "mutated-via-view"
    assert backend.get(BANK, "mem_1").metadata == {"k": "v"}

    hits = backend.recall(BANK, "text", tag_groups=(), limit=10)
    hits[0].metadata["k"] = "mutated-via-hit"
    assert backend.recall(BANK, "text", tag_groups=(), limit=10)[0].metadata == {"k": "v"}

    listed = backend.list(BANK, tag_groups=(), state=None, limit=10, offset=0).items[0]
    listed.metadata["k"] = "mutated-via-list-item"
    assert backend.get(BANK, "mem_1").metadata == {"k": "v"}


def test_banks_are_isolated(backend):
    backend.provision(OTHER_BANK)
    backend.retain(BANK, "mem_1", "only in bank-a", tags=(), metadata={}, wait=True)

    assert backend.get(OTHER_BANK, "mem_1") is None
    assert backend.recall(OTHER_BANK, "only in bank-a", tag_groups=(), limit=10) == []
    assert backend.list(OTHER_BANK, tag_groups=(), state=None, limit=10, offset=0).total == 0


def test_capabilities_are_honest(backend):
    capabilities = backend.capabilities()

    for name, call in (
        ("synthesis", lambda: backend.reflect(BANK, "q", tag_groups=())),
        ("operations", lambda: backend.get_operation(BANK, "op_1")),
        ("operations", lambda: backend.list_operations(BANK, status=None, limit=10, offset=0)),
        ("operations", lambda: backend.cancel_operation(BANK, "op_1")),
    ):
        if name not in capabilities:
            with pytest.raises(UnsupportedCapability):
                call()


def test_provision_mental_models_is_idempotent(backend):
    if "mental_models" not in backend.capabilities():
        pytest.skip("backend does not declare mental_models")
    builtins = (BuiltinModel(key="k1", name=f"Contract Model {uuid.uuid4().hex[:8]}",
                              prompt="summarize things", version=1),)

    backend.provision_mental_models(BANK, builtins)
    backend.provision_mental_models(BANK, builtins)  # must not raise, must not duplicate

    stored = getattr(backend, "mental_models", None)
    if stored is not None:  # FakeBackend exposes its store directly; assert exact idempotency
        assert len(stored[BANK]) == 1


def test_incomplete_backend_subclass_cannot_be_instantiated():
    class _Incomplete(Backend):
        def capabilities(self):
            return frozenset()

    with pytest.raises(TypeError):
        _Incomplete()
