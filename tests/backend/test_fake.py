"""FakeBackend specifics not covered by the shared contract: token-overlap scoring order,
`capabilities()` and `reset()`."""
from __future__ import annotations

import pytest

from memory.backend.fake import FakeBackend

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


def test_capabilities_are_empty():
    assert FakeBackend().capabilities() == frozenset()


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


def test_reset_clears_memories(backend):
    backend.retain(BANK, "mem_1", "text", tags=(), metadata={}, wait=True)

    backend.reset()

    assert backend.get(BANK, "mem_1") is None
