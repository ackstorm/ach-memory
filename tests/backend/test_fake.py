"""FakeBackend specifics not covered by the shared contract: token-overlap scoring order,
`capabilities()`, and `provision_mental_models`."""
from __future__ import annotations

import pytest

from memory.backend.fake import FakeBackend
from memory.builtin_models import BuiltinModel

BANK = "bank-a"


@pytest.fixture
def backend() -> FakeBackend:
    adapter = FakeBackend()
    adapter.provision(BANK)
    return adapter


def test_recall_orders_by_token_overlap_score(backend):
    # query tokens: {"apple", "banana"}
    backend.retain(BANK, "mem_low", "apple only", tags=(), metadata={}, wait=True)
    backend.retain(BANK, "mem_high", "apple banana", tags=(), metadata={}, wait=True)
    backend.retain(BANK, "mem_none", "totally unrelated words", tags=(), metadata={}, wait=True)

    hits = backend.recall(BANK, "apple banana", tag_groups=(), limit=10)

    assert [h.memory_id for h in hits] == ["mem_high", "mem_low"]
    assert hits[0].score == 1.0
    assert hits[1].score == pytest.approx(1 / 3)


def test_recall_breaks_score_ties_by_memory_id(backend):
    backend.retain(BANK, "mem_b", "apple", tags=(), metadata={}, wait=True)
    backend.retain(BANK, "mem_a", "apple", tags=(), metadata={}, wait=True)

    hits = backend.recall(BANK, "apple", tag_groups=(), limit=10)

    assert [h.memory_id for h in hits] == ["mem_a", "mem_b"]


def test_capabilities_is_mental_models_only():
    assert FakeBackend().capabilities() == frozenset({"mental_models"})


def test_retain_wait_false_returns_pending_but_stores_immediately(backend):
    ack = backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=False)

    assert ack.status == "pending"
    assert backend.get(BANK, "mem_1") is not None
    assert backend.recall(BANK, "text", tag_groups=(), limit=10)[0].memory_id == "mem_1"


def test_retain_wait_true_returns_completed(backend):
    ack = backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=True)

    assert ack.status == "completed"


def test_tag_group_with_invalid_match_raises_value_error(backend):
    backend.retain(BANK, "mem_1", "text", tags=("a",), metadata={}, wait=True)

    with pytest.raises(ValueError):
        backend.recall(BANK, "text", tag_groups=({"tags": ["a"], "match": "xor"},), limit=10)


def test_tag_group_with_empty_tags_raises_value_error(backend):
    backend.retain(BANK, "mem_1", "text", tags=("a",), metadata={}, wait=True)

    with pytest.raises(ValueError):
        backend.recall(BANK, "text", tag_groups=({"tags": [], "match": "any"},), limit=10)


def test_provision_mental_models_creates_one_entry_per_key(backend):
    builtins = (
        BuiltinModel(key="user-context", name="User Context", prompt="p1", scope="user"),
        BuiltinModel(key="project-context", name="Project Context", prompt="p2", scope="project"),
    )

    backend.provision_mental_models(BANK, builtins)

    assert set(backend.mental_models[BANK]) == {"user-context", "project-context"}
    assert backend.mental_models[BANK]["user-context"].prompt == "p1"


def test_provision_mental_models_overwrites_on_repeat_call(backend):
    backend.provision_mental_models(
        BANK, (BuiltinModel(key="user-context", name="User Context", prompt="old", scope="user"),)
    )
    backend.provision_mental_models(
        BANK, (BuiltinModel(key="user-context", name="User Context", prompt="new", scope="user"),)
    )

    assert len(backend.mental_models[BANK]) == 1
    assert backend.mental_models[BANK]["user-context"].prompt == "new"


def test_reset_clears_memories_and_mental_models(backend):
    backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=True)
    backend.provision_mental_models(
        BANK, (BuiltinModel(key="user-context", name="User Context", prompt="p", scope="user"),)
    )

    backend.reset()

    assert backend.get(BANK, "mem_1") is None
    assert backend.mental_models == {}
